"""独立基础业务配置，不变更检测服务共用模型。"""
import uuid

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from app.models import Base


def new_id():
    return str(uuid.uuid4())


ROLE_CHECK = "role IN ('expert', 'supervisor', 'staff', 'owner', 'agent', 'bidder')"


class BusinessProject(Base):
    __tablename__ = "business_projects"
    project_id = Column(String(36), primary_key=True, default=new_id)
    code = Column(String(64), nullable=False, unique=True)
    name = Column(String(128), nullable=False)
    owner = Column(String(128), nullable=True)
    start_at = Column(DateTime(timezone=True), nullable=True)
    end_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(16), nullable=False, default="draft")
    __table_args__ = (
        CheckConstraint("status IN ('draft', 'running', 'closed')", name="business_project_status"),
        CheckConstraint("start_at IS NULL OR end_at IS NULL OR start_at <= end_at", name="business_project_dates"),
        CheckConstraint("length(btrim(code)) > 0 AND length(btrim(name)) > 0 AND (owner IS NULL OR length(btrim(owner)) > 0)", name="business_project_text"),
    )


class BusinessRoom(Base):
    __tablename__ = "business_rooms"
    room_id = Column(String(36), primary_key=True, default=new_id)
    project_id = Column(String(36), ForeignKey("business_projects.project_id", ondelete="RESTRICT"), nullable=False)
    code = Column(String(64), nullable=False)
    name = Column(String(128), nullable=False)
    location = Column(String(256), nullable=True)
    mode = Column(String(16), nullable=False, default="centralized")
    enabled = Column(Boolean, nullable=False, default=True)
    __table_args__ = (
        UniqueConstraint("project_id", "code", name="business_room_project_code"),
        UniqueConstraint("room_id", "project_id", name="business_room_project_pair"),
        CheckConstraint("mode IN ('centralized', 'remote')", name="business_room_mode"),
        CheckConstraint("length(btrim(code)) > 0 AND length(btrim(name)) > 0 AND (location IS NULL OR length(btrim(location)) > 0)", name="business_room_text"),
    )


class BusinessPerson(Base):
    __tablename__ = "business_persons"
    person_id = Column(String(36), primary_key=True, default=new_id)
    code = Column(String(64), nullable=False, unique=True)
    name = Column(String(128), nullable=False)
    unit = Column(String(128), nullable=False)
    role = Column(String(16), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    __table_args__ = (
        CheckConstraint(ROLE_CHECK, name="business_person_role"),
        CheckConstraint("length(btrim(code)) > 0 AND length(btrim(name)) > 0 AND length(btrim(unit)) > 0", name="business_person_text"),
    )


class BusinessParticipant(Base):
    __tablename__ = "business_participants"
    membership_id = Column(String(36), primary_key=True, default=new_id)
    project_id = Column(String(36), ForeignKey("business_projects.project_id", ondelete="RESTRICT"), nullable=False)
    person_id = Column(String(36), ForeignKey("business_persons.person_id", ondelete="RESTRICT"), nullable=False)
    role = Column(String(16), nullable=False)
    __table_args__ = (
        UniqueConstraint("project_id", "person_id", name="business_participant_project_person"),
        CheckConstraint(ROLE_CHECK, name="business_participant_role"),
    )


class BusinessSeat(Base):
    __tablename__ = "business_seats"
    seat_id = Column(String(36), primary_key=True, default=new_id)
    room_id = Column(String(36), ForeignKey("business_rooms.room_id", ondelete="RESTRICT"), nullable=False)
    # 保存派生项目，以数据库唯一约束防止跨评标室重复分配。
    project_id = Column(String(36), ForeignKey("business_projects.project_id", ondelete="RESTRICT"), nullable=False)
    code = Column(String(64), nullable=False)
    person_id = Column(String(36), ForeignKey("business_persons.person_id", ondelete="RESTRICT"), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    __table_args__ = (
        UniqueConstraint("room_id", "code", name="business_seat_room_code"),
        UniqueConstraint("project_id", "person_id", name="business_seat_project_person"),
        UniqueConstraint("seat_id", "room_id", name="business_seat_room_pair"),
        ForeignKeyConstraint(["room_id", "project_id"], ["business_rooms.room_id", "business_rooms.project_id"], ondelete="RESTRICT", name="business_seat_room_project_fk"),
        ForeignKeyConstraint(["project_id", "person_id"], ["business_participants.project_id", "business_participants.person_id"], ondelete="RESTRICT", name="business_seat_membership_fk"),
        CheckConstraint("length(btrim(code)) > 0", name="business_seat_code"),
    )


class BusinessEventContext(Base):
    __tablename__ = "business_event_contexts"
    event_id = Column(String(36), ForeignKey("events.event_id", ondelete="CASCADE"), primary_key=True)
    project_id = Column(String(36), ForeignKey("business_projects.project_id", ondelete="RESTRICT"), nullable=False)
    room_id = Column(String(36), ForeignKey("business_rooms.room_id", ondelete="RESTRICT"), nullable=False)
    seat_id = Column(String(36), ForeignKey("business_seats.seat_id", ondelete="RESTRICT"), nullable=True)
    person_id = Column(String(36), ForeignKey("business_persons.person_id", ondelete="RESTRICT"), nullable=True)
    snapshot = Column(JSONB, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["room_id", "project_id"], ["business_rooms.room_id", "business_rooms.project_id"], ondelete="RESTRICT", name="business_context_room_project_fk"),
        ForeignKeyConstraint(["seat_id", "room_id"], ["business_seats.seat_id", "business_seats.room_id"], ondelete="RESTRICT", name="business_context_seat_room_fk"),
        ForeignKeyConstraint(["project_id", "person_id"], ["business_participants.project_id", "business_participants.person_id"], ondelete="RESTRICT", name="business_context_membership_fk"),
    )


class RegistryBootstrap(Base):
    __tablename__ = "business_registry_bootstrap"
    key = Column(String(64), primary_key=True)
