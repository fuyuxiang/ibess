"""执行卡 4.3：素材绑定接口（管理员 Cookie + CSRF，AuthRoute），10.1 以 prefix='/api' 挂载。"""
from __future__ import annotations

from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DbSession

from app import stage_media_bindings as service
from app.auth import AuthRoute
from app.db import get_session
from app.stage_media_binding_models import StageMediaBinding as MediaBinding

router = APIRouter(route_class=AuthRoute)
Identifier = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256, pattern=r"\S")]
UTCString = Annotated[str, StringConstraints(strict=True, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")]
STATUS = {"SESSION_NOT_FOUND": 404, "RECORDING_NOT_FOUND": 404, "ASSET_NOT_FOUND": 404, "BINDING_NOT_FOUND": 404,
          "SESSION_CLOSED": 409, "BINDING_REQUIRED": 409, "REQUEST_CONFLICT": 409, "BINDING_CONFLICT": 409}


class BindingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")]
    kind: Literal["recording", "media_run", "scoring"]
    subject_id: Identifier
    started_at: UTCString
    duration_ms: Optional[int] = Field(default=None, strict=True, ge=1, le=2_147_483_647)
    asset_id: Optional[Identifier] = None


def _admin(request: Request) -> str:
    admin = getattr(request.state, "admin", None)
    if not isinstance(admin, dict) or admin.get("role") != "admin" or not admin.get("username"):
        raise HTTPException(401, "请先登录管理员账号", headers={"Cache-Control": "no-store"})
    return admin["username"]


def _fail(exc: Exception):
    if isinstance(exc, service.MediaBindingError):
        raise HTTPException(STATUS.get(exc.code, 422), {"code": exc.code, "message": str(exc)}, headers={"Cache-Control": "no-store"}) from None
    raise HTTPException(503, {"code": "BINDING_STORAGE_UNAVAILABLE", "message": "绑定存储暂不可用，请重试"},
                        headers={"Cache-Control": "no-store", "Retry-After": "3"}) from None


@router.post("/sessions/{session_id}/media-bindings", status_code=201)
def create_binding(session_id: str, body: BindingIn, response: Response, actor: str = Depends(_admin), db: DbSession = Depends(get_session)):
    response.headers["Cache-Control"] = "no-store"
    try:
        value, created = service.bind(db, session_id=session_id, request_id=body.request_id, kind=body.kind, subject_id=body.subject_id,
                                      started_at=body.started_at, duration_ms=body.duration_ms, asset_id=body.asset_id, actor=actor)
        db.commit()
    except (service.MediaBindingError, SQLAlchemyError) as exc:
        db.rollback()
        _fail(exc)
    if not created:
        response.status_code = 200
    return value


@router.get("/sessions/{session_id}/media-bindings")
def list_bindings(session_id: str, response: Response, actor: str = Depends(_admin), db: DbSession = Depends(get_session)):
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"session_id": session_id, "bindings": service.list_bindings(db, session_id)}
    except (service.MediaBindingError, SQLAlchemyError) as exc:
        _fail(exc)



@router.get("/media-bindings/{binding_id}/locate")
def locate_binding(binding_id: str, response: Response, media_ms: Optional[int] = Query(default=None, ge=0),
                   at: Optional[str] = Query(default=None), actor: str = Depends(_admin), db: DbSession = Depends(get_session)):
    response.headers["Cache-Control"] = "no-store"
    try:
        row = db.scalar(select(MediaBinding).where(MediaBinding.binding_id == binding_id))
        if row is None:
            raise service.MediaBindingError("BINDING_NOT_FOUND", "绑定不存在")
        return service.locate(row, media_ms=media_ms, at=at)
    except (service.MediaBindingError, SQLAlchemyError) as exc:
        _fail(exc)


@router.get("/media-bindings/recordings/{recording_id}")
def recording_context(recording_id: str, response: Response, actor: str = Depends(_admin), db: DbSession = Depends(get_session)):
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.recording_context(db, recording_id)
    except (service.MediaBindingError, SQLAlchemyError) as exc:
        _fail(exc)
