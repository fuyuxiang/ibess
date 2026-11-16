"""Application behavior with explicit runtime dependencies; no startup side effects."""

from __future__ import annotations
from ibess_db.models import Event
from ibess_contracts.messages import event_message


async def handle_recovery(context, r, data: dict):
    """设备恢复：自动关闭对应的 PENDING 设备异常事件（CLOSED + Decision 记录）。"""
    obs_type = data.get("type")
    target_type = context.RECOVERY_MAP.get(obs_type)
    if not target_type:
        return
    source = data.get("source_id", "camera-001")
    session = context.Session()
    try:
        evts = (
            session.query(context.Event)
            .filter(
                context.Event.event_type == target_type,
                context.Event.camera_id == source,
                context.Event.session_id == data.get("session_id", "session-001"),
                context.Event.status == "PENDING",
            )
            .all()
        )
        for e in evts:
            e.status = "CLOSED"
            session.add(
                context.Decision(
                    event_id=e.event_id,
                    action="auto_close",
                    reviewer_id="system",
                    note="设备恢复自动关闭",
                )
            )
        session.commit()
        if evts:
            context.log.info(
                "自动关闭 %d 条 %s 事件（source=%s）", len(evts), target_type, source
            )
    except Exception as e:
        session.rollback()
        context.log.warning("自动关闭失败: %s", e)
    finally:
        session.close()


async def commit_and_publish(
    context, r, evt: Event, frame_key: str | None = None, scope=None
):
    if frame_key:
        context.LAST_KEYFRAME[evt.camera_id] = (frame_key, context.time.time())
    elif evt.camera_id in context.LAST_KEYFRAME:
        cached_key, cached_at = context.LAST_KEYFRAME[evt.camera_id]
        if context.time.time() - cached_at <= context.KEYFRAME_REUSE_SECONDS:
            frame_key = cached_key

    if scope:
        evt.session_id = scope["session_id"]
        evt.phase_id = scope["phase_id"]
    session = context.Session()
    saved = False
    try:
        clue = getattr(evt, "_risk_clue", None)
        if clue:
            from service.event_adapter import _insert_ignore, parse_utc

            values = {
                column.name: clue[column.name]
                for column in context.RiskClueRecord.__table__.columns
                if column.name in clue
            }
            values.update(
                event_id=evt.event_id,
                clue=dict(clue),
                occurred_at=parse_utc(clue["occurred_at"]),
            )
            if not _insert_ignore(session, context.RiskClueRecord.__table__, values):
                return False
        evt, is_new = context.persist_episode(session, evt)
        session.add(evt)
        session.flush()
        context.save_context(session, evt, scope)
        if clue and clue.get("conclusion") == "insufficient":
            session.execute(
                context.text(
                    "INSERT INTO supervision_cases(event_id,status,version,updated_at) VALUES (:id,'SUPPLEMENT',0,:at) ON CONFLICT(event_id) DO NOTHING"
                ),
                {"id": evt.event_id, "at": context.datetime.utcnow()},
            )
        session.commit()
        saved = True
        context.log.info(
            "事件创建: %s (%s, %s)", evt.event_id, evt.camera_id, evt.event_type
        )
        if frame_key and is_new:
            evid = context.Evidence(
                event_id=evt.event_id,
                evidence_type="keyframe",
                object_key=frame_key,
                captured_at=getattr(evt, "_keyframe_captured_at", None)
                or context.datetime.utcnow(),
            )
            session.add(evid)
            session.commit()
            context.log.info("证据记录: %s → %s", evt.event_id[:8], frame_key)
        await r.xadd(
            "events",
            event_message(
                {
                    "event_id": evt.event_id,
                    "event_type": evt.event_type,
                    "camera_id": evt.camera_id,
                    "session_id": evt.session_id,
                    "risk_level": evt.risk_level,
                    "reason": evt.reason,
                    "status": evt.status,
                    "occurred_at": evt.occurred_at.isoformat(),
                }
            ),
        )
        await r.xtrim("events", maxlen=1000)
    except Exception as e:
        session.rollback()
        context.log.warning("入库失败: %s", e)
    finally:
        session.close()
    return saved
