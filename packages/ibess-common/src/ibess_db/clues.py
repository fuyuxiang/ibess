"""执行卡 2.3：风险线索、处理失败与观察收据三张独立表（挂 service.models.Base，不改 models.py）。

线索（RiskClue 2.0.0）全量存 JSON，另拆可查列；suspected 映射到现有 events 表时把 event_id 写回线索。
处理失败（规则异常、作用域不符）是处理状态，不是结论，单独一张表，不伪装 normal / insufficient。
观察收据用于消费组重投时去重。JSON 列在 PostgreSQL 上是 JSONB，SQLite 上是 JSON，同一份代码。
"""
from datetime import datetime, timezone

from sqlalchemy import JSON, CheckConstraint, Column, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from ibess_db.base import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")
CONCLUSIONS = ("normal", "suspected", "insufficient")
RISK_LEVELS = ("none", "low", "medium", "high")
CONTRACT_VERSION = "2.0.0"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RiskClueRecord(Base):
    __tablename__ = "risk_clues"

    id = Column(Integer, primary_key=True, autoincrement=True)
    clue_id = Column(String(256), nullable=False, unique=True)
    session_id = Column(String(256), nullable=True, index=True)
    source_id = Column(String(256), nullable=False)
    project_id = Column(String(256), nullable=True)
    room_id = Column(String(256), nullable=True)
    stage_snapshot_id = Column(String(256), nullable=True)
    phase_id = Column(String(32), nullable=True)
    phase_version = Column(Integer, nullable=True)
    binding_status = Column(String(16), nullable=False)
    scene_id = Column(String(8), nullable=False)
    rule_id = Column(String(256), nullable=False)
    rule_version = Column(String(256), nullable=False)
    conclusion = Column(String(16), nullable=False)
    risk_level = Column(String(8), nullable=False)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    media_run_id = Column(String(256), nullable=False)
    asset_id = Column(String(256), nullable=True)
    start_ms = Column(Integer, nullable=False)
    end_ms = Column(Integer, nullable=False)
    person_id = Column(String(256), nullable=True)
    track_id = Column(String(256), nullable=True)
    seat_id = Column(String(256), nullable=True)
    gap_id = Column(String(512), nullable=True)
    reason = Column(Text, nullable=False)
    observation_ids = Column(JSONType, nullable=False, default=list)
    missing_facts = Column(JSONType, nullable=False, default=list)
    quality_flags = Column(JSONType, nullable=False, default=list)
    evidence_refs = Column(JSONType, nullable=False, default=list)
    clue = Column(JSONType, nullable=False)
    event_id = Column(String(36), nullable=True, index=True)
    deduped_from = Column(String(256), nullable=True)
    contract_version = Column(String(16), nullable=False, default=CONTRACT_VERSION)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

    __table_args__ = (
        CheckConstraint("conclusion IN ('normal','suspected','insufficient')", name="risk_clue_conclusion"),
        CheckConstraint("risk_level IN ('none','low','medium','high')", name="risk_clue_risk_level"),
        CheckConstraint("(conclusion = 'suspected' AND risk_level <> 'none') OR (conclusion <> 'suspected' AND risk_level = 'none')",
                        name="risk_clue_level_matches_conclusion"),
        CheckConstraint("binding_status IN ('bound','unbound')", name="risk_clue_binding"),
        CheckConstraint("end_ms >= start_ms AND start_ms >= 0", name="risk_clue_time_order"),
        Index("ix_risk_clues_cooldown", "session_id", "source_id", "scene_id", "rule_id", "media_run_id", "occurred_at"),
        Index("ix_risk_clues_gap", "session_id", "source_id", "gap_id"),
    )


class ClueProcessingRecord(Base):
    """规则异常、作用域不符等处理失败；status 固定 failed，与结论三态无关。"""

    __tablename__ = "clue_processing"

    id = Column(Integer, primary_key=True, autoincrement=True)
    observation_id = Column(String(256), nullable=False, index=True)
    session_id = Column(String(256), nullable=True)
    source_id = Column(String(256), nullable=False)
    scene_id = Column(String(8), nullable=True)
    rule_id = Column(String(256), nullable=True)
    status = Column(String(16), nullable=False, default="failed")
    error_code = Column(String(64), nullable=False)
    error_message = Column(String(512), nullable=False)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

    __table_args__ = (CheckConstraint("status = 'failed'", name="clue_processing_failed_only"),)


class ObservationReceipt(Base):
    """已处理过的观察；重投时跳过。"""

    __tablename__ = "observation_receipts"

    observation_id = Column(String(256), primary_key=True)
    session_id = Column(String(256), nullable=True)
    source_id = Column(String(256), nullable=False)
    type = Column(String(256), nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
