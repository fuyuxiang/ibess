import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session as DbSession

from app.db import get_session
from app.models import Operator, OperatorBinding, Person, Seat, SensitiveWord, Skill, Zone
from app.core.operator_parameters import PARAMETERS, validate_parameters

router = APIRouter()


# ---------- 专家 ----------
class PersonIn(BaseModel):
    name: str
    role: str = "expert"
    external_id: str | None = None


def _person_dict(p: Person) -> dict:
    return {"person_id": p.person_id, "name": p.name, "role": p.role, "external_id": p.external_id}


@router.get("/persons")
def list_persons(db: DbSession = Depends(get_session)):
    return [_person_dict(p) for p in db.query(Person).order_by(desc(Person.id)).all()]


@router.post("/persons")
def create_person(body: PersonIn, db: DbSession = Depends(get_session)):
    p = Person(person_id=str(uuid.uuid4()), **body.model_dump())
    db.add(p)
    db.commit()
    return _person_dict(p)


@router.delete("/persons/{person_id}")
def delete_person(person_id: str, db: DbSession = Depends(get_session)):
    p = db.query(Person).filter(Person.person_id == person_id).first()
    if not p:
        raise HTTPException(404, "专家不存在")
    db.delete(p)
    db.commit()
    return {"ok": True}


# ---------- 工位 ----------
class SeatIn(BaseModel):
    session_id: str
    code: str
    person_id: str | None = None
    zone_id: str | None = None


def _seat_dict(st: Seat) -> dict:
    return {
        "seat_id": st.seat_id,
        "session_id": st.session_id,
        "code": st.code,
        "person_id": st.person_id,
        "zone_id": st.zone_id,
    }


@router.get("/seats")
def list_seats(session_id: str | None = None, db: DbSession = Depends(get_session)):
    q = db.query(Seat)
    if session_id:
        q = q.filter(Seat.session_id == session_id)
    return [_seat_dict(st) for st in q.order_by(desc(Seat.id)).all()]


@router.post("/seats")
def create_seat(body: SeatIn, db: DbSession = Depends(get_session)):
    st = Seat(seat_id=str(uuid.uuid4()), **body.model_dump())
    db.add(st)
    db.commit()
    return _seat_dict(st)


@router.delete("/seats/{seat_id}")
def delete_seat(seat_id: str, db: DbSession = Depends(get_session)):
    st = db.query(Seat).filter(Seat.seat_id == seat_id).first()
    if not st:
        raise HTTPException(404, "工位不存在")
    db.delete(st)
    db.commit()
    return {"ok": True}


# ---------- 区域 ----------
class ZoneIn(BaseModel):
    session_id: str
    name: str
    type: str = "restricted"   # seat / entry / restricted
    polygon: list | None = None
    camera_id: str | None = None


def _zone_dict(z: Zone) -> dict:
    return {
        "zone_id": z.zone_id,
        "session_id": z.session_id,
        "name": z.name,
        "type": z.type,
        "polygon": z.polygon,
        "camera_id": z.camera_id,
    }


@router.get("/zones")
def list_zones(session_id: str | None = None, db: DbSession = Depends(get_session)):
    q = db.query(Zone)
    if session_id:
        q = q.filter(Zone.session_id == session_id)
    return [_zone_dict(z) for z in q.order_by(desc(Zone.id)).all()]


@router.post("/zones")
def create_zone(body: ZoneIn, db: DbSession = Depends(get_session)):
    z = Zone(zone_id=str(uuid.uuid4()), **body.model_dump())
    db.add(z)
    db.commit()
    return _zone_dict(z)


@router.delete("/zones/{zone_id}")
def delete_zone(zone_id: str, db: DbSession = Depends(get_session)):
    z = db.query(Zone).filter(Zone.zone_id == zone_id).first()
    if not z:
        raise HTTPException(404, "区域不存在")
    db.delete(z)
    db.commit()
    return {"ok": True}


# ---------- 技能 ----------
class SkillIn(BaseModel):
    code: str
    version: str = "v1"
    enabled: bool = True
    parameters: dict | None = None


def _skill_dict(k: Skill) -> dict:
    return {
        "skill_id": k.skill_id,
        "code": k.code,
        "version": k.version,
        "enabled": k.enabled,
        "parameters": k.parameters,
    }


@router.get("/skills")
def list_skills(db: DbSession = Depends(get_session)):
    return [_skill_dict(k) for k in db.query(Skill).order_by(desc(Skill.id)).all()]


@router.post("/skills")
def create_skill(body: SkillIn, db: DbSession = Depends(get_session)):
    k = Skill(skill_id=str(uuid.uuid4()), **body.model_dump())
    db.add(k)
    db.commit()
    return _skill_dict(k)


# ---------- 敏感词 ----------
class WordIn(BaseModel):
    word: str
    homophone: str | None = None
    enabled: bool = True


def _word_dict(w: SensitiveWord) -> dict:
    return {"id": w.id, "word": w.word, "homophone": w.homophone, "enabled": w.enabled}


@router.get("/words")
def list_words(db: DbSession = Depends(get_session)):
    return [_word_dict(w) for w in db.query(SensitiveWord).order_by(desc(SensitiveWord.id)).all()]


@router.post("/words")
def create_word(body: WordIn, db: DbSession = Depends(get_session)):
    w = SensitiveWord(**body.model_dump())
    db.add(w)
    db.commit()
    return _word_dict(w)


@router.delete("/words/{word_id}")
def delete_word(word_id: int, db: DbSession = Depends(get_session)):
    w = db.query(SensitiveWord).filter(SensitiveWord.id == word_id).first()
    if not w:
        raise HTTPException(404, "敏感词不存在")
    db.delete(w)
    db.commit()
    return {"ok": True}


# ---------- 算子 ----------
RISK_LEVELS = {"info", "low", "medium", "high"}


class OperatorIn(BaseModel):
    code: str
    name: str
    category: str = "rule"
    version: str = "v1"
    input_types: list[str] = []
    output_event_type: str
    risk_level: str = "low"
    parameters: dict = {}
    description: str | None = None


class OperatorPatch(BaseModel):
    expected_updated_at: str | None = None
    name: str | None = None
    version: str | None = None
    input_types: list[str] | None = None
    output_event_type: str | None = None
    risk_level: str | None = None
    parameters: dict | None = None
    description: str | None = None


class OperatorBindingIn(BaseModel):
    session_id: str | None = None
    camera_id: str | None = None
    zone_id: str | None = None
    parameters: dict = {}


def _operator_dict(operator: Operator) -> dict:
    return {
        "operator_id": operator.operator_id,
        "code": operator.code,
        "name": operator.name,
        "category": operator.category,
        "version": operator.version,
        "enabled": operator.enabled,
        "input_types": operator.input_types or [],
        "output_event_type": operator.output_event_type,
        "risk_level": operator.risk_level,
        "parameters": operator.parameters or {},
        "description": operator.description,
        "created_at": operator.created_at.isoformat() if operator.created_at else None,
        "updated_at": operator.updated_at.isoformat() if operator.updated_at else None,
        'supported': operator.code in PARAMETERS,
        'parameter_schema': PARAMETERS.get(operator.code, []),
    }


def _binding_dict(binding: OperatorBinding) -> dict:
    return {
        "binding_id": binding.binding_id,
        "operator_id": binding.operator_id,
        "session_id": binding.session_id,
        "camera_id": binding.camera_id,
        "zone_id": binding.zone_id,
        "enabled": binding.enabled,
        "parameters": binding.parameters or {},
        "created_at": binding.created_at.isoformat() if binding.created_at else None,
    }


def _validate_operator(body: OperatorIn | OperatorPatch):
    risk_level = body.risk_level
    if risk_level is not None and risk_level not in RISK_LEVELS:
        raise HTTPException(422, "risk_level 必须是 info、low、medium 或 high")


@router.get("/operators")
def list_operators(enabled: bool | None = None, db: DbSession = Depends(get_session)):
    query = db.query(Operator).order_by(desc(Operator.id))
    if enabled is not None:
        query = query.filter(Operator.enabled == enabled)
    from datetime import datetime, timedelta
    from app.supervision_models import OperatorApplication
    receipt = db.get(OperatorApplication, 'event-service')
    from app.supervision_models import SupervisionSignal
    from app.core.rule_catalog import CORE_RULES
    signals = db.query(SupervisionSignal).filter(SupervisionSignal.kind=='core', SupervisionSignal.observed_at>=datetime.utcnow()-timedelta(seconds=30)).all()
    def application(item):
        result = _operator_dict(item)
        entry = (receipt.configuration or {}).get(item.operator_id) if receipt else None
        result['application_status'] = ('unsupported' if not result['supported'] else
            'unknown' if not receipt or receipt.applied_at < datetime.utcnow()-timedelta(seconds=30) else
            'pending' if not entry or entry.get('updated_at') != result['updated_at'] or entry.get('enabled') != item.enabled else
            'error' if entry.get('error') else 'applied')
        result['application_error'] = entry.get('error') if entry else None
        if item.code in CORE_RULES or item.code in ('phone_call','phone_capture'):
            required = 'interactions' if item.code in ('whispering','document_transfer','phone_call','phone_capture') else 'entrance' if item.code=='unauthorized_entry' else 'spatial'
            bindings = db.query(OperatorBinding).filter_by(operator_id=item.operator_id, enabled=True).all()
            def matches(signal):
                from app.core.supervision_scope import normalize_source
                return not bindings or any((not b.session_id or b.session_id==signal.session_id) and
                    (not b.camera_id or normalize_source(b.camera_id)==normalize_source(signal.source_id)) for b in bindings)
            result['recognition_sources'] = [{'source_id':s.source_id, 'session_id':s.session_id,
                'status':(s.detail or {}).get('capabilities',{}).get(required,'unknown')} for s in signals if matches(s)]
        return result
    return [application(item) for item in query.all()]


@router.post("/operators", status_code=201)
def create_operator(body: OperatorIn, db: DbSession = Depends(get_session)):
    _validate_operator(body)
    validate_parameters(body.code, body.parameters)
    if db.query(Operator).filter(Operator.code == body.code).first():
        raise HTTPException(409, "算子编码已存在")
    operator = Operator(operator_id=str(uuid.uuid4()), enabled=False, **body.model_dump())
    db.add(operator)
    db.commit()
    db.refresh(operator)
    return _operator_dict(operator)


@router.get("/operators/{operator_id}")
def get_operator(operator_id: str, db: DbSession = Depends(get_session)):
    operator = db.query(Operator).filter(Operator.operator_id == operator_id).first()
    if not operator:
        raise HTTPException(404, "算子不存在")
    return {**_operator_dict(operator), "bindings": [_binding_dict(item) for item in db.query(OperatorBinding).filter(OperatorBinding.operator_id == operator_id).all()]}


@router.patch("/operators/{operator_id}")
def update_operator(operator_id: str, body: OperatorPatch, request: Request, db: DbSession = Depends(get_session)):
    _validate_operator(body)
    operator = db.query(Operator).filter(Operator.operator_id == operator_id).with_for_update().first()
    if not operator:
        raise HTTPException(404, "算子不存在")
    if body.expected_updated_at is not None and body.expected_updated_at != (operator.updated_at.isoformat() if operator.updated_at else None):
        raise HTTPException(409, '规则已被更新，请刷新后重新核对配置')
    changes = body.model_dump(exclude_unset=True, exclude={'expected_updated_at'})
    for key in ('name','version','output_event_type'):
        if key in changes and (not isinstance(changes[key], str) or not changes[key].strip() or len(changes[key]) > {'name':128,'version':32,'output_event_type':64}[key]):
            raise HTTPException(422, '规则名称、版本与事件类型必须是有效文本')
    if 'parameters' in changes and not isinstance(changes['parameters'],dict):
        raise HTTPException(422, '规则参数必须为JSON对象')
    parameters = changes.get('parameters', operator.parameters or {})
    validate_parameters(operator.code, parameters)
    for binding in db.query(OperatorBinding).filter_by(operator_id=operator_id, enabled=True):
        validate_parameters(operator.code, {**parameters, **(binding.parameters or {})})
    if 'input_types' in changes and not isinstance(changes['input_types'],list):
        raise HTTPException(422, '输入类型必须为列表')
    before = _operator_dict(operator)
    for key, value in changes.items():
        setattr(operator, key, value)
    db.flush()
    from app.supervision_models import OperatorAudit
    db.add(OperatorAudit(operator_id=operator_id, actor=request.state.admin['username'], action='configure', before=before, after=_operator_dict(operator)))
    db.commit()
    db.refresh(operator)
    return _operator_dict(operator)


def _set_operator_enabled(operator_id: str, enabled: bool, db: DbSession, actor: str, expected_updated_at: str | None = None):
    operator = db.query(Operator).filter(Operator.operator_id == operator_id).with_for_update().first()
    if not operator:
        raise HTTPException(404, "算子不存在")
    if expected_updated_at is not None and expected_updated_at != (operator.updated_at.isoformat() if operator.updated_at else None):
        raise HTTPException(409, '规则状态已变化，请刷新后操作')
    if enabled:
        if operator.code not in PARAMETERS:
            raise HTTPException(422, '当前检测服务未部署该规则，不能启用')
        validate_parameters(operator.code, operator.parameters or {})
        for binding in db.query(OperatorBinding).filter_by(operator_id=operator_id, enabled=True):
            validate_parameters(operator.code, {**(operator.parameters or {}), **(binding.parameters or {})})
    before = _operator_dict(operator)
    operator.enabled = enabled
    db.flush()
    from app.supervision_models import OperatorAudit
    db.add(OperatorAudit(operator_id=operator_id, actor=actor, action='enable' if enabled else 'disable', before=before, after=_operator_dict(operator)))
    db.commit()
    db.refresh(operator)
    return _operator_dict(operator)


@router.post("/operators/{operator_id}/enable")
def enable_operator(operator_id: str, request: Request, expected_updated_at: str | None = None, db: DbSession = Depends(get_session)):
    return _set_operator_enabled(operator_id, True, db, request.state.admin['username'], expected_updated_at)


@router.post("/operators/{operator_id}/disable")
def disable_operator(operator_id: str, request: Request, expected_updated_at: str | None = None, db: DbSession = Depends(get_session)):
    return _set_operator_enabled(operator_id, False, db, request.state.admin['username'], expected_updated_at)


@router.get('/operators/{operator_id}/audit')
def operator_audit(operator_id: str, db: DbSession = Depends(get_session)):
    from app.supervision_models import OperatorAudit
    from app.core.workflow import iso_utc
    rows = db.query(OperatorAudit).filter(OperatorAudit.operator_id == operator_id).order_by(OperatorAudit.id.desc()).limit(50).all()
    return [{'id':r.id,'actor':r.actor,'action':r.action,'before':r.before,'after':r.after,'at':iso_utc(r.at)} for r in rows]


@router.get("/operators/{operator_id}/bindings")
def list_operator_bindings(operator_id: str, db: DbSession = Depends(get_session)):
    return [_binding_dict(item) for item in db.query(OperatorBinding).filter(OperatorBinding.operator_id == operator_id).all()]


@router.post("/operators/{operator_id}/bindings", status_code=201)
def create_operator_binding(operator_id: str, body: OperatorBindingIn, request: Request, db: DbSession = Depends(get_session)):
    operator = db.query(Operator).filter(Operator.operator_id == operator_id).with_for_update().first()
    if not operator:
        raise HTTPException(404, "算子不存在")
    validate_parameters(operator.code, {**(operator.parameters or {}), **body.parameters})
    if not any((body.session_id, body.camera_id, body.zone_id)):
        raise HTTPException(422, "至少需要绑定 session_id、camera_id 或 zone_id")
    binding = OperatorBinding(binding_id=str(uuid.uuid4()), operator_id=operator_id, **body.model_dump())
    db.add(binding)
    # 作用域也是执行配置的一部分；变更后旧消费者回执必须失效。
    operator.updated_at = datetime.utcnow()
    db.flush()
    from app.supervision_models import OperatorAudit
    db.add(OperatorAudit(operator_id=operator_id, actor=request.state.admin['username'], action='bind', before={}, after=_binding_dict(binding)))
    db.commit()
    db.refresh(binding)
    return _binding_dict(binding)


@router.delete("/operators/{operator_id}/bindings/{binding_id}")
def delete_operator_binding(operator_id: str, binding_id: str, request: Request, db: DbSession = Depends(get_session)):
    operator = db.query(Operator).filter(Operator.operator_id == operator_id).with_for_update().first()
    if not operator:
        raise HTTPException(404, "算子不存在")
    binding = db.query(OperatorBinding).filter(OperatorBinding.operator_id == operator_id, OperatorBinding.binding_id == binding_id).first()
    if not binding:
        raise HTTPException(404, "算子绑定不存在")
    from app.supervision_models import OperatorAudit
    db.add(OperatorAudit(operator_id=operator_id, actor=request.state.admin['username'], action='unbind', before=_binding_dict(binding), after={}))
    db.delete(binding)
    operator.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}
