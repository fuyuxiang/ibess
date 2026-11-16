"""文件录音与模型输出片段；音频文件存服务端私有目录。"""
from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, String, Text
from app.models import Base


class AudioRecording(Base):
    __tablename__ = 'audio_recordings'
    recording_id = Column(String(36), primary_key=True)
    title = Column(String(128), nullable=False)
    source_id = Column(String(64), nullable=False)
    source_type = Column(String(16), nullable=False)
    source_key = Column(String(160), nullable=True, unique=True)
    status = Column(String(16), nullable=False, index=True)
    duration_ms = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    claim_at = Column(DateTime(timezone=True), nullable=True)
    claim_id = Column(String(36), nullable=True)
    model = Column(String(160), nullable=True)
    speaker_model = Column(String(160), nullable=True)
    speaker_count = Column(Integer, nullable=True)
    segment_count = Column(Integer, nullable=False, default=0)
    sha256 = Column(String(64), nullable=False)
    original_path = Column(String(80), nullable=False)
    pcm_path = Column(String(80), nullable=True)
    error = Column(Text, nullable=True)
    __table_args__ = (
        CheckConstraint("status IN ('queued','processing','ready','failed')", name='audio_recording_status'),
        CheckConstraint("source_type IN ('file','upload')", name='audio_recording_source_type'),
    )


class AudioSegment(Base):
    __tablename__ = 'audio_segments'
    segment_id = Column(String(36), primary_key=True)
    recording_id = Column(String(36), ForeignKey('audio_recordings.recording_id', ondelete='CASCADE'), nullable=False, index=True)
    ordinal = Column(Integer, nullable=False)
    speaker = Column(String(16), nullable=True)
    start_ms = Column(Integer, nullable=False)
    end_ms = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    __table_args__ = (CheckConstraint('start_ms >= 0 AND end_ms > start_ms', name='audio_segment_time'),)
