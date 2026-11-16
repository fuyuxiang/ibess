from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from ibess_db.base import Base
from ibess_db.models import Event, Decision, Evidence, Operator, OperatorBinding


class EvalSession(Base):
    """评标场次（避免与 SQLAlchemy Session 命名冲突）"""
    __tablename__ = "eval_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(36), unique=True, index=True)
    name = Column(String(128))
    status = Column(String(16), default="draft")      # draft / running / closed
    current_phase = Column(String(32), default="review")
    expected_people = Column(Integer, default=0)      # 应到人数
    start_at = Column(DateTime, nullable=True)
    end_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Person(Base):
    """专家、监督人员、工作人员"""
    __tablename__ = "persons"

    id = Column(Integer, primary_key=True, autoincrement=True)
    person_id = Column(String(36), unique=True, index=True)
    name = Column(String(64))
    role = Column(String(32))                         # expert / supervisor / staff
    external_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Seat(Base):
    """专家工位和人员分配"""
    __tablename__ = "seats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    seat_id = Column(String(36), unique=True, index=True)
    session_id = Column(String(36), index=True)
    code = Column(String(32))                         # 工位号
    person_id = Column(String(36), nullable=True)
    zone_id = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Zone(Base):
    """工位区、出入口和限制区域"""
    __tablename__ = "zones"

    id = Column(Integer, primary_key=True, autoincrement=True)
    zone_id = Column(String(36), unique=True, index=True)
    session_id = Column(String(36), index=True)
    camera_id = Column(String(64), nullable=True)
    name = Column(String(64))
    type = Column(String(32))                         # seat / entry / restricted
    polygon = Column(JSONB, nullable=True)            # 多边形坐标点
    created_at = Column(DateTime, default=datetime.utcnow)


class Skill(Base):
    """技能版本和规则参数"""
    __tablename__ = "skills"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(String(36), unique=True, index=True)
    code = Column(String(64), index=True)
    version = Column(String(32))
    enabled = Column(Boolean, default=True)
    parameters = Column(JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)






class SensitiveWord(Base):
    """敏感词与同音词"""
    __tablename__ = "sensitive_words"

    id = Column(Integer, primary_key=True, autoincrement=True)
    word = Column(String(64))
    homophone = Column(String(64), nullable=True)     # 同音词
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)








class POCEventMeta(Base):
    """独立演示元数据，不改变检测服务共用表。"""
    __tablename__ = "demo_event_meta"
    event_id = Column(String(36), primary_key=True)
    room = Column(String(128), nullable=False)
    note = Column(Text, default="")
    request_id = Column(String(64), unique=True, nullable=False)


class WorkflowRequest(Base):
    __tablename__ = "workflow_requests"
    request_id = Column(String(64), primary_key=True)
    event_id = Column(String(36), nullable=False)
    action = Column(String(32), nullable=False)
    reviewer_id = Column(String(64), nullable=False)
    note = Column(Text, default="")
    decision_id = Column(Integer, nullable=False)
    result_status = Column(String(16), nullable=False)


class WorkflowAudit(Base):
    __tablename__ = "workflow_audit"
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(36), index=True, nullable=False)
    action = Column(String(64), nullable=False)
    actor = Column(String(64), nullable=False)
    note = Column(Text, default="")
    at = Column(DateTime, default=datetime.utcnow)


class ReminderAsset(Base):
    __tablename__ = "demo_reminder_assets"
    id = Column(Integer, primary_key=True)
    url = Column(String(256), nullable=True)
    version = Column(String(64), nullable=True)


class ReminderExecution(Base):
    __tablename__ = "reminder_executions"
    execution_id = Column(String(36), primary_key=True)
    event_id = Column(String(36), index=True, nullable=False)
    decision_id = Column(Integer, unique=True, nullable=False)
    status = Column(String(32), nullable=False)
    asset_url = Column(String(256), nullable=True)
    asset_version = Column(String(64), nullable=True)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ReminderAttempt(Base):
    __tablename__ = "reminder_attempts"
    attempt_id = Column(String(36), primary_key=True)
    execution_id = Column(String(36), index=True, nullable=False)
    asset_url = Column(String(256), nullable=True)
    asset_version = Column(String(64), nullable=True)
    status = Column(String(32), nullable=False)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class SensitiveLexicon(Base):
    """敏感词库：转写完成后按词库自动生成 sensitive_word 事件。"""
    __tablename__ = "sensitive_lexicon"
    word = Column(String(64), primary_key=True)
    risk_level = Column(String(16), nullable=False, default="medium")
    enabled = Column(Boolean, nullable=False, default=True)
    note = Column(String(256))
    category = Column(String(32))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class FaceVerifyConfig(Base):
    """身份核验现有配置接口依赖的单行配置；支持全新数据库初始化。"""
    __tablename__ = 'face_verify_config'
    id = Column(Integer, primary_key=True)
    threshold_same = Column(Float, nullable=False, default=0.363)
    threshold_suspect = Column(Float, nullable=False, default=0.280)
    auto_enabled = Column(Boolean, nullable=False, default=False)
    interval_seconds = Column(Integer, nullable=False, default=600)
    updated_by = Column(String(64), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now())
