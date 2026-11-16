"""监管闭环独立于检测服务的事件状态，增量建表，不改写历史证据。"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, CheckConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from app.models import Base


class SupervisionCase(Base):
    __tablename__ = 'supervision_cases'
    event_id = Column(String(36), ForeignKey('events.event_id', ondelete='CASCADE'), primary_key=True)
    status = Column(String(24), nullable=False, default='PENDING', index=True)
    version = Column(Integer, nullable=False, default=0)
    resolution = Column(String(32), nullable=True)
    assignee = Column(String(128), nullable=True)
    due_at = Column(DateTime, nullable=True)
    feedback = Column(Text, nullable=True)
    archived_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (CheckConstraint("status IN ('PENDING','SUPPLEMENT','CONFIRMED','ASSIGNED','FEEDBACK','ARCHIVED')", name='supervision_case_status'),)


class SupervisionOperation(Base):
    __tablename__ = 'supervision_operations'
    request_id = Column(String(64), primary_key=True)
    event_id = Column(String(36), ForeignKey('events.event_id', ondelete='CASCADE'), nullable=False, index=True)
    fingerprint = Column(String(64), nullable=False)
    actor = Column(String(64), nullable=False)
    result = Column(JSONB, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class SemanticEventLink(Base):
    __tablename__ = 'semantic_event_links'
    analysis_id = Column(String(36), ForeignKey('semantic_analyses.analysis_id', ondelete='RESTRICT'), primary_key=True)
    event_id = Column(String(36), ForeignKey('events.event_id', ondelete='RESTRICT'), nullable=False, unique=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class OperatorAudit(Base):
    __tablename__ = 'operator_audit'
    id = Column(Integer, primary_key=True)
    operator_id = Column(String(36), nullable=False, index=True)
    actor = Column(String(64), nullable=False)
    action = Column(String(32), nullable=False)
    before = Column(JSONB, nullable=False)
    after = Column(JSONB, nullable=False)
    at = Column(DateTime, nullable=False, default=datetime.utcnow)


class SupervisionIntakeFailure(Base):
    __tablename__ = 'supervision_intake_failures'
    analysis_id = Column(String(36), ForeignKey('semantic_analyses.analysis_id', ondelete='CASCADE'), primary_key=True)
    attempts = Column(Integer, nullable=False, default=0)
    error = Column(Text, nullable=False)
    next_retry_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class RecordingCaptureTime(Base):
    """人工登记的实际采集起点，与上传时间分开保存。"""
    __tablename__ = 'recording_capture_times'
    recording_id = Column(String(36), ForeignKey('audio_recordings.recording_id', ondelete='CASCADE'), primary_key=True)
    started_at = Column(DateTime(timezone=True), nullable=False)
    actor = Column(String(64), nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class SemanticBusinessSnapshot(Base):
    __tablename__ = 'semantic_business_snapshots'
    analysis_id = Column(String(36), ForeignKey('semantic_analyses.analysis_id', ondelete='CASCADE'), primary_key=True)
    project_id = Column(String(36), ForeignKey('business_projects.project_id', ondelete='RESTRICT'), nullable=False)
    room_id = Column(String(36), ForeignKey('business_rooms.room_id', ondelete='RESTRICT'), nullable=False)
    snapshot = Column(JSONB, nullable=False)


class SupervisionMediaHold(Base):
    """独立持久化卷中的事件窗口，原始轮转录像删除不影响本表引用。"""
    __tablename__ = 'supervision_media_holds'
    event_id = Column(String(36), ForeignKey('events.event_id', ondelete='RESTRICT'), primary_key=True)
    status = Column(String(24), nullable=False, default='PENDING', index=True)
    manifest = Column(JSONB, nullable=False, default=list)
    error = Column(Text, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    next_retry_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class SupervisionArchive(Base):
    """归档版本只追加；重新核查不覆盖原认定与资料状况。"""
    __tablename__ = 'supervision_archives'
    event_id = Column(String(36), ForeignKey('events.event_id', ondelete='RESTRICT'), primary_key=True)
    version = Column(Integer, primary_key=True)
    resolution = Column(String(32), nullable=True)
    actor = Column(String(64), nullable=False)
    note = Column(Text, nullable=False)
    evidence_note = Column(Text, nullable=True)
    snapshot = Column(JSONB, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class OperatorApplication(Base):
    __tablename__ = 'supervision_operator_receipts'
    consumer_id = Column(String(128), primary_key=True)
    configuration = Column(JSONB, nullable=False)
    applied_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class SupervisionChannel(Base):
    """辅助机位和拾音源复用已有场次主绑定，不创建虚假场次。"""
    __tablename__ = 'supervision_channels'
    channel_id = Column(String(36), primary_key=True)
    session_id = Column(String(36), ForeignKey('eval_sessions.session_id', ondelete='RESTRICT'), nullable=False, index=True)
    source_id = Column(String(64), nullable=False)
    name = Column(String(128), nullable=False)
    kind = Column(String(16), nullable=False, default='video')
    active = Column(Boolean, nullable=False, default=True)
    actor = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint('session_id','source_id'), CheckConstraint("kind IN ('video','audio')"))


class SupervisionEventSpan(Base):
    __tablename__ = 'supervision_event_spans'
    event_id = Column(String(36), ForeignKey('events.event_id', ondelete='RESTRICT'), primary_key=True)
    group_key = Column(String(64), nullable=True, index=True)
    started_at = Column(DateTime, nullable=False)
    last_seen_at = Column(DateTime, nullable=False)
    ended_at = Column(DateTime, nullable=True)
    active = Column(Boolean, nullable=False, default=False)
    trigger_count = Column(Integer, nullable=False, default=1)
    end_basis = Column(String(32), nullable=True)


class SupervisionOccurrence(Base):
    __tablename__ = 'supervision_occurrences'
    occurrence_id = Column(String(64), primary_key=True)
    event_id = Column(String(36), ForeignKey('events.event_id', ondelete='RESTRICT'), nullable=False, index=True)
    occurred_at = Column(DateTime, nullable=False)
    detail = Column(JSONB, nullable=False, default=dict)


class SupervisionEvidenceWindow(Base):
    """人工关联或扩展的摄像头证据范围；追加操作审计并使保全重新检查。"""
    __tablename__ = 'supervision_evidence_windows'
    event_id = Column(String(36), ForeignKey('events.event_id', ondelete='RESTRICT'), primary_key=True)
    source_id = Column(String(64), primary_key=True)
    started_at = Column(DateTime, nullable=False)
    ended_at = Column(DateTime, nullable=False)
    note = Column(Text, nullable=False)
    actor = Column(String(64), nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (CheckConstraint('ended_at > started_at'),)


class SupervisionAudioLink(Base):
    __tablename__ = 'supervision_audio_links'
    event_id = Column(String(36), ForeignKey('events.event_id', ondelete='RESTRICT'), primary_key=True)
    recording_id = Column(String(36), ForeignKey('audio_recordings.recording_id', ondelete='RESTRICT'), primary_key=True)
    capture_started_at = Column(DateTime, nullable=False)
    note = Column(Text, nullable=False)
    actor = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class SupervisionTaskReceipt(Base):
    __tablename__ = 'supervision_task_receipts'
    event_id = Column(String(36), ForeignKey('events.event_id', ondelete='RESTRICT'), primary_key=True)
    assignment_version = Column(Integer, primary_key=True)
    assignee = Column(String(128), nullable=False)
    received_by = Column(String(64), nullable=True)
    received_at = Column(DateTime, nullable=True)
    note = Column(Text, nullable=True)


class SupervisionCoverageSample(Base):
    """持久化采样结果；未知与未采样区间不能解释成监测正常。"""
    __tablename__ = 'supervision_coverage_samples'
    id = Column(Integer, primary_key=True)
    session_id = Column(String(36), ForeignKey('eval_sessions.session_id', ondelete='RESTRICT'), nullable=False, index=True)
    source_id = Column(String(64), nullable=False)
    observed_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    states = Column(JSONB, nullable=False)


class SupervisionSignal(Base):
    __tablename__ = 'supervision_signals'
    session_id = Column(String(64), primary_key=True)
    source_id = Column(String(64), primary_key=True)
    kind = Column(String(16), primary_key=True)
    observed_at = Column(DateTime, nullable=False)
    detail = Column(JSONB, nullable=False, default=dict)


class SupervisionSessionReport(Base):
    __tablename__ = 'supervision_session_reports'
    report_id = Column(String(36), primary_key=True)
    session_id = Column(String(36), ForeignKey('eval_sessions.session_id', ondelete='RESTRICT'), nullable=False, index=True)
    actor = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    snapshot = Column(JSONB, nullable=False)


class SupervisionEvidenceAnnotation(Base):
    __tablename__ = 'supervision_evidence_annotations'
    annotation_id = Column(String(36), primary_key=True)
    event_id = Column(String(36), ForeignKey('events.event_id',ondelete='RESTRICT'), nullable=False,index=True)
    recording_id = Column(String(36), ForeignKey('audio_recordings.recording_id',ondelete='RESTRICT'), nullable=False)
    segment_id = Column(String(36), nullable=False)
    kind = Column(String(32), nullable=False)
    original = Column(JSONB, nullable=False)
    correction = Column(Text, nullable=False)
    actor = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (CheckConstraint("kind IN ('transcript_correction','identity_note')"),)
