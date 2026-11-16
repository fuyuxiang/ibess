"""媒体绑定 API（4.3）：录音↔评标室绑定（单条/批量/查询/解绑），语义研判面板自动消费。

绑定后语义面板按绑定关系自动取该评标室的有效授权快照；未绑定时保留人工选择兜底。
"""
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.audio_models import AudioRecording
from app.business_models import BusinessRoom
from app.db import get_session
from app.media_binding_models import MediaBinding
from pydantic import Field

router = APIRouter()


def _actor(request: Request):
    admin = getattr(request.state, "admin", None)
    if not isinstance(admin, dict) or not admin.get("username"):
        raise HTTPException(403, "管理员身份缺失，无法操作媒体绑定")
    return admin["username"]


def _recording(session: Session, recording_id: str):
    row = session.get(AudioRecording, recording_id)
    if row is None:
        raise HTTPException(404, "录音不存在")
    return row


def _room_checked(session: Session, room_id: str):
    room = session.get(BusinessRoom, room_id)
    if room is None:
        raise HTTPException(422, "评标室不存在")
    return room


def _serialize(binding: MediaBinding) -> dict:
    return {"binding_id": binding.binding_id, "recording_id": binding.recording_id,
            "project_id": binding.project_id, "room_id": binding.room_id,
            "actor": binding.actor,
            "created_at": binding.created_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}


class BindingIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    recording_id: UUID
    room_id: UUID


class BatchBindingIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    recording_ids: list[UUID] = Field(min_length=1, max_length=50)
    room_id: UUID




@router.post("/media-bindings", status_code=201)
def bind_recording(body: BindingIn, request: Request, session: Session = Depends(get_session)):
    """绑定单条录音到评标室；重复绑定同评标室幂等，换评标室则覆盖（重新绑定）。"""
    actor = _actor(request)
    recording = _recording(session, str(body.recording_id))
    # 录音本身不属于项目；project_id 由所选评标室推导（room→project 一致性由复合外键保证）。
    room = _room_checked(session, str(body.room_id))
    project_id = str(room.project_id)
    existing = session.query(MediaBinding).filter_by(recording_id=recording.recording_id).first()
    if existing is not None:
        if str(existing.room_id) == str(body.room_id):
            return {**_serialize(existing), "deduplicated": True}
        existing.room_id = str(body.room_id)
        existing.project_id = project_id
        existing.actor = actor
        existing.created_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(existing)
        return {**_serialize(existing), "deduplicated": False, "rebound": True}
    binding = MediaBinding(binding_id=str(uuid4()), recording_id=recording.recording_id,
                           project_id=project_id, room_id=str(body.room_id),
                           actor=actor, created_at=datetime.now(timezone.utc))
    session.add(binding)
    session.commit()
    session.refresh(binding)
    return {**_serialize(binding), "deduplicated": False}


@router.post("/media-bindings/batch", status_code=201)
def bind_batch(body: BatchBindingIn, request: Request, session: Session = Depends(get_session)):
    """批量绑定多条录音到同一评标室；返回逐条结果（bound/deduplicated）。"""
    results = []
    for rid in body.recording_ids:
        single = bind_recording(BindingIn(recording_id=rid, room_id=body.room_id), request, session)
        results.append({"recording_id": str(rid), "status": "deduplicated" if single["deduplicated"] else "bound",
                        "binding_id": single["binding_id"]})
    return {"items": results, "total": len(results)}


@router.get("/media-bindings")
def list_bindings(room_id: UUID | None = None, session: Session = Depends(get_session)):
    query = session.query(MediaBinding)
    if room_id is not None:
        query = query.filter_by(room_id=str(room_id))
    rows = query.order_by(MediaBinding.created_at.desc()).limit(100).all()
    return {"items": [_serialize(row) for row in rows], "total": len(rows)}


@router.get("/media-bindings/by-recording/{recording_id}")
def get_binding_by_recording(recording_id: UUID, session: Session = Depends(get_session)):
    binding = session.query(MediaBinding).filter_by(recording_id=str(recording_id)).first()
    if binding is None:
        raise HTTPException(404, "该录音尚未绑定评标室")
    return _serialize(binding)


@router.delete("/media-bindings/{recording_id}")
def unbind_recording(recording_id: UUID, session: Session = Depends(get_session)):
    binding = session.query(MediaBinding).filter_by(recording_id=str(recording_id)).first()
    if binding is None:
        raise HTTPException(404, "该录音尚未绑定评标室")
    session.delete(binding)
    session.commit()
    return {"ok": True}
