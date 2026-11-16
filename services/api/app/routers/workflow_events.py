import asyncio
import json
import uuid
from datetime import date, datetime, time, timedelta
import redis.asyncio as redis
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.config import settings
from app.db import get_session
from app.models import Decision, POCEventMeta, Event, ReminderAsset, ReminderAttempt, ReminderExecution, WorkflowRequest
from app.business_models import BusinessEventContext, BusinessRoom, BusinessProject
from app.auth import session_is_valid
from app.core.workflow import DEMO_SKILL, add_audit, event_detail, event_dict, find_event, publish_event

router = APIRouter()
ACTIONS = {"confirm": "CONFIRMED", "false_positive": "FALSE_POSITIVE", "escalate": "ESCALATED", "close": "CLOSED"}
TRANSITIONS = {"PENDING": set(ACTIONS), "ESCALATED": {"confirm", "false_positive", "close"}, "CONFIRMED": {"close"}, "FALSE_POSITIVE": set(), "CLOSED": set()}


@router.get("/events/stream")
async def event_stream(request: Request):
    async def gen():
        client = redis.from_url(settings.redis_url)
        last_id = "$"
        stream_status = None
        try:
            yield "data: connected\n\n"
            while True:
                if not await asyncio.to_thread(session_is_valid, request.cookies.get("eval_admin_session", "")):
                    break
                try:
                    entries = await client.xread({"events": last_id}, block=2000, count=20)
                    if stream_status != 'live':
                        stream_status = 'live'
                        yield 'data: {"type":"stream_status","status":"live"}\n\n'
                    if not await asyncio.to_thread(session_is_valid, request.cookies.get("eval_admin_session", "")):
                        break
                    if not entries:
                        yield ": heartbeat\n\n"
                    for _, items in entries:
                        for msg_id, data in items:
                            last_id = msg_id
                            decoded = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v) for k, v in data.items()}
                            yield f"data: {json.dumps(decoded, ensure_ascii=False)}\n\n"
                except asyncio.CancelledError:
                    break
                except Exception:
                    if stream_status != 'polling':
                        stream_status = 'polling'
                        yield 'data: {"type":"stream_status","status":"polling"}\n\n'
                    await asyncio.sleep(1)
        finally:
            await client.aclose()
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/events")
def list_events(limit: int = Query(50, ge=1, le=200), status: str | None = None, event_type: str | None = None,
                risk_level: str | None = None, camera_id: str | None = None, phase_id: str | None = None,
                demo_only: bool = False, session_id: str | None = None, session: Session = Depends(get_session)):
    query = session.query(Event).order_by(desc(Event.id))
    for column, value in ((Event.status, status), (Event.event_type, event_type), (Event.risk_level, risk_level),
                          (Event.camera_id, camera_id), (Event.phase_id, phase_id), (Event.session_id, session_id)):
        if value:
            query = query.filter(column == value)
    if demo_only:
        query = query.filter(Event.skill_code == DEMO_SKILL)
    rows = query.limit(limit).all()
    payload = [event_dict(e, session) for e in rows]
    # 事件卡片缩略图：取该事件最新一张关键帧，前端懒加载
    ids = [item["event_id"] for item in payload]
    if ids:
        from urllib.parse import quote
        thumbs = {
            row["event_id"]: row["object_key"]
            for row in session.execute(text(
                "select distinct on (event_id) event_id, object_key from evidences "
                "where evidence_type = 'keyframe' and event_id = any(:ids) "
                "order by event_id, id desc"), {"ids": ids}).mappings().all()
        }
        for item in payload:
            key = thumbs.get(item["event_id"])
            item["thumb"] = f"/api/evidence/keyframes/object?w=160&key={quote(key)}" if key else None
    return payload


@router.get("/events/query")
def query_events(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                 keyword: str = Query("", max_length=200), status: str | None = None, risk_level: str | None = None,
                 event_type: str | None = None, camera_id: str | None = None, room: str | None = Query(None, max_length=100), date_from: date | None = None,
                 date_to: date | None = None, demo_only: bool = False, project_id: str | None = Query(None, max_length=36),
                 room_id: str | None = Query(None, max_length=36), session: Session = Depends(get_session)):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "开始日期不能晚于结束日期")
    query = session.query(Event).outerjoin(POCEventMeta, Event.event_id == POCEventMeta.event_id)
    if project_id is not None or room_id is not None:
        query = query.join(BusinessEventContext, Event.event_id == BusinessEventContext.event_id)
        if project_id is not None:
            query = query.filter(BusinessEventContext.project_id == project_id)
        if room_id is not None:
            query = query.filter(BusinessEventContext.room_id == room_id)
    if demo_only:
        query = query.filter(Event.skill_code == DEMO_SKILL)
    if room is not None:
        query = query.filter(POCEventMeta.room == room)
    for column, value in ((Event.status, status), (Event.risk_level, risk_level), (Event.event_type, event_type), (Event.camera_id, camera_id)):
        if value:
            query = query.filter(column == value)
    if keyword.strip():
        term = "%" + keyword.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.filter(or_(*(col.ilike(term, escape="\\") for col in (Event.event_id, Event.reason, Event.camera_id, POCEventMeta.room, POCEventMeta.note))))
    if date_from:
        query = query.filter(Event.occurred_at >= datetime.combine(date_from, time.min) - timedelta(hours=8))
    if date_to:
        query = query.filter(Event.occurred_at < datetime.combine(date_to + timedelta(days=1), time.min) - timedelta(hours=8))
    records = query.order_by(desc(Event.occurred_at), desc(Event.id)).all()
    today = (datetime.utcnow() + timedelta(hours=8)).date()
    stats = {key: 0 for key in ("today", "pending", "high", "medium", "low", "confirmed", "closed", "false_positive", "escalated")}
    stats["total"] = len(records)
    types, rooms, trend = {}, {}, {}
    room_map = {m.event_id: m.room for m in session.query(POCEventMeta).all()}
    context_map = {context.event_id: context for context in session.query(BusinessEventContext).all()}
    configured_rooms = {room.room_id: room for room in session.query(BusinessRoom).all()}
    configured_projects = {project.project_id: project for project in session.query(BusinessProject).all()}
    for e in records:
        event_day = (e.occurred_at + timedelta(hours=8)).date() if e.occurred_at else None
        stats["today"] += int(event_day == today)
        for key, value in (("pending", "PENDING"), ("confirmed", "CONFIRMED"), ("closed", "CLOSED"), ("false_positive", "FALSE_POSITIVE"), ("escalated", "ESCALATED")):
            if key == "pending":
                # 待人工处置只计业务风险级（info 级人员检测属原始观察流，不进入处置队列）
                stats["pending"] += int(e.status == value and e.risk_level in ("high", "medium", "low"))
            else:
                stats[key] += int(e.status == value)
        if e.risk_level in ("high", "medium", "low"):
            stats[e.risk_level] += 1
        types[e.event_type] = types.get(e.event_type, 0) + 1
        context = context_map.get(e.event_id)
        if context:
            group_key = "room:" + context.room_id
            current_room = configured_rooms.get(context.room_id)
            current_project = configured_projects.get(context.project_id)
            room_name = current_room.name if current_room else context.snapshot.get("room_name", "未记录评标室")
            project_name = current_project.name if current_project else context.snapshot.get("project_name", "未记录项目")
            group = rooms.setdefault(group_key, {"name": f"{project_name} / {room_name}", "value": 0,
                "room_id": context.room_id, "project_id": context.project_id, "project_name": project_name})
        else:
            room_name = room_map.get(e.event_id, e.camera_id or "未关联场所")
            group = rooms.setdefault("legacy:" + room_name, {"name": room_name, "value": 0})
        group["value"] += 1
        if event_day:
            day = event_day.isoformat()
            row = trend.setdefault(day, {"date": day, "high": 0, "medium": 0, "low": 0})
            if e.risk_level in ("high", "medium", "low"):
                row[e.risk_level] += 1
    stats.update(by_type=[{"name": name, "value": count} for name, count in types.items()],
                 rooms=sorted(rooms.values(), key=lambda group: -group["value"]),
                 trend=[trend[day] for day in sorted(trend)[-7:]])
    return {"items": [event_dict(e, session) for e in records[(page - 1) * page_size:page * page_size]], "total": len(records), "page": page, "page_size": page_size, "stats": stats}


@router.get("/events/{event_id}")
def get_event(event_id: str, session: Session = Depends(get_session)):
    return event_detail(session, event_id)


class DecisionIn(BaseModel):
    action: str
    reviewer_id: str = Field("demo-reviewer", min_length=1, max_length=64)
    note: str = Field("", max_length=2000)
    expected_status: str | None = None
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=1, max_length=64)


def request_result(session, record):
    reminder = session.query(ReminderExecution).filter(ReminderExecution.decision_id == record.decision_id).first()
    return {"ok": True, "status": record.result_status, "decision_id": record.decision_id,
            "reminder_id": reminder.execution_id if reminder else None, "duplicate": True}


@router.post("/events/{event_id}/decisions")
def decide(event_id: str, body: DecisionIn, request: Request, session: Session = Depends(get_session)):
    reviewer_id = request.state.admin["username"]
    if body.action not in ACTIONS:
        raise HTTPException(422, "无效处置动作")
    if body.action in ("false_positive", "escalate") and not body.note.strip():
        raise HTTPException(422, "误报或升级需要填写原因")
    event = find_event(session, event_id, lock=True)
    saved = session.get(WorkflowRequest, body.request_id)
    if saved:
        if (saved.event_id, saved.action, saved.note, saved.reviewer_id) != (event_id, body.action, body.note.strip(), reviewer_id):
            raise HTTPException(409, "请求编号已用于不同操作")
        return request_result(session, saved)
    from app.supervision_models import SupervisionCase
    if session.get(SupervisionCase, event_id):
        raise HTTPException(409, '该事件已进入监管闭环，请在事件工作台完成确认、处置和复核归档')
    if body.expected_status is not None and body.expected_status != event.status:
        raise HTTPException(409, "事件状态已变化，请刷新后操作")
    if body.action not in TRANSITIONS.get(event.status, set()):
        raise HTTPException(409, "当前状态不允许此操作，请刷新事件")
    previous = event.status
    event.status = ACTIONS[body.action]
    decision = Decision(event_id=event_id, action=body.action, reviewer_id=reviewer_id, note=body.note.strip())
    session.add(decision)
    session.flush()
    reminder = None
    if body.action == "confirm":
        asset = session.get(ReminderAsset, 1)
        configured = bool(asset and asset.url)
        reminder = ReminderExecution(execution_id=str(uuid.uuid4()), event_id=event_id, decision_id=decision.id,
            status="QUEUED" if configured else "NOT_CONFIGURED", asset_url=asset.url if configured else None,
            asset_version=asset.version if configured else None, error=None if configured else "预制监督提醒视频未配置")
        session.add(reminder)
        session.add(ReminderAttempt(attempt_id=str(uuid.uuid4()), execution_id=reminder.execution_id,
            status=reminder.status, asset_url=reminder.asset_url, asset_version=reminder.asset_version, error=reminder.error))
    saved = WorkflowRequest(request_id=body.request_id, event_id=event_id, action=body.action,
        reviewer_id=reviewer_id, note=body.note.strip(), decision_id=decision.id, result_status=event.status)
    session.add(saved)
    add_audit(session, event_id, body.action, f"{previous} → {event.status}；{body.note.strip()}", reviewer_id)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "操作请求冲突，请刷新记录")
    publish_event(event, session)
    return {"ok": True, "status": event.status, "decision_id": decision.id, "reminder_id": reminder.execution_id if reminder else None, "duplicate": False}


@router.get("/events/{event_id}/export")
def export_event(event_id: str, request: Request, session: Session = Depends(get_session)):
    find_event(session, event_id)
    add_audit(session, event_id, "export", "导出事件与操作留痕清单（无视频时不含视频证据）", request.state.admin["username"])
    session.commit()
    detail = event_detail(session, event_id)
    document = {"description": "业务模拟记录；模拟输入不构成真实现场证据。" if detail['is_demo'] else "监管事件记录，包含人工核查、处置及操作留痕；机器线索不等同于违规认定。", "event": detail}
    return Response(json.dumps(document, ensure_ascii=False, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="event-{event_id}.json"', "Cache-Control": "no-store"})
