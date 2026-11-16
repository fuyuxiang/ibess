"""无视频业务演示入口和平台内预制提醒播放记录。"""
import re
import uuid
from datetime import datetime
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.core.workflow import DEMO_SKILL, add_audit, event_dict, find_event, publish_event, reminder_dict
from app.db import get_session
from app.models import POCEventMeta, Event, ReminderAsset, ReminderAttempt, ReminderExecution
from app.business_models import BusinessEventContext, BusinessProject, BusinessRoom, BusinessSeat, BusinessPerson
from app.core.business_registry import lock_registry

router = APIRouter()
EVENTS = {
    "crowd_gathering": ("人员聚集", "medium"), "seat_absence": ("异常离席", "medium"),
    "camera_blacked": ("视频遮挡", "high"), "video_offline": ("设备离线", "high"),
    "sensitive_word": ("敏感词提示", "high"),
}


class POCInput(BaseModel):
    event_type: Literal["crowd_gathering", "seat_absence", "camera_blacked", "video_offline", "sensitive_word"]
    room: str = Field("评标室 A01", min_length=1, max_length=128)
    room_id: str | None = Field(None, min_length=1, max_length=36)
    seat_id: str | None = Field(None, min_length=1, max_length=36)
    note: str = Field("", max_length=2000)
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=1, max_length=64)


def same_demo_input(db, body, event, meta):
    context = db.get(BusinessEventContext, event.event_id)
    if context:
        return (event.event_type, context.room_id, context.seat_id, meta.note) == (body.event_type, body.room_id, body.seat_id, body.note.strip())
    return body.room_id is None and body.seat_id is None and (event.event_type, meta.room, meta.note) == (body.event_type, body.room.strip(), body.note.strip())


def resolve_business_context(db, body):
    if not body.room_id:
        if body.seat_id:
            raise HTTPException(422, "选择工位前请先选择评标室")
        return None
    room = db.query(BusinessRoom).filter(BusinessRoom.room_id == body.room_id).with_for_update().first()
    if room is None:
        raise HTTPException(404, "评标室配置不存在")
    project = db.query(BusinessProject).filter(BusinessProject.project_id == room.project_id).with_for_update().first()
    if not room.enabled or project.status == "closed":
        raise HTTPException(422, "评标室已停用或所属项目已结束，请选择有效配置")
    seat, person = None, None
    if body.seat_id:
        seat = db.query(BusinessSeat).filter(BusinessSeat.seat_id == body.seat_id).with_for_update().first()
        if seat is None:
            raise HTTPException(404, "工位配置不存在")
        if seat.room_id != room.room_id or not seat.enabled:
            raise HTTPException(422, "工位不属于当前评标室或已停用")
        if seat.person_id:
            person = db.query(BusinessPerson).filter(BusinessPerson.person_id == seat.person_id).with_for_update().first()
            if person is None or not person.enabled:
                raise HTTPException(422, "工位分配人员已停用，请先调整分配")
    return {
        "project_id": project.project_id, "room_id": room.room_id,
        "seat_id": seat.seat_id if seat else None, "person_id": person.person_id if person else None,
        "snapshot": {"project_name": project.name, "project_code": project.code, "room_name": room.name,
            "room_code": room.code, "seat_code": seat.code if seat else None,
            "person_name": person.name if person else None, "person_code": person.code if person else None},
    }


@router.post("/demo/events", status_code=201)
def create_demo(body: POCInput, request: Request, session: Session = Depends(get_session)):
    # 在幂等键查询前加锁：等待期间已提交的请求应优先重放，避免配置停用导致重试失败。
    if body.room_id:
        lock_registry(session)
    room, note = body.room.strip(), body.note.strip()
    if not room:
        raise HTTPException(422, "请填写评标室")
    existing = session.query(POCEventMeta).filter(POCEventMeta.request_id == body.request_id).first()
    if existing:
        event = find_event(session, existing.event_id)
        if not same_demo_input(session, body, event, existing):
            raise HTTPException(409, "请求编号已用于不同演示输入")
        return event_dict(event, session)
    context = resolve_business_context(session, body)
    if context:
        room = context["snapshot"]["room_name"]
    label, risk = EVENTS[body.event_type]
    event = Event(event_id=str(uuid.uuid4()), event_type=body.event_type, risk_level=risk,
        camera_id="demo-source", session_id="demo-session", skill_code=DEMO_SKILL, skill_version="v1",
        model_name="simulation", model_version="-", confidence=None,
        reason=f"模拟输入：{room} · {label}" + (f"；{note}" if note else ""),
        occurred_at=datetime.utcnow(), status="PENDING")
    session.add(event)
    session.flush()
    if context:
        session.add(BusinessEventContext(event_id=event.event_id, **context))
    session.add(POCEventMeta(event_id=event.event_id, room=room, note=note, request_id=body.request_id))
    add_audit(session, event.event_id, "simulate", f"模拟生成{label}；{note}", request.state.admin["username"])
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        # A concurrent duplicate may have committed the same key while this request was inserting.
        existing = session.query(POCEventMeta).filter(POCEventMeta.request_id == body.request_id).first()
        if existing is None:
            raise HTTPException(409, "演示事件创建冲突，请重试")
        event = find_event(session, existing.event_id)
        if not same_demo_input(session, body, event, existing):
            raise HTTPException(409, "请求编号已用于不同演示输入")
        return event_dict(event, session)
    publish_event(event, session)
    return event_dict(event, session)


class AssetIn(BaseModel):
    url: str | None = None
    version: str = Field("v1", min_length=1, max_length=64)

    @field_validator("url")
    @classmethod
    def valid_path(cls, value):
        if value is None or value == "":
            return None
        if not re.fullmatch(r"/reminder-assets/[A-Za-z0-9_-]+\.(mp4|webm)", value):
            raise ValueError("仅支持本平台 /reminder-assets/文件名.mp4 或 .webm 素材路径")
        return value


def asset_dict(asset):
    return {"configured": bool(asset and asset.url), "url": asset.url if asset else None, "version": asset.version if asset else None}


@router.get("/demo/reminder")
def get_asset(session: Session = Depends(get_session)):
    return asset_dict(session.get(ReminderAsset, 1))


@router.put("/demo/reminder")
def configure_asset(body: AssetIn, session: Session = Depends(get_session)):
    asset = session.get(ReminderAsset, 1)
    if asset is None:
        asset = ReminderAsset(id=1)
        session.add(asset)
    asset.url, asset.version = body.url, body.version.strip()
    if not asset.version:
        raise HTTPException(422, "素材版本不能为空")
    session.commit()
    return asset_dict(asset)


@router.get("/events/{event_id}/reminders")
def list_reminders(event_id: str, session: Session = Depends(get_session)):
    find_event(session, event_id)
    return [reminder_dict(r, session) for r in session.query(ReminderExecution).filter(ReminderExecution.event_id == event_id).order_by(ReminderExecution.created_at).all()]


class PlaybackIn(BaseModel):
    status: Literal["PLAYING", "COMPLETED", "FAILED"]
    error: str = Field("", max_length=1000)
    attempt_id: str = Field(min_length=1, max_length=36)


def find_reminder(session, execution_id):
    record = session.query(ReminderExecution).filter(ReminderExecution.execution_id == execution_id).with_for_update().first()
    if record is None:
        raise HTTPException(404, "提醒执行记录不存在")
    return record


@router.post("/reminders/{execution_id}/status")
def playback_status(execution_id: str, body: PlaybackIn, request: Request, session: Session = Depends(get_session)):
    record = find_reminder(session, execution_id)
    attempt = session.query(ReminderAttempt).filter(ReminderAttempt.execution_id == execution_id).order_by(ReminderAttempt.created_at.desc()).first()
    if attempt is None or body.attempt_id != attempt.attempt_id:
        raise HTTPException(409, "播放轮次已变化，请刷新；旧回执不会覆盖新任务")
    if record.status == body.status:
        return reminder_dict(record, session)
    allowed = {"QUEUED": {"PLAYING", "FAILED"}, "PLAYING": {"COMPLETED", "FAILED"}}
    if body.status not in allowed.get(record.status, set()):
        raise HTTPException(409, "当前播放状态不允许此操作")
    record.status = body.status
    if body.status == "PLAYING":
        record.started_at = datetime.utcnow()
    else:
        record.completed_at = datetime.utcnow()
    record.error = (body.error.strip() or "播放失败或中断") if body.status == "FAILED" else None
    attempt.status, attempt.error = record.status, record.error
    attempt.started_at, attempt.completed_at = record.started_at, record.completed_at
    add_audit(session, record.event_id, "reminder_" + body.status.lower(), record.error or "平台内客户端报告播放状态", request.state.admin["username"])
    session.commit()
    publish_event(find_event(session, record.event_id), session)
    return reminder_dict(record, session)


@router.post("/reminders/{execution_id}/retry")
def retry_reminder(execution_id: str, request: Request, session: Session = Depends(get_session)):
    record = find_reminder(session, execution_id)
    if record.status not in ("FAILED", "NOT_CONFIGURED", "PLAYING"):
        raise HTTPException(409, "只有失败、未配置或中断的任务可重试")
    if record.status == "PLAYING" and record.started_at and (datetime.utcnow() - record.started_at).total_seconds() < 120:
        raise HTTPException(409, "播放尚未超时，请等待或记录失败后重试")
    asset = session.get(ReminderAsset, 1)
    if asset is None or not asset.url:
        raise HTTPException(409, "预制监督提醒视频未配置")
    add_audit(session, record.event_id, "reminder_retry", f"原状态：{record.status}；原结果：{record.error or '无完成回执'}", request.state.admin["username"])
    previous = session.query(ReminderAttempt).filter(ReminderAttempt.execution_id == execution_id).order_by(ReminderAttempt.created_at.desc()).first()
    if previous and previous.status == "PLAYING":
        previous.status, previous.error, previous.completed_at = "FAILED", "播放超时后人工重试", datetime.utcnow()
    record.asset_url, record.asset_version = asset.url, asset.version
    record.status, record.error = "QUEUED", None
    record.started_at, record.completed_at = None, None
    session.add(ReminderAttempt(attempt_id=str(uuid.uuid4()), execution_id=execution_id,
        asset_url=record.asset_url, asset_version=record.asset_version, status="QUEUED"))
    session.commit()
    publish_event(find_event(session, record.event_id), session)
    return reminder_dict(record, session)
