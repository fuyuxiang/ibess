"""离线质检运行模型（9.4）：单文件单任务的离线重跑批次。"""
import uuid

from sqlalchemy import CheckConstraint, Column, DateTime, String
from sqlalchemy.dialects.postgresql import JSONB

from app.models import Base


def new_id():
    return str(uuid.uuid4())


class OfflineQaRun(Base):
    __tablename__ = "offline_qa_runs"
    run_id = Column(String(36), primary_key=True, default=new_id)
    recording_id = Column(String(36), nullable=False)
    room_id = Column(String(36), nullable=False)
    scenes = Column(JSONB, nullable=False)
    task_keys = Column(JSONB, nullable=False, default=list)
    status = Column(String(16), nullable=False, default="queued")
    actor = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("status IN ('queued','completed','failed')", name="offline_qa_status"),
    )
