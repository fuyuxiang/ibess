"""Session preparation, operational journals and immutable archive references."""

from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, false
from sqlalchemy.dialects.postgresql import JSONB
from app.models import Base


class SessionWorkflow(Base):
    __tablename__ = "supervision_session_workflows"
    session_id = Column(
        String(36),
        ForeignKey("eval_sessions.session_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    version = Column(Integer, nullable=False, default=0)
    checks = Column(JSONB, nullable=False, default=dict)
    phase_plans = Column(JSONB, nullable=False, default=dict)
    preparation_snapshot = Column(JSONB)
    identity_verification_enabled = Column(Boolean, nullable=False, default=False, server_default=false())
    started_by = Column(String(64))
    ended_by = Column(String(64))
    end_note = Column(Text)
    archived_report_id = Column(
        String(36),
        ForeignKey("supervision_session_reports.report_id", ondelete="RESTRICT"),
    )
    archived_fingerprint = Column(String(64))
    archived_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class SessionJournal(Base):
    __tablename__ = "supervision_session_journal"
    journal_id = Column(String(36), primary_key=True)
    session_id = Column(
        String(36),
        ForeignKey("eval_sessions.session_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    kind = Column(String(32), nullable=False)
    note = Column(Text, nullable=False)
    actor = Column(String(64), nullable=False)
    detail = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
