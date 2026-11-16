"""9.4 离线质检任务表：一行一个「单源录像 × 冻结版本」重跑任务，结果只存在本行，不写实时事件。

模型由 app/schema.py 集中注册，结构变更统一交给 API 的 Alembic 版本迁移。
worker（services/offline-worker）用 Core 表定义直连本表认领，列名两边必须一致。
"""
from datetime import datetime, timezone
import uuid

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

from app.models import Base

CONTRACT_VERSION = "2.0.0"
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")
RETRYABLE_STATUSES = ("failed", "cancelled")
DEFAULT_MAX_ATTEMPTS = 3

JSONType = JSON().with_variant(JSONB, "postgresql")


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OfflineQaJob(Base):
    """frozen 在建任务时写定（components 在第一次运行时由 worker 补写），之后只读；result 只由当前认领写。"""

    __tablename__ = "offline_qa_jobs"

    job_id = Column(String(36), primary_key=True, default=new_id)
    request_id = Column(String(64), nullable=False, unique=True)
    request_digest = Column(String(64), nullable=False)
    session_id = Column(String(36), ForeignKey("eval_sessions.session_id", ondelete="RESTRICT"), nullable=False, index=True)
    project_id = Column(String(36), nullable=False)
    room_id = Column(String(36), nullable=False)
    source_id = Column(String(256), nullable=False)
    binding_id = Column(String(36), nullable=False, index=True)
    asset_id = Column(String(256), nullable=False)
    status = Column(String(16), nullable=False, default="queued", index=True)
    cancel_requested = Column(Boolean, nullable=False, default=False)
    frozen = Column(JSONType, nullable=False)
    progress_ms = Column(Integer, nullable=False, default=0)
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=DEFAULT_MAX_ATTEMPTS)
    claim_id = Column(String(36), nullable=True)
    claimed_by = Column(String(128), nullable=True)
    claim_at = Column(DateTime(timezone=True), nullable=True)
    error = Column(JSONType, nullable=True)
    result = Column(JSONType, nullable=True)
    created_by = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    contract_version = Column(String(16), nullable=False, default=CONTRACT_VERSION)

    __table_args__ = (
        CheckConstraint("status IN ('queued','running','succeeded','failed','cancelled')", name="offline_qa_job_status"),
        CheckConstraint("progress_ms >= 0 AND attempts >= 0 AND max_attempts >= 1", name="offline_qa_job_counters"),
        CheckConstraint("(status = 'running') = (claim_id IS NOT NULL)", name="offline_qa_job_claim"),
    )
