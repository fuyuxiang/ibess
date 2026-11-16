"""评分记录 API（9.3）：固定JSON评分文档的登记、查询与内容哈希校验。

最小路径：评分数据为固定结构JSON（计划约定"评分先固定JSON"），登记时计算规范SHA-256
（幂等去重+完整性依据），校验端点重算比对。正式评分系统对接属后续增强。
"""
import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID, uuid4

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy.orm import Session

from app.business_models import BusinessRoom
from app.db import get_session
from app.score_models import BusinessScoreRecord

router = APIRouter()
PhaseId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class ScoreItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    subject: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    item: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    score: float
    max_score: float = Field(gt=0)
    basis: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)] | None = None

    from pydantic import model_validator

    @model_validator(mode="after")
    def score_range(self):
        if not (0 <= self.score <= self.max_score):
            raise ValueError("得分必须在 0 与满分之间")
        self.score = round(self.score, 4)
        return self


class ScoreRecordIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project_id: UUID
    room_id: UUID
    phase_id: PhaseId
    scores: list[ScoreItem] = Field(min_length=1, max_length=100)
    occurred_at: datetime | None = None

    @field_validator("occurred_at")
    @classmethod
    def normalize_time(cls, value):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def canonical_sha256(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _actor(request: Request):
    admin = getattr(request.state, "admin", None)
    if not isinstance(admin, dict) or not admin.get("username"):
        raise HTTPException(403, "管理员身份缺失，无法登记评分记录")
    return admin["username"]


def _serialize(record: BusinessScoreRecord) -> dict:
    return {"record_id": record.record_id, "project_id": record.project_id, "room_id": record.room_id,
            "phase_id": record.phase_id, "payload": record.payload, "content_sha256": record.content_sha256,
            "actor": record.actor,
            "created_at": record.created_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}


@router.post("/scores", status_code=201)
def create_score_record(body: ScoreRecordIn, request: Request, session: Session = Depends(get_session)):
    room = session.get(BusinessRoom, str(body.room_id))
    if room is None:
        raise HTTPException(422, "评标室不存在")
    if str(room.project_id) != str(body.project_id):
        raise HTTPException(422, "评分记录所属项目与评标室不一致")
    payload = {
        "project_id": str(body.project_id), "room_id": str(body.room_id), "phase_id": body.phase_id,
        "scores": [item.model_dump() for item in body.scores],
        "occurred_at": (body.occurred_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    digest = canonical_sha256(payload)
    existing = session.query(BusinessScoreRecord).filter_by(content_sha256=digest).first()
    if existing is not None:
        return {**_serialize(existing), "deduplicated": True}
    record = BusinessScoreRecord(
        record_id=str(uuid.uuid4()), project_id=str(body.project_id),
        room_id=str(body.room_id), phase_id=body.phase_id, payload=payload, content_sha256=digest,
        actor=_actor(request), created_at=datetime.now(timezone.utc))
    session.add(record)
    session.commit()
    session.refresh(record)
    return {**_serialize(record), "deduplicated": False}


@router.get("/scores")
def list_scores(room_id: UUID, limit: int = 20, session: Session = Depends(get_session)):
    rows = (session.query(BusinessScoreRecord).filter_by(room_id=str(room_id))
            .order_by(BusinessScoreRecord.created_at.desc()).limit(min(max(limit, 1), 100)).all())
    return {"items": [_serialize(row) for row in rows], "total": len(rows)}


@router.get("/scores/{record_id}/verify")
def verify_score(record_id: UUID, session: Session = Depends(get_session)):
    record = session.get(BusinessScoreRecord, str(record_id))
    if record is None:
        raise HTTPException(404, "评分记录不存在")
    actual = canonical_sha256(record.payload)
    return {"record_id": record.record_id, "expected": record.content_sha256, "actual": actual,
            "verified": actual == record.content_sha256}


@router.get("/scores/{record_id}")
def get_score(record_id: UUID, session: Session = Depends(get_session)):
    record = session.get(BusinessScoreRecord, str(record_id))
    if record is None:
        raise HTTPException(404, "评分记录不存在")
    return _serialize(record)
