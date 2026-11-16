"""S04授权快照（6.3）：房间在册人员、身份核验状态与保密授权的时点快照。

由 6.1 业务事实（在场推导）+ 6.2 人脸核验关联（身份可信）+ 人工授权覆盖（grants）
组装，供语义研判（8.x）的 trusted context 使用：speaker_bindings、
information_visibility、confidentiality_policy_id、recipient 授权。
"""
import uuid

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, ForeignKeyConstraint, Index, String, UniqueConstraint, text
from sqlalchemy.orm import relationship

from app.models import Base


def new_id():
    return str(uuid.uuid4())


class BusinessAuthorizationSnapshot(Base):
    __tablename__ = "business_authorization_snapshots"
    snapshot_id = Column(String(36), primary_key=True, default=new_id)
    project_id = Column(String(36), ForeignKey("business_projects.project_id", ondelete="RESTRICT"), nullable=False)
    room_id = Column(String(36), ForeignKey("business_rooms.room_id", ondelete="RESTRICT"), nullable=False)
    phase_id = Column(String(64), nullable=True)
    # S07语义输入需要：信息公开级别与保密依据
    information_visibility = Column(String(16), nullable=False, default="public")
    confidentiality_policy_id = Column(String(64), nullable=True)
    status = Column(String(16), nullable=False, default="active")  # active=当前有效; superseded=被新快照取代
    created_by = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        # 仅 active 快照按房间唯一（superseded 可多条历史）：DB 用 partial unique index 实现
        Index("business_authz_room_active_unique", "room_id", unique=True, postgresql_where=text("status = 'active'")),
        CheckConstraint("status IN ('active', 'superseded')", name="business_authz_status"),
        CheckConstraint("information_visibility IN ('public', 'confidential')", name="business_authz_visibility"),
        ForeignKeyConstraint(["room_id", "project_id"], ["business_rooms.room_id", "business_rooms.project_id"], ondelete="RESTRICT", name="business_authz_room_project_fk"),
        CheckConstraint("length(btrim(confidentiality_policy_id)) > 0", name="business_authz_policy_text"),
    )
    entries = relationship("BusinessAuthorizationEntry", cascade="all, delete-orphan", back_populates="snapshot")


class BusinessAuthorizationEntry(Base):
    __tablename__ = "business_authorization_entries"
    entry_id = Column(String(36), primary_key=True, default=new_id)
    snapshot_id = Column(String(36), ForeignKey("business_authorization_snapshots.snapshot_id", ondelete="CASCADE"), nullable=False, index=True)
    person_id = Column(String(36), ForeignKey("business_persons.person_id", ondelete="RESTRICT"), nullable=False)
    seat_id = Column(String(36), ForeignKey("business_seats.seat_id", ondelete="SET NULL"), nullable=True)
    role = Column(String(16), nullable=False)
    # 由6.2核验关联推导：在场依据事实是否关联了matched核验
    identity_verified = Column(Boolean, nullable=False, default=False)
    verification_result_id = Column(String(36), ForeignKey("person_verification_results.result_id", ondelete="SET NULL"), nullable=True)
    basis_fact_id = Column(String(36), ForeignKey("business_attendance_facts.fact_id", ondelete="SET NULL"), nullable=True)
    # S07接收授权：默认False，可由人工授权覆盖
    recipient_authorized = Column(Boolean, nullable=False, default=False)
    grant_fact_id = Column(String(36), ForeignKey("business_attendance_facts.fact_id", ondelete="SET NULL"), nullable=True)
    __table_args__ = (
        UniqueConstraint("snapshot_id", "person_id", name="business_authz_entry_unique"),
        CheckConstraint("role IN ('expert', 'supervisor', 'staff', 'owner', 'agent', 'bidder')", name="business_authz_entry_role"),
    )
    snapshot = relationship("BusinessAuthorizationSnapshot", back_populates="entries")
    person = relationship("BusinessPerson", foreign_keys=[person_id])
    seat = relationship("BusinessSeat", foreign_keys=[seat_id])
