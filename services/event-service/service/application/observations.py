"""Application behavior with explicit runtime dependencies; no startup side effects."""

from __future__ import annotations


async def handle_observation(context, r, data: dict, last_emit: dict):
    data = dict(data)
    obs_type = data.get("type")
    if (
        obs_type not in context.EVENT_TEMPLATES
        and obs_type not in context.RECOVERY_MAP
        and obs_type not in (context.A_EVENT_TYPE, "behavior_detected")
    ):
        return
    data.setdefault("occurred_at", str(context.time.time()))
    if obs_type == context.A_EVENT_TYPE:
        try:
            clue_time = context.json.loads(data["payload"])["clue"]["occurred_at"]
            data["occurred_at"] = str(
                context.datetime.fromisoformat(
                    clue_time.replace("Z", "+00:00")
                ).timestamp()
            )
        except (KeyError, ValueError, TypeError, AttributeError):
            return
    scope = context.observation_scope(context.Session, data)
    if scope:
        data["session_id"] = scope["session_id"]
        data["phase_id"] = scope.get("phase_id")
        if scope.get("enabled_scenes") is not None:
            context.phase_scene_cache[scope["session_id"]] = set(
                scope["enabled_scenes"]
            )
        try:
            context.record_signal(context.Session, data)
        except Exception:
            context.log.warning("来源分析回执写入失败，继续处理业务观察")
    obs_type = data.get("type")
    now = context.time.time()
    if obs_type in context.RECOVERY_MAP:
        await context.handle_recovery(r, data)
        return
    if obs_type == context.A_EVENT_TYPE:
        event = context.build_a_event(
            data,
            event_cls=context.Event,
            last_emit=last_emit,
            now=now,
            allowed=context.phase_scene_cache.get(data.get("session_id"))
            if scope
            else None,
            cooldown_for=context.cooldown_for,
        )
        if event:
            event._risk_clue = context.json.loads(data["payload"])["clue"]
            if scope and any(
                event._risk_clue.get(k) != scope[k]
                for k in ("project_id", "room_id", "session_id")
            ):
                context.log.warning("A场景线索作用域与当前绑定不符，未接入")
                return
            await context.commit_and_publish(r, event, scope=scope)
        return
    if obs_type not in context.EVENT_TEMPLATES and obs_type != "behavior_detected":
        return
    payload = context.json.loads(data.get("payload", "{}"))
    if obs_type == "behavior_detected" and (
        not scope or payload.get("phase_id") != scope.get("phase_id")
    ):
        return
    occurred = float(data.get("occurred_at", context.time.time()))
    source = data.get("source_id", "camera-001")
    # 每条观察都可能带最新关键帧（worker 每 5 秒存一张），先记下来，供后续事件兜底
    fresh_key = payload.get("frame_key")
    if fresh_key:
        context.LAST_KEYFRAME[source] = (fresh_key, now)
    session_id = data.get("session_id", "session-001")
    if session_id in (None, "", "session-001"):
        session_id = context.active_session_id() or session_id
    allowed = context.phase_scene_cache.get(session_id) if scope else None
    if allowed is not None and not context.observation_allowed(
        session_id, obs_type, allowed
    ):
        context.log.info(
            "当前阶段忽略观察：phase-scene=%s type=%s", sorted(allowed), obs_type
        )
        return

    if obs_type == "person_detected":
        payload["absence_seconds"] = context.compute_absence_seconds(
            context.ABSENCE_SEEN, source, int(payload.get("count", 0) or 0), occurred
        )
    observed_groups = set()
    applied_codes = set()
    applied_types = set()
    applied_scopes = set()
    observed_keys = set()
    for config in context.refresh_operator_cache().values():
        if (
            obs_type == "person_detected"
            and config.code in ("phone_call", "phone_capture")
            and "behavior_detected" in config.input_types
        ):
            # Upgraded action rules use captured temporal evidence; an unreviewed frame cannot clear them.
            continue
        if obs_type == "behavior_detected":
            if (config.code == "unauthorized_entry") != bool(
                payload.get("identity_only")
            ):
                continue
            if bool(payload.get("review_only")) != (
                config.code
                in ("whispering", "document_transfer", "phone_call", "phone_capture")
            ):
                continue
        if not context.scene_allowed(config.code, source, allowed):
            continue
        scope_data = data
        if obs_type == "behavior_detected" and config.zone_id:
            scope_data = {**data, "zone_id": config.zone_id}
        if config.zone_id and any(
            str(seat.get("zone_id") or seat.get("name")) == config.zone_id
            for seat in payload.get("seats", [])
        ):
            scope_data = {**data, "zone_id": config.zone_id}
        try:
            candidates = context.evaluate_candidates(config, data, payload)
        except (TypeError, ValueError, KeyError, IndexError):
            context.log.warning(
                "规则计算失败，已隔离：code=%s source=%s", config.code, source
            )
            continue
        if (
            config.enabled
            and obs_type in config.input_types
            and context._scope_matches(config, scope_data)
        ):
            applied_codes.add(config.code)
            applied_scopes.add((config.code, config.zone_id))
            applied_types.add((config.output_event_type, config.zone_id))
        for subject, result in candidates:
            key = (source, result.event_type, subject)
            observed_keys.add(key)
            if (
                not subject
                and key in last_emit
                and now - last_emit[key] < context.cooldown_for(result.event_type)
            ):
                continue
            event_time = float(result.detail.get("ended_at", occurred))
            evt = context.Event(
                event_id=str(context.uuid.uuid4()),
                session_id=session_id,
                camera_id=source,
                zone_id=(result.detail.get("zone_id") or config.zone_id)
                if obs_type == "behavior_detected"
                else subject,
                person_id=result.detail.get("person_id"),
                seat_id=result.detail.get("seat_id"),
                skill_code=config.code,
                skill_version=config.version,
                model_name=data.get(
                    "model_name", payload.get("model_name", "operator")
                ),
                model_version=data.get(
                    "model_version", payload.get("model_version", "-")
                ),
                event_type=result.event_type,
                risk_level=result.risk_level,
                confidence=result.confidence,
                reason=result.reason,
                occurred_at=context.datetime.utcfromtimestamp(event_time),
                phase_id=scope.get("phase_id") if scope else None,
                status="PENDING",
            )
            if obs_type == "behavior_detected" and payload.get("frame_key"):
                evt._keyframe_captured_at = context.datetime.utcfromtimestamp(occurred)
            if subject:
                duration = float(result.detail.get("absence_seconds", 0))
                group = context.episode_key(evt, subject, payload.get("media_run_id"))
                observed_groups.add(group)
                evt._episode = {
                    "group": group,
                    "started_at": context.datetime.utcfromtimestamp(
                        float(result.detail.get("started_at", event_time - duration))
                    ),
                    "detail": {
                        **result.detail,
                        "subject": subject,
                        "frame_key": payload.get("frame_key"),
                    },
                }
            if not context.sustained(source, result.event_type, now, subject):
                continue
            if subject and key in context.SUSTAIN:
                evt._episode["started_at"] = min(
                    evt._episode["started_at"],
                    evt.occurred_at
                    - context.timedelta(seconds=max(0, now - context.SUSTAIN[key][0])),
                )
            if await context.commit_and_publish(
                r, evt, payload.get("frame_key"), scope
            ):
                last_emit[key] = now
    for key in list(context.SUSTAIN):
        if (
            key[0] == source
            and ((key[1], None) in applied_types or (key[1], key[2]) in applied_types)
            and key not in observed_keys
        ):
            context.SUSTAIN.pop(key, None)
    if (
        scope
        and applied_codes
        and (obs_type != "behavior_detected" or payload.get("quality") == "clear")
    ):
        context.finish_observed_episodes(
            context.Session, data, observed_groups, applied_codes, applied_scopes
        )

    if obs_type == "behavior_detected":
        return

    frame_key = None
    if obs_type == "person_detected":
        frame_key = payload.get("frame_key")
        detail = {
            "count": payload.get("count", 0),
            "max_conf": payload.get("max_conf", 0),
            "occurred_at": occurred,
        }
    elif obs_type == "transcript_text":
        detail = {"occurred_at": occurred, "text": payload.get("text", "")}
    elif obs_type == "sensitive_word":
        detail = {
            "occurred_at": occurred,
            "word": payload.get("word", ""),
            "context": payload.get("context", ""),
        }
    elif obs_type == "long_talking":
        detail = {
            "occurred_at": occurred,
            "duration_sec": payload.get("duration_sec", 0),
        }
    else:
        detail = {"occurred_at": occurred}

    if obs_type not in context.EVENT_TEMPLATES:
        return
    tkey = (source, obs_type)
    if tkey in last_emit and now - last_emit[tkey] < context.cooldown_for(obs_type):
        return
    last_emit[tkey] = now
    evt = context.EVENT_TEMPLATES[obs_type](source, session_id, detail)
    await context.commit_and_publish(r, evt, frame_key, scope)
