"""S04业务事实登记与查询 API（6.1）。

事实类型：check_in=到场签到；check_out=离场；change=换人；
leave_permit=离岗许可；clarification=澄清事实。
写请求沿用管理员认证/CSRF中间件，登记人取登录身份。
"""
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator, model_validator
from sqlalchemy.orm import Session, joinedload

from app.business_facts_models import BusinessAttendanceFact
from app.business_models import BusinessParticipant, BusinessPerson, BusinessProject, BusinessRoom, BusinessSeat
from app.db import get_session

router = APIRouter()
Note = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
PhaseId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
FACT_TYPES = ("check_in", "check_out", "change", "leave_permit", "clarification")


class FactIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fact_type: Literal["check_in", "check_out", "change", "leave_permit", "clarification"]
    project_id: UUID
    room_id: UUID
    session_id: UUID | None = None
    person_id: UUID | None = None
    seat_id: UUID | None = None
    phase_id: PhaseId | None = None
    occurred_at: datetime | None = None
    note: Note | None = None
    payload: dict | None = None

    @field_validator("occurred_at")
    @classmethod
    def normalize_time(cls, value):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value):
        if value is None:
            return None
        unknown = set(value) - {"replaced_person_id", "question", "answer", "permit_scope", "permit_until", "supplement", "authorization_kind", "valid_until"}
        if unknown:
            raise ValueError(f"事实负载包含未声明字段：{'、'.join(sorted(unknown))}")
        if any(item is not None and not isinstance(item, str) for item in value.values()):
            raise ValueError("事实负载字段必须为字符串")
        return value


    @model_validator(mode='after')
    def validate_authorization(self):
        payload=self.payload or {}
        if 'authorization_kind' in payload or 'valid_until' in payload:
            if self.fact_type!='clarification' or payload.get('authorization_kind') not in ('contact_allowed','document_access','photo_allowed','device_allowed','break'):
                raise ValueError('临时许可类型无效，且只能登记为澄清事实')
            try:
                until=datetime.fromisoformat(payload['valid_until'].replace('Z','+00:00'))
                if until.tzinfo is None or until<= (self.occurred_at or datetime.now(timezone.utc)):
                    raise ValueError
            except (KeyError,ValueError,TypeError):
                raise ValueError('临时许可必须有晚于发生时间且带时区的有效期') from None
        return self


def _actor(request: Request):
    admin = getattr(request.state, "admin", None)
    if not isinstance(admin, dict) or not admin.get("username"):
        raise HTTPException(403, "管理员身份缺失，无法登记业务事实")
    return admin["username"]


def _find(session, model, record_id, message):
    record = session.get(model, str(record_id))
    if record is None:
        raise HTTPException(422, message)
    return record


def _serialize(fact: BusinessAttendanceFact, session: Session) -> dict:
    result = {}
    for column in fact.__table__.columns:
        value = getattr(fact, column.name)
        if column.name in ("occurred_at", "created_at") and value is not None:
            value = value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        result[column.name] = value
    result["person_name"] = fact.person.name if fact.person else None
    result["seat_code"] = fact.seat.code if fact.seat else None
    result["room_name"] = fact.room.name if fact.room else None
    result["replaced_person_name"] = None
    if fact.fact_type == "change" and fact.payload and fact.payload.get("replaced_person_id"):
        replaced = session.get(BusinessPerson, fact.payload["replaced_person_id"])
        result["replaced_person_name"] = replaced.name if replaced else None
    return result


def save_fact(body: FactIn, session: Session, actor: str, *, commit=True) -> dict:
    if body.session_id:
        from app.models import EvalSession
        session.query(EvalSession).filter_by(session_id=str(body.session_id)).with_for_update().first()
    room = session.query(BusinessRoom).filter_by(room_id=str(body.room_id)).with_for_update().first()
    if room is None:
        raise HTTPException(422, "评标室不存在")
    if str(room.project_id) != str(body.project_id):
        raise HTTPException(422, "事实所属项目与评标室不一致")
    if body.session_id:
        from app.routers.rosters import session_scope
        session_scope(session, room, body.session_id, writing=True)
    _find(session, BusinessProject, body.project_id, "项目不存在")
    person = None
    if body.person_id is not None:
        person = _find(session, BusinessPerson, body.person_id, "人员不存在")
        if not session.query(BusinessParticipant).filter_by(project_id=str(body.project_id), person_id=str(body.person_id)).first():
            raise HTTPException(422, "人员必须先加入所属项目（成员配置）")
    seat = None
    if body.seat_id is not None:
        seat = _find(session, BusinessSeat, body.seat_id, "工位不存在")
        if str(seat.room_id) != str(body.room_id):
            raise HTTPException(422, "工位不属于该评标室")
    payload = body.payload
    replaced_name = None
    if body.fact_type == "change":
        replaced_id = (payload or {}).get("replaced_person_id")
        if not replaced_id:
            raise HTTPException(422, "换人事实必须提供被替换人员（replaced_person_id）")
        if body.person_id is None or replaced_id == str(body.person_id):
            raise HTTPException(422, "换人事实的新旧人员不能相同或缺失新人员")
        replaced = session.get(BusinessPerson, replaced_id)
        if replaced is None or not session.query(BusinessParticipant).filter_by(project_id=str(body.project_id), person_id=replaced.person_id).first():
            raise HTTPException(422, "被替换人员必须已加入所属项目（成员配置）")
        replaced_name = replaced.name
    elif body.fact_type in ("leave_permit", "clarification") and not body.note:
        raise HTTPException(422, "离岗许可与澄清事实必须填写说明")
    fact = BusinessAttendanceFact(
        fact_id=str(uuid4()), project_id=str(body.project_id), room_id=str(body.room_id),
        fact_type=body.fact_type, person_id=str(body.person_id) if body.person_id else None,
        seat_id=str(body.seat_id) if body.seat_id else None, phase_id=body.phase_id,
        occurred_at=body.occurred_at or datetime.now(timezone.utc), note=body.note,
        payload=payload, actor=actor, created_at=datetime.now(timezone.utc))
    session.add(fact)
    if commit:session.commit()
    else:session.flush()
    session.refresh(fact)
    result = _serialize(fact, session)
    result["replaced_person_name"] = replaced_name
    return result


@router.post("/facts", status_code=201)
def create_fact(body: FactIn, request: Request, session: Session = Depends(get_session)):
    return save_fact(body, session, _actor(request))


@router.get("/facts")
def list_facts(project_id: UUID, room_id: UUID | None = None, person_id: UUID | None = None,
               fact_type: str | None = None, limit: int = 50, session: Session = Depends(get_session)):
    query = (session.query(BusinessAttendanceFact)
             .options(joinedload(BusinessAttendanceFact.person), joinedload(BusinessAttendanceFact.seat))
             .filter(BusinessAttendanceFact.project_id == str(project_id)))
    if room_id is not None:
        query = query.filter(BusinessAttendanceFact.room_id == str(room_id))
    if person_id is not None:
        query = query.filter(BusinessAttendanceFact.person_id == str(person_id))
    if fact_type:
        if fact_type not in FACT_TYPES:
            raise HTTPException(422, "事实类型不受支持")
        query = query.filter(BusinessAttendanceFact.fact_type == fact_type)
    records = query.order_by(BusinessAttendanceFact.occurred_at.desc(), BusinessAttendanceFact.fact_id).limit(min(max(limit, 1), 200)).all()
    return {"items": [_serialize(row, session) for row in records], "total": len(records)}


@router.get("/facts/{fact_id}")
def get_fact(fact_id: UUID, session: Session = Depends(get_session)):
    fact = session.get(BusinessAttendanceFact, str(fact_id))
    if fact is None:
        raise HTTPException(404, "业务事实不存在")
    return _serialize(fact, session)


@router.delete("/facts/{fact_id}")
def delete_fact(fact_id: UUID, session_id: UUID | None = None, session: Session = Depends(get_session)):
    if session_id:
        from app.models import EvalSession
        session.query(EvalSession).filter_by(session_id=str(session_id)).with_for_update().first()
    fact = session.get(BusinessAttendanceFact, str(fact_id))
    if fact is None:
        raise HTTPException(404, "业务事实不存在")
    room = session.query(BusinessRoom).filter_by(room_id=fact.room_id).with_for_update().first()
    if session_id:
        from app.routers.rosters import session_scope
        session_scope(session, room, session_id, writing=True)
    session.delete(fact)
    session.commit()
    return {"ok": True}
