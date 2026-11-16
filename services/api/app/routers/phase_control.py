"""2.2控制状态与内部消费者端点。路由使用绝对路径，注册时不加prefix。"""
import hashlib
import hmac
import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DbSession

from app import phase_control as controls
from app.auth import AuthRoute
from app.db import get_session

router = APIRouter(route_class=AuthRoute)
Identifier = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256, pattern=r'\S')]
UTCString = Annotated[str, StringConstraints(strict=True,
    pattern=r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$')]


class ErrorIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str = Field(strict=True, min_length=1, max_length=64)
    message: str = Field(strict=True, min_length=1, max_length=512)


class AckIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    contract_version: Literal['2.0.0']
    project_id: Identifier
    room_id: Identifier
    session_id: Identifier
    source_id: Identifier
    stage_snapshot_id: Identifier
    phase_id: Identifier
    phase_version: int = Field(strict=True, ge=1)
    consumer_id: Identifier
    received_at: UTCString
    applied_at: UTCString | None
    status: Literal['applied', 'missing_asset', 'failed']
    error: ErrorIn | None


def _admin(request: Request):
    admin = getattr(request.state, 'admin', None)
    if not isinstance(admin, dict) or admin.get('role') != 'admin' or not admin.get('username'):
        raise HTTPException(401, '请先登录管理员账号', headers={'Cache-Control':'no-store'})
    return admin['username']


def _fail(exc):
    if isinstance(exc, controls.PhaseControlError):
        status = 404 if exc.code in {'SESSION_NOT_FOUND','SNAPSHOT_NOT_FOUND'} else 409 if exc.code in {
            'CONTROL_NOT_ENQUEUED','CONTROL_NOT_FOUND','PHASE_NOT_CONFIGURED','ACK_SCOPE_MISMATCH',
            'ACK_CONFLICT','CONTROL_CONFLICT','CONSUMER_NOT_REQUIRED'} else 422
        raise HTTPException(status, {'code':exc.code, 'message':str(exc)}, headers={'Cache-Control':'no-store'}) from None
    raise HTTPException(503, {'code':'CONTROL_STORAGE_UNAVAILABLE','message':'控制存储暂不可用，请重试'},
        headers={'Cache-Control':'no-store','Retry-After':'3'}) from None


def _consumer(request: Request, session_id: str):
    """使用应用接线时注入的token摘要和明确作用域；没有配置时默认拒绝。"""
    mapping = getattr(request.app.state, 'phase_control_consumers', None)
    if not isinstance(mapping, dict) or not mapping:
        raise HTTPException(503, {'code':'CONSUMER_AUTH_NOT_CONFIGURED','message':'消费者认证尚未配置'},
            headers={'Cache-Control':'no-store','Retry-After':'3'})
    # 服务端Bearer端点不接受浏览器跨域Origin或URL内凭据。
    if request.headers.get('origin') is not None:
        raise HTTPException(403, '内部消费者端点不接受浏览器来源', headers={'Cache-Control':'no-store'})
    authorization = request.headers.get('authorization', '')
    parts = authorization.split(' ')
    if len(parts) != 2 or parts[0] != 'Bearer' or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', parts[1]):
        raise HTTPException(401, '消费者凭据无效', headers={'Cache-Control':'no-store','WWW-Authenticate':'Bearer'})
    digest = hashlib.sha256(parts[1].encode('ascii')).hexdigest()
    matches = []
    for consumer_id, spec in mapping.items():
        if not isinstance(consumer_id, str) or not isinstance(spec, dict):
            continue
        expected = spec.get('token_sha256')
        if isinstance(expected, str) and re.fullmatch(r'[a-f0-9]{64}', expected) and hmac.compare_digest(expected, digest):
            matches.append((consumer_id, spec))
    if len(matches) != 1:
        raise HTTPException(401, '消费者凭据无效', headers={'Cache-Control':'no-store','WWW-Authenticate':'Bearer'})
    consumer_id, spec = matches[0]
    scopes = spec.get('scopes')
    if not isinstance(scopes, list):
        raise HTTPException(503, '消费者作用域未配置', headers={'Cache-Control':'no-store'})
    allowed = [scope for scope in scopes if isinstance(scope, dict)
        and all(isinstance(scope.get(k), str) and scope[k].strip() for k in ('project_id','room_id','session_id','source_id'))
        and scope['session_id'] == session_id]
    if not allowed:
        raise HTTPException(403, '消费者无权访问该场次', headers={'Cache-Control':'no-store'})
    return {'consumer_id':consumer_id, 'scopes':allowed}


def _check_scope(identity, value):
    if not any(all(value[k] == scope[k] for k in ('project_id','room_id','session_id','source_id'))
               for scope in identity['scopes']):
        raise HTTPException(403, '消费者无权访问该项目、评标室或媒体源', headers={'Cache-Control':'no-store'})


@router.get('/api/sessions/{session_id}/control-status')
def control_status(session_id: str, response: Response, actor: str = Depends(_admin),
                   db: DbSession = Depends(get_session)):
    response.headers['Cache-Control'] = 'no-store'
    try:
        return controls.get_control_status(db, session_id)
    except (controls.PhaseControlError, SQLAlchemyError) as exc:
        _fail(exc)


@router.get('/internal/phase-control/sessions/{session_id}/config')
def consumer_config(session_id: str, response: Response, identity: dict = Depends(_consumer),
                    db: DbSession = Depends(get_session)):
    response.headers['Cache-Control'] = 'no-store'
    try:
        value = controls.read_latest_config(db, session_id)
    except (controls.PhaseControlError, SQLAlchemyError) as exc:
        _fail(exc)
    _check_scope(identity, value)
    return value


@router.put('/internal/phase-control/sessions/{session_id}/acks/{consumer_id}')
def consumer_ack(session_id: str, consumer_id: str, body: AckIn, response: Response,
                 identity: dict = Depends(_consumer), db: DbSession = Depends(get_session)):
    response.headers['Cache-Control'] = 'no-store'
    if identity['consumer_id'] != consumer_id or body.consumer_id != consumer_id:
        raise HTTPException(403, '不得提交其他消费者的确认', headers={'Cache-Control':'no-store'})
    if body.session_id != session_id:
        raise HTTPException(403, '确认场次与路径不一致', headers={'Cache-Control':'no-store'})
    value = body.model_dump()
    _check_scope(identity, value)
    try:
        result = controls.record_ack(db, value)
        db.commit()
        return result
    except (controls.PhaseControlError, SQLAlchemyError) as exc:
        db.rollback()
        _fail(exc)
