"""评分记录模型（9.3）：固定JSON评分文档，含规范SHA-256（幂等与完整性依据）。"""
import uuid

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, ForeignKeyConstraint, String
from sqlalchemy.dialects.postgresql import JSONB

from app.models import Base


def new_id():
    return str(uuid.uuid4())


class BusinessScoreRecord(Base):
    __tablename__ = "business_score_records"
    record_id = Column(String(36), primary_key=True, default=new_id)
    project_id = Column(String(36), ForeignKey("business_projects.project_id", ondelete="RESTRICT"), nullable=False)
    room_id = Column(String(36), ForeignKey("business_rooms.room_id", ondelete="RESTRICT"), nullable=False)
    phase_id = Column(String(64), nullable=False)
    payload = Column(JSONB, nullable=False)
    content_sha256 = Column(String(64), nullable=False, unique=True)
    actor = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["room_id", "project_id"], ["business_rooms.room_id", "business_rooms.project_id"], ondelete="RESTRICT", name="business_score_room_project_fk"),
        CheckConstraint("length(btrim(phase_id)) > 0", name="business_score_phase_text"),
    )
