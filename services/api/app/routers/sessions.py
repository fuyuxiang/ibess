import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import desc
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DbSession

from app.auth import AuthRoute
from app.db import get_session as get_db
from app.models import EvalSession, Person, Seat, SensitiveWord, Skill, Zone
from app import phase_config as phases
from app import phase_control as controls

router = APIRouter(route_class=AuthRoute)
BusinessId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=36)]
SourceId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1024)]


class SessionIn(BaseModel):
    name: str
    current_phase: str = "review"
    expected_people: int = 0


class BindingIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_id: BusinessId
    room_id: BusinessId
    source_id: SourceId


class PhaseIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    phase: str = Field(min_length=1, max_length=32)
    expected_version: int = Field(ge=0, strict=True)
    enabled_scenes: list[str]
    rule_versions: dict[str, str]
    reason: Reason
    effective_at: datetime | None = None
    assets: list[dict] = Field(default_factory=list)
    exceptions: list[dict] = Field(default_factory=list)


def _admin(request: Request):
    admin = getattr(request.state, 'admin', None)
    if not isinstance(admin, dict) or admin.get('role') != 'admin' or not admin.get('username'):
        raise HTTPException(401, '请先登录管理员账号')
    return admin['username']


def _required_consumers(request: Request):
    # 仅从服务端接线配置读取，不接受浏览器提交或默认缩减消费者集合。
    configured = getattr(request.app.state, 'phase_control_required_consumers', None)
    if (not isinstance(configured, list) or not configured
            or any(not isinstance(value, str) or not value.strip() or len(value) > 128 for value in configured)
            or len(set(configured)) != len(configured)):
        raise HTTPException(503, {'code':'CONTROL_CONSUMERS_NOT_CONFIGURED',
            'message':'阶段控制必需消费者尚未配置'}, headers={'Cache-Control':'no-store','Retry-After':'3'})
    return sorted(configured)


def _phase_failure(exc):
    if isinstance(exc, phases.PhaseConfigError):
        code = exc.code
        status = 404 if code.endswith('_NOT_FOUND') else 409 if code in {
            'BINDING_REQUIRED', 'BINDING_CONFLICT', 'BINDING_INACTIVE', 'SESSION_CLOSED',
            'PROJECT_CLOSED', 'ROOM_DISABLED', 'VERSION_CONFLICT', 'TIME_CONFLICT',
        } else 422
        raise HTTPException(status, {'code': code, 'message': str(exc)}, headers={'Cache-Control': 'no-store'}) from None
    raise HTTPException(503, {'code': 'PHASE_STORAGE_UNAVAILABLE', 'message': '阶段存储暂不可用，请重试'},
        headers={'Cache-Control':'no-store','Retry-After':'3'}) from None


def _save_phase(session_id, body, actor, required_consumers, db):
    try:
        result = phases.append_snapshot(db, session_id=session_id, phase_id=body.phase,
            expected_version=body.expected_version, enabled_scenes=body.enabled_scenes,
            rule_versions=body.rule_versions, reason=body.reason, actor_id=actor,
            effective_at=body.effective_at, assets=body.assets, exceptions=body.exceptions)
        controls.enqueue_snapshot(db, result['stage_snapshot_id'], required_consumers=required_consumers)
        # 快照、outbox和旧场次展示字段同事务保存；网络发布交后台执行。
        current = _find_session(session_id, db)
        current.current_phase = result['phase_id']
        db.commit()
        return result
    except (phases.PhaseConfigError, SQLAlchemyError) as exc:
        db.rollback()
        _phase_failure(exc)
    except controls.PhaseControlError:
        db.rollback()
        raise HTTPException(503, {'code':'CONTROL_ENQUEUE_FAILED','message':'阶段控制入队失败，请检查服务配置'},
            headers={'Cache-Control':'no-store','Retry-After':'3'}) from None


def _session_dict(s: EvalSession) -> dict:
    return {
        "session_id": s.session_id,
        "name": s.name,
        "status": s.status,
        "current_phase": s.current_phase,
        "expected_people": s.expected_people,
        "start_at": s.start_at.isoformat() if s.start_at else None,
        "end_at": s.end_at.isoformat() if s.end_at else None,
    }


def _find_session(session_id: str, db: DbSession, *, lock: bool = False) -> EvalSession:
    query = db.query(EvalSession).filter(EvalSession.session_id == session_id)
    if lock:
        query = query.populate_existing().with_for_update()
    s = query.first()
    if not s:
        raise HTTPException(404, "场次不存在")
    return s


@router.get("/sessions")
def list_sessions(db: DbSession = Depends(get_db)):
    return [_session_dict(s) for s in db.query(EvalSession).order_by(desc(EvalSession.id)).all()]


@router.post("/sessions")
def create_session(body: SessionIn, db: DbSession = Depends(get_db)):
    s = EvalSession(
        session_id=str(uuid.uuid4()),
        name=body.name,
        current_phase=body.current_phase,
        expected_people=body.expected_people,
        status="draft",
    )
    db.add(s)
    db.commit()
    return _session_dict(s)


@router.get("/sessions/{session_id}")
def get_session_detail(session_id: str, db: DbSession = Depends(get_db)):
    return _session_dict(_find_session(session_id, db))


@router.put('/sessions/{session_id}/binding')
def bind_session(session_id: str, body: BindingIn, response: Response,
                 actor: str = Depends(_admin), db: DbSession = Depends(get_db)):
    response.headers['Cache-Control'] = 'no-store'
    try:
        result = phases.bind_session(db, session_id=session_id, **body.model_dump())
        db.commit()
        return result
    except (phases.PhaseConfigError, SQLAlchemyError) as exc:
        db.rollback()
        _phase_failure(exc)


@router.put('/sessions/{session_id}/phase-config', status_code=201)
def save_phase_config(session_id: str, body: PhaseIn, response: Response,
                      actor: str = Depends(_admin), required_consumers: list[str] = Depends(_required_consumers),
                      db: DbSession = Depends(get_db)):
    response.headers['Cache-Control'] = 'no-store'
    return _save_phase(session_id, body, actor, required_consumers, db)


@router.patch("/sessions/{session_id}/phase", status_code=201)
def update_phase(session_id: str, body: PhaseIn, response: Response,
                 actor: str = Depends(_admin), required_consumers: list[str] = Depends(_required_consumers),
                 db: DbSession = Depends(get_db)):
    response.headers['Cache-Control'] = 'no-store'
    return _save_phase(session_id, body, actor, required_consumers, db)


@router.get("/sessions/{session_id}/phase-config")
def get_phase_config(session_id: str, response: Response, actor: str = Depends(_admin),
                     db: DbSession = Depends(get_db)):
    response.headers['Cache-Control'] = 'no-store'
    try:
        result = phases.get_latest_config(db, session_id)
    except (phases.PhaseConfigError, SQLAlchemyError) as exc:
        _phase_failure(exc)
    if result is None:
        raise HTTPException(409, {'code':'PHASE_NOT_CONFIGURED',
            'message':'场次未配置阶段，请明确业务绑定并选择初始阶段'}, headers={'Cache-Control':'no-store'})
    return result


@router.get('/sessions/{session_id}/timeline')
def get_timeline(session_id: str, response: Response, actor: str = Depends(_admin),
                 db: DbSession = Depends(get_db)):
    response.headers['Cache-Control'] = 'no-store'
    try:
        return {'session_id': session_id, 'items': phases.list_timeline(db, session_id)}
    except (phases.PhaseConfigError, SQLAlchemyError) as exc:
        _phase_failure(exc)


@router.post("/sessions/{session_id}/start")
def start_session(session_id: str, actor: str = Depends(_admin), db: DbSession = Depends(get_db)):
    s = _find_session(session_id, db, lock=True)
    if s.status == 'closed':
        db.rollback()
        raise HTTPException(409, {'code':'SESSION_CLOSED','message':'已关闭场次不能重新打开'})
    if s.status == 'running':
        return _session_dict(s)
    s.status = "running"
    s.start_at = datetime.utcnow()
    db.commit()
    return _session_dict(s)


@router.post("/sessions/{session_id}/stop")
def stop_session(session_id: str, actor: str = Depends(_admin), db: DbSession = Depends(get_db)):
    s = _find_session(session_id, db, lock=True)
    if s.status == 'closed':
        return _session_dict(s)
    s.status = "closed"
    s.end_at = datetime.utcnow()
    db.commit()
    return _session_dict(s)


@router.get("/sessions/{session_id}/context")
def session_context(session_id: str, db: DbSession = Depends(get_db)):
    """场次上下文：人员、工位、区域、技能、敏感词（供规则引擎关联）"""
    s = _find_session(session_id, db)
    persons = db.query(Person).all()
    seats = db.query(Seat).filter(Seat.session_id == session_id).all()
    zones = db.query(Zone).filter(Zone.session_id == session_id).all()
    skills = db.query(Skill).filter(Skill.enabled == True).all()
    words = db.query(SensitiveWord).filter(SensitiveWord.enabled == True).all()
    return {
        "session": _session_dict(s),
        "persons": [
            {"person_id": p.person_id, "name": p.name, "role": p.role} for p in persons
        ],
        "seats": [
            {"seat_id": st.seat_id, "code": st.code, "person_id": st.person_id, "zone_id": st.zone_id}
            for st in seats
        ],
        "zones": [
            {"zone_id": z.zone_id, "name": z.name, "type": z.type, "polygon": z.polygon}
            for z in zones
        ],
        "skills": [
            {"code": k.code, "version": k.version, "parameters": k.parameters} for k in skills
        ],
        "sensitive_words": [w.word for w in words],
    }
