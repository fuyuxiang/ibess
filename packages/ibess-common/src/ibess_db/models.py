"""Single mappings for tables accessed by API, event-service and media-service."""
from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from ibess_db.base import Base

class Operator(Base):
    """可治理的场景算子定义。实现代码随服务发布，配置通过 API 管理。"""
    __tablename__ = "operators"

    id = Column(Integer, primary_key=True, autoincrement=True)
    operator_id = Column(String(36), unique=True, index=True)
    code = Column(String(64), unique=True, index=True)
    name = Column(String(128))
    category = Column(String(32), default="rule")
    version = Column(String(32), default="v1")
    enabled = Column(Boolean, default=False)
    input_types = Column(JSONB, nullable=False, default=list)
    output_event_type = Column(String(64))
    risk_level = Column(String(16), default="low")
    parameters = Column(JSONB, nullable=False, default=dict)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class OperatorBinding(Base):
    """算子与场次、摄像头、区域的作用域绑定。"""
    __tablename__ = "operator_bindings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    binding_id = Column(String(36), unique=True, index=True)
    operator_id = Column(String(36), index=True)
    session_id = Column(String(64), nullable=True, index=True)
    camera_id = Column(String(64), nullable=True, index=True)
    zone_id = Column(String(36), nullable=True, index=True)
    enabled = Column(Boolean, default=True)
    parameters = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)

class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(36), unique=True, index=True)
    session_id = Column(String(64), index=True)
    camera_id = Column(String(64))
    person_id = Column(String(64), nullable=True)
    seat_id = Column(String(64), nullable=True)
    zone_id = Column(String(64), nullable=True)
    skill_code = Column(String(64))
    skill_version = Column(String(32))
    model_name = Column(String(64))
    model_version = Column(String(32))
    event_type = Column(String(64), index=True)
    phase_id = Column(String(32), index=True)
    risk_level = Column(String(16), default="low")
    confidence = Column(Float)
    reason = Column(Text)
    occurred_at = Column(DateTime)
    status = Column(String(16), default="PENDING", index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Decision(Base):
    __tablename__ = "decisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(36), index=True)
    action = Column(String(32))          # confirm / false_positive / escalate / close
    reviewer_id = Column(String(64))
    note = Column(Text)
    decided_at = Column(DateTime, default=datetime.utcnow)

class Evidence(Base):
    __tablename__ = "evidences"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(36), index=True)
    evidence_type = Column(String(32))   # keyframe / video / audio / transcript
    object_key = Column(String(256))
    sha256 = Column(String(64), nullable=True)
    captured_at = Column(DateTime, default=datetime.utcnow)
