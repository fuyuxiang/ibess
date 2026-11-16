"""项目、评标室、人员、工位与项目成员配置 API。"""
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator
from sqlalchemy.orm import Session

from app.business_models import BusinessEventContext, BusinessParticipant, BusinessPerson, BusinessProject, BusinessRoom, BusinessSeat
from app.core.business_registry import commit_registry, entity_dict, lock_registry
from app.db import get_session

router = APIRouter()
Code = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Location = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
Role = Literal["expert", "supervisor", "staff", "owner", "agent", "bidder"]


class RegistryInput(BaseModel):
    model_config = ConfigDict(extra="ignore")  # 返回的只读 ID 可用于编辑回传，不能改变主键。


class ProjectIn(RegistryInput):
    code: Code
    name: Name
    owner: Name
    start_at: datetime | None = None
    end_at: datetime | None = None
    status: Literal["draft", "running", "closed"] = "draft"

    @model_validator(mode="after")
    def valid_dates(self):
        for key in ("start_at", "end_at"):
            value = getattr(self, key)
            if value is not None:
                setattr(self, key, value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc))
        if self.start_at and self.end_at and self.start_at > self.end_at:
            raise ValueError("开始时间不能晚于结束时间")
        return self


class RoomIn(RegistryInput):
    project_id: UUID
    code: Code
    name: Name
    location: Location
    mode: Literal["centralized", "remote"] = "centralized"
    enabled: bool = True


class PersonIn(RegistryInput):
    code: Code
    name: Name
    unit: Name
    role: Role
    enabled: bool = True


class SeatIn(RegistryInput):
    room_id: UUID
    code: Code
    person_id: UUID | None = None
    enabled: bool = True


class ParticipantIn(RegistryInput):
    project_id: UUID
    person_id: UUID
    role: Role


ENTITIES = {
    "projects": (BusinessProject, "project_id"), "rooms": (BusinessRoom, "room_id"),
    "persons": (BusinessPerson, "person_id"), "seats": (BusinessSeat, "seat_id"),
    "participants": (BusinessParticipant, "membership_id"),
}


def find(session, model, key, association=False):
    record = session.get(model, str(key))
    if record is None:
        raise HTTPException(422 if association else 404, "关联配置不存在" if association else "配置不存在")
    return record


def exists(session, model, *conditions):
    return session.query(model).filter(*conditions).first() is not None


def membership_used(session, project_id, person_id):
    return exists(session, BusinessSeat, BusinessSeat.project_id == project_id, BusinessSeat.person_id == person_id) or exists(session, BusinessEventContext, BusinessEventContext.project_id == project_id, BusinessEventContext.person_id == person_id)


def save(entity, body, session, record_id=None):
    lock_registry(session)
    model, key = ENTITIES[entity]
    record = find(session, model, record_id) if record_id else model()
    values = body.model_dump()
    if record_id and entity in ('projects', 'rooms'):
        from app.core.supervision_scope import freeze_bound_contexts
        freeze_bound_contexts(session, **{('project_id' if entity == 'projects' else 'room_id'): record_id})
        session.flush()
    for field in ("project_id", "room_id", "person_id"):
        if values.get(field) is not None:
            values[field] = str(values[field])
    if entity == "rooms":
        find(session, BusinessProject, values["project_id"], association=True)
        if record_id and record.project_id != values["project_id"]:
            if exists(session, BusinessSeat, BusinessSeat.room_id == record.room_id) or exists(session, BusinessEventContext, BusinessEventContext.room_id == record.room_id):
                raise HTTPException(409, "评标室已有工位或历史事件，不能变更所属项目")
    elif entity == "participants":
        find(session, BusinessProject, values["project_id"], association=True)
        find(session, BusinessPerson, values["person_id"], association=True)
        if record_id and (record.project_id, record.person_id) != (values["project_id"], values["person_id"]) and membership_used(session, record.project_id, record.person_id):
            raise HTTPException(409, "项目成员已有工位或历史事件，不能变更关联")
    elif entity == "seats":
        room = find(session, BusinessRoom, values["room_id"], association=True)
        values["project_id"] = room.project_id
        if record_id and record.room_id != room.room_id and exists(session, BusinessEventContext, BusinessEventContext.seat_id == record.seat_id):
            raise HTTPException(409, "工位已有历史事件，不能变更所属评标室")
        if values["person_id"] is not None:
            person = find(session, BusinessPerson, values["person_id"], association=True)
            if not exists(session, BusinessParticipant, BusinessParticipant.project_id == room.project_id, BusinessParticipant.person_id == person.person_id):
                raise HTTPException(422, "工位人员必须先加入所属项目")
            new_assignment = not record_id or (record.project_id, record.person_id) != (room.project_id, person.person_id)
            if new_assignment and not person.enabled:
                raise HTTPException(422, "停用人员不能新分配工位")
    for field, value in values.items():
        setattr(record, field, value)
    session.add(record)
    commit_registry(session)
    return entity_dict(record)


@router.get("/registry")
def registry(session: Session = Depends(get_session)):
    return {name: [entity_dict(row) for row in session.query(model).order_by(getattr(model, key)).all()] for name, (model, key) in ENTITIES.items()}


@router.post("/registry/projects", status_code=201)
def create_project(body: ProjectIn, session: Session = Depends(get_session)):
    return save("projects", body, session)


@router.put("/registry/projects/{project_id}")
def update_project(project_id: UUID, body: ProjectIn, session: Session = Depends(get_session)):
    return save("projects", body, session, str(project_id))


@router.post("/registry/rooms", status_code=201)
def create_room(body: RoomIn, session: Session = Depends(get_session)):
    return save("rooms", body, session)


@router.put("/registry/rooms/{room_id}")
def update_room(room_id: UUID, body: RoomIn, session: Session = Depends(get_session)):
    return save("rooms", body, session, str(room_id))


@router.post("/registry/persons", status_code=201)
def create_person(body: PersonIn, session: Session = Depends(get_session)):
    return save("persons", body, session)


@router.put("/registry/persons/{person_id}")
def update_person(person_id: UUID, body: PersonIn, session: Session = Depends(get_session)):
    return save("persons", body, session, str(person_id))


@router.post("/registry/seats", status_code=201)
def create_seat(body: SeatIn, session: Session = Depends(get_session)):
    return save("seats", body, session)


@router.put("/registry/seats/{seat_id}")
def update_seat(seat_id: UUID, body: SeatIn, session: Session = Depends(get_session)):
    return save("seats", body, session, str(seat_id))


@router.post("/registry/participants", status_code=201)
def create_participant(body: ParticipantIn, session: Session = Depends(get_session)):
    return save("participants", body, session)


@router.put("/registry/participants/{membership_id}")
def update_participant(membership_id: UUID, body: ParticipantIn, session: Session = Depends(get_session)):
    return save("participants", body, session, str(membership_id))


@router.delete("/registry/{entity}/{record_id}")
def delete_config(entity: Literal["projects", "rooms", "persons", "seats", "participants"], record_id: UUID, session: Session = Depends(get_session)):
    lock_registry(session)
    model, _ = ENTITIES[entity]
    record = find(session, model, record_id)
    session.delete(record)
    commit_registry(session, deleting=True)
    return {"ok": True}
