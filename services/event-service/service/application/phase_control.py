"""Application behavior with explicit runtime dependencies; no startup side effects."""

from __future__ import annotations


def send_phase_ack(
    context,
    notification: dict,
    payload: dict,
    status: str = "applied",
    error: dict | None = None,
):
    """按2.0.0契约向API回写ack；失败仅记日志（快照将保持pending，可由API侧观察）。"""
    if not context.PHASE_CONTROL_API or not context.PHASE_CONTROL_TOKEN:
        return
    received_at = context.datetime.now(context.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    def work():
        import urllib.request
        from urllib.error import HTTPError

        body = {
            "contract_version": context.PHASE_CONTROL_CONTRACT,
            "project_id": str(payload.get("project_id", "")),
            "room_id": str(payload.get("room_id", "")),
            "session_id": str(
                notification.get("session_id", payload.get("session_id", ""))
            ),
            "source_id": str(
                payload.get("source_id", notification.get("source_id", ""))
            ),
            "stage_snapshot_id": str(payload.get("stage_snapshot_id", "")),
            "phase_id": str(payload.get("phase_id", "")),
            "phase_version": int(payload.get("phase_version", 0)),
            "consumer_id": context.PHASE_CONTROL_CONSUMER_ID,
            "received_at": received_at,
            "applied_at": received_at if status == "applied" else None,
            "status": status,
            "error": error,
        }
        req = urllib.request.Request(
            f"{context.PHASE_CONTROL_API}/internal/phase-control/sessions/{body['session_id']}/acks/{context.PHASE_CONTROL_CONSUMER_ID}",
            method="PUT",
            data=context.json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {context.PHASE_CONTROL_TOKEN}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                context.log.info(
                    "阶段控制ack已回写: notification=%s status=%s http=%s",
                    str(notification.get("notification_id"))[:8],
                    status,
                    resp.status,
                )
        except HTTPError as exc:
            context.log.warning("阶段控制ack被拒(%s): %s", exc.code, exc.read()[:200])
        except Exception as exc:
            context.log.warning("阶段控制ack发送失败: %s", exc)

    context.threading.Thread(target=work, daemon=True).start()


async def handle_phase_control(context, r, data: dict):
    """阶段控制消息：更新本地场景缓存后回写ack。"""
    if data.get("type") != "phase_changed":
        return
    context.update_phase_cache(data)
    try:
        payload = context.json.loads(data.get("payload", "{}"))
        required = ("project_id", "room_id", "stage_snapshot_id", "phase_id")
        if all(payload.get(field) for field in required):
            context.send_phase_ack(data, payload, status="applied")
        else:
            context.log.warning("阶段控制消息缺少ack必需字段，跳过回写")
    except (TypeError, ValueError) as exc:
        context.log.warning("阶段控制ack组装失败: %s", exc)


def update_phase_cache(context, data: dict):
    try:
        config = context.json.loads(data.get("payload", "{}"))
        scenes = set(config.get("enabled_scenes", []))
        session_id = data.get("session_id", "session-001")
        context.phase_scene_cache[session_id] = scenes
        # 预制演示 worker 默认上报 session-001：这里必须覆盖，
        # 用 setdefault 会因为启动时已写入旧场景而永远不更新（表现为换阶段后仍按上一阶段过滤）
        context.phase_scene_cache["session-001"] = scenes
        context.log.info(
            "阶段规则已切换：session=%s scenes=%s asset=%s",
            session_id,
            sorted(scenes),
            config.get("media_asset"),
        )
    except (TypeError, ValueError, context.json.JSONDecodeError):
        context.log.warning("阶段控制消息格式无效")
