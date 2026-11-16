"""2.1阶段绑定与不可变阶段快照模型。"""
from datetime import datetime, timezone
import uuid

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, UniqueConstraint, event
from sqlalchemy.dialects.postgresql import JSONB

from app.models import Base


def new_id():
    return str(uuid.uuid4())


class PhaseBinding(Base):
    """明确场次属于哪个项目、评标室和媒体源；绑定建立后不静默改写。"""

    __tablename__ = "phase_bindings"

    binding_id = Column(String(36), primary_key=True, default=new_id)
    session_id = Column(String(36), ForeignKey('eval_sessions.session_id', ondelete='RESTRICT'), nullable=False, unique=True, index=True)
    project_id = Column(String(36), ForeignKey('business_projects.project_id', ondelete='RESTRICT'), nullable=False, index=True)
    room_id = Column(String(36), nullable=False, index=True)
    source_id = Column(String(256), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    active = Column(Boolean, nullable=False, default=True)
    __table_args__ = (
        ForeignKeyConstraint(['room_id', 'project_id'], ['business_rooms.room_id', 'business_rooms.project_id'], ondelete='RESTRICT', name='phase_binding_room_project'),
        UniqueConstraint('session_id', 'project_id', 'room_id', 'source_id', name='phase_binding_scope'),
        CheckConstraint("length(btrim(session_id)) > 0", name="phase_binding_session_text"),
        CheckConstraint("length(btrim(project_id)) > 0", name="phase_binding_project_text"),
        CheckConstraint("length(btrim(room_id)) > 0", name="phase_binding_room_text"),
        CheckConstraint("length(btrim(source_id)) > 0", name="phase_binding_source_text"),
    )


class PhaseSnapshot(Base):
    """阶段配置快照。业务更新只能追加新行，不能UPDATE/DELETE历史。"""

    __tablename__ = "phase_snapshots"

    stage_snapshot_id = Column(String(36), primary_key=True, default=new_id)
    session_id = Column(String(36), nullable=False, index=True)
    project_id = Column(String(36), nullable=False)
    room_id = Column(String(36), nullable=False)
    source_id = Column(String(256), nullable=False)
    phase_id = Column(String(32), nullable=False)
    phase_version = Column(Integer, nullable=False)
    effective_at = Column(DateTime(timezone=True), nullable=False)
    enabled_scenes = Column(JSONB, nullable=False, default=list)
    always_on_scenes = Column(JSONB, nullable=False, default=lambda: ["S10"])
    assets = Column(JSONB, nullable=False, default=list)
    rule_versions = Column(JSONB, nullable=False, default=dict)
    exceptions = Column(JSONB, nullable=False, default=list)
    sync_status = Column(String(32), nullable=False, default="pending")
    actor_id = Column(String(128), nullable=False)
    reason = Column(String(1024), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (
        ForeignKeyConstraint(['session_id', 'project_id', 'room_id', 'source_id'], ['phase_bindings.session_id', 'phase_bindings.project_id', 'phase_bindings.room_id', 'phase_bindings.source_id'], ondelete='RESTRICT', name='phase_snapshot_binding_scope'),
        UniqueConstraint("session_id", "phase_version", name="phase_snapshot_session_version"),
        UniqueConstraint("session_id", "effective_at", name="phase_snapshot_session_effective"),
        CheckConstraint("phase_version > 0", name="phase_snapshot_version_positive"),
        CheckConstraint("sync_status IN ('pending','partially_applied','applied','missing_asset','failed')", name="phase_snapshot_sync_status"),
        CheckConstraint("always_on_scenes = '[\"S10\"]'::jsonb", name="phase_snapshot_s10_always_on"),
        CheckConstraint("phase_id IN ('SIGNIN','DISCIPLINE','PRELIM','DETAIL','CLARIFY','REPORT')", name='phase_snapshot_known_phase'),
        CheckConstraint("length(btrim(actor_id)) > 0", name="phase_snapshot_actor_text"),
        CheckConstraint("length(btrim(reason)) > 0", name="phase_snapshot_reason_text"),
    )


@event.listens_for(PhaseSnapshot, "before_update")
def reject_snapshot_update(mapper, connection, target):
    raise ValueError("阶段快照不可修改，请追加新版本")


@event.listens_for(PhaseSnapshot, "before_delete")
def reject_snapshot_delete(mapper, connection, target):
    raise ValueError("阶段快照不可删除")
