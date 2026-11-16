"""敏感词库管理：转写完成后按词库自动生成 sensitive_word 事件（词库可配置）。"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.db import get_session
from app.models import SensitiveLexicon
router = APIRouter(prefix="/sensitive-lexicon", tags=["sensitive-lexicon"])


def _admin(request: Request):
    admin = getattr(request.state, 'admin', None)
    if not isinstance(admin, dict) or admin.get('role') != 'admin' or not admin.get('username'):
        raise HTTPException(401, '请先登录管理员账号')
    return admin['username']


class LexiconItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    word: str = Field(min_length=1, max_length=64)
    risk_level: str = Field(default="medium", pattern="^(high|medium|low)$")
    enabled: bool = True
    note: str | None = Field(default=None, max_length=256)


class LexiconBulkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[LexiconItemIn] = Field(min_length=1, max_length=200)


def _row(item: SensitiveLexicon) -> dict:
    return {"word": item.word, "risk_level": item.risk_level, "enabled": item.enabled, "note": item.note, "category": item.category}


@router.get("")
def list_lexicon(db = Depends(get_session), _admin: str = Depends(_admin)):
    rows = db.scalars(select(SensitiveLexicon).order_by(SensitiveLexicon.word)).all()
    return {"items": [_row(r) for r in rows], "total": len(rows)}


@router.put("")
def replace_lexicon(body: LexiconBulkIn, db = Depends(get_session), _admin: str = Depends(_admin)):
    seen = set()
    for item in body.items:
        if item.word in seen:
            raise HTTPException(422, f"词重复：{item.word}")
        seen.add(item.word)
    db.query(SensitiveLexicon).delete()
    for item in body.items:
        db.add(SensitiveLexicon(word=item.word, risk_level=item.risk_level, enabled=item.enabled, note=item.note))
    db.commit()
    rows = db.scalars(select(SensitiveLexicon).order_by(SensitiveLexicon.word)).all()
    return {"items": [_row(r) for r in rows], "total": len(rows)}


@router.post("")
def add_word(body: LexiconItemIn, db = Depends(get_session), _admin: str = Depends(_admin)):
    exists = db.get(SensitiveLexicon, body.word)
    if exists:
        exists.risk_level = body.risk_level
        exists.enabled = body.enabled
        exists.note = body.note
    else:
        db.add(SensitiveLexicon(word=body.word, risk_level=body.risk_level, enabled=body.enabled, note=body.note))
    db.commit()
    row = db.get(SensitiveLexicon, body.word)
    return _row(row)
