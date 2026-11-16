"""媒体绑定模型（4.3）：录音与评标室/项目的显式绑定，供语义研判自动消费授权快照。

视频侧的摄像头↔评标室关系由阶段绑定（phase_bindings.source_id）承载，本表只管音频录音。
"""
import uuid

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, ForeignKeyConstraint, String, UniqueConstraint

from app.models import Base


def new_id():
    return str(uuid.uuid4())


class MediaBinding(Base):
    __tablename__ = "media_bindings"
    binding_id = Column(String(36), primary_key=True, default=new_id)
    recording_id = Column(String(36), nullable=False, unique=True)
    project_id = Column(String(36), ForeignKey("business_projects.project_id", ondelete="CASCADE"), nullable=False)
    room_id = Column(String(36), ForeignKey("business_rooms.room_id", ondelete="CASCADE"), nullable=False)
    actor = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["room_id", "project_id"], ["business_rooms.room_id", "business_rooms.project_id"], ondelete="CASCADE", name="media_binding_room_project_fk"),
        CheckConstraint("length(btrim(recording_id)) > 0", name="media_binding_recording_text"),
    )
