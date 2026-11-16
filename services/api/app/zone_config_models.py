"""3.3按媒体源版本化保存区域几何，不修改旧zones表。"""
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from app.models import Base


class SourceZoneConfig(Base):
    __tablename__ = 'source_zone_configs'

    config_id = Column(String(36), primary_key=True)
    session_id = Column(String(36), ForeignKey('eval_sessions.session_id', ondelete='RESTRICT'), nullable=False)
    project_id = Column(String(36), ForeignKey('business_projects.project_id', ondelete='RESTRICT'), nullable=False)
    room_id = Column(String(36), nullable=False)
    source_id = Column(String(256), nullable=False)
    config_version = Column(Integer, nullable=False)
    zones = Column(JSONB, nullable=False, default=list)
    entry_lines = Column(JSONB, nullable=False, default=list)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        ForeignKeyConstraint(['room_id', 'project_id'], ['business_rooms.room_id', 'business_rooms.project_id'],
            ondelete='RESTRICT', name='source_zone_config_room_project'),
        UniqueConstraint('session_id', 'source_id', name='source_zone_config_session_source'),
        CheckConstraint('config_version > 0', name='source_zone_config_version_positive'),
        CheckConstraint('length(btrim(source_id)) > 0', name='source_zone_config_source_nonempty'),
    )
