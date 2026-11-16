"""Independent persistence for semantic analysis tasks (8.2)."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from app.models import Base


class SemanticAnalysis(Base):
    __tablename__ = "semantic_analyses"

    analysis_id = Column(String(36), primary_key=True)
    recording_id = Column(String(36), ForeignKey("audio_recordings.recording_id", ondelete="CASCADE"), nullable=False, index=True)
    task_key = Column(String(64), nullable=False, unique=True, index=True)
    scene_id = Column(String(8), nullable=False, index=True)
    window_start_ms = Column(Integer, nullable=False)
    window_end_ms = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False, index=True, default="queued")
    request_snapshot = Column(JSONB, nullable=False)
    result = Column(JSONB, nullable=True)
    error = Column(JSONB, nullable=True)
    claim_id = Column(String(36), nullable=True, index=True)
    claimed_by = Column(String(128), nullable=True)
    claim_at = Column(DateTime(timezone=True), nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        CheckConstraint("scene_id IN ('S06','S07')", name="semantic_scene_id"),
        CheckConstraint("status IN ('queued','processing','ready','failed')", name="semantic_analysis_status"),
        CheckConstraint("window_start_ms >= 0 AND window_end_ms > window_start_ms", name="semantic_window_time"),
    )


class SemanticTaskError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def claim_task(session, analysis_id, worker_id, *, lease_seconds=120, now=None):
    """Claim a queued task or recover an expired processing claim."""
    now = now or datetime.now(timezone.utc)
    row = session.get(SemanticAnalysis, str(analysis_id), with_for_update=True)
    if row is None:
        raise SemanticTaskError("ANALYSIS_NOT_FOUND", "语义任务不存在")
    expired = row.status == "processing" and row.claim_at is not None and row.claim_at <= now - timedelta(seconds=lease_seconds)
    if row.status != "queued" and not expired:
        raise SemanticTaskError("ANALYSIS_NOT_CLAIMABLE", "语义任务当前不可认领")
    row.status = "processing"
    row.claim_id = str(uuid4())
    row.claimed_by = str(worker_id)
    row.claim_at = now
    row.attempts += 1
    row.updated_at = now
    return row.claim_id


def complete_task(session, analysis_id, claim_id, result, *, now=None):
    """Commit a result only for the currently active claim."""
    now = now or datetime.now(timezone.utc)
    row = session.get(SemanticAnalysis, str(analysis_id), with_for_update=True)
    if row is None or row.status != "processing" or row.claim_id != str(claim_id):
        return False
    row.status = "ready"
    row.result = result
    row.error = None
    row.claim_id = None
    row.claimed_by = None
    row.claim_at = None
    row.completed_at = now
    row.updated_at = now
    return True
