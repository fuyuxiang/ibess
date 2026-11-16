"""Append-only, room-scoped monitoring resources. Sessions copy an explicit revision."""

from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from app.models import Base


class RoomMonitoringPreset(Base):
    __tablename__ = "room_monitoring_presets"
    preset_id = Column(String(36), primary_key=True)
    room_id = Column(
        String(36),
        ForeignKey("business_rooms.room_id", ondelete="RESTRICT"),
        nullable=False,
    )
    version = Column(Integer, nullable=False)
    scheme = Column(String(16), nullable=False)
    primary_source_id = Column(String(64), nullable=False)
    channels = Column(JSONB, nullable=False)
    created_by = Column(String(64), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    note = Column(String(500), nullable=False)
    __table_args__ = (
        UniqueConstraint("room_id", "version", name="room_monitoring_preset_version"),
    )
