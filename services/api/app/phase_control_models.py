"""2.2阶段控制事务outbox与消费者确认。"""
from datetime import datetime, timezone
import uuid

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from app.models import Base


class PhaseControlOutbox(Base):
    __tablename__ = 'phase_control_outbox'
    notification_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    stage_snapshot_id = Column(String(36), ForeignKey('phase_snapshots.stage_snapshot_id', ondelete='CASCADE'), nullable=False, unique=True)
    session_id = Column(String(36), nullable=False, index=True)
    project_id = Column(String(36), nullable=False)
    room_id = Column(String(36), nullable=False)
    source_id = Column(String(256), nullable=False)
    phase_version = Column(Integer, nullable=False)
    required_consumers = Column(JSONB, nullable=False)
    delivery_status = Column(String(16), nullable=False, default='pending')
    attempts = Column(Integer, nullable=False, default=0)
    last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True, index=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    last_error_code = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (
        CheckConstraint("delivery_status IN ('pending','published')", name='phase_outbox_delivery_status'),
        CheckConstraint('attempts >= 0', name='phase_outbox_attempts_positive'),
        CheckConstraint("jsonb_typeof(required_consumers) = 'array' AND jsonb_array_length(required_consumers) > 0", name='phase_outbox_required_consumers'),
    )


class PhaseControlAck(Base):
    __tablename__ = 'phase_control_acks'
    id = Column(Integer, primary_key=True, autoincrement=True)
    stage_snapshot_id = Column(String(36), ForeignKey('phase_snapshots.stage_snapshot_id', ondelete='CASCADE'), nullable=False)
    session_id = Column(String(36), nullable=False, index=True)
    project_id = Column(String(36), nullable=False)
    room_id = Column(String(36), nullable=False)
    source_id = Column(String(256), nullable=False)
    phase_id = Column(String(32), nullable=False)
    phase_version = Column(Integer, nullable=False)
    consumer_id = Column(String(128), nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)
    applied_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(16), nullable=False)
    error = Column(JSONB(none_as_null=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (
        UniqueConstraint('stage_snapshot_id', 'consumer_id', name='phase_ack_snapshot_consumer'),
        CheckConstraint("status IN ('applied','missing_asset','failed')", name='phase_ack_status'),
        CheckConstraint("(status = 'applied' AND applied_at IS NOT NULL AND error IS NULL AND applied_at >= received_at) OR (status <> 'applied' AND applied_at IS NULL AND error IS NOT NULL)",name='phase_ack_state_consistent'),
    )
