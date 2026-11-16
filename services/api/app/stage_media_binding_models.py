"""执行卡 4.3：场次级素材绑定表（独立模型，挂 app.models.Base，不改 models.py）。
5.01：原名 media_binding_models / 表 media_bindings，与 B 的 4.3（录音↔评标室）同名，改名共存。

一条录音（recording）、一轮视频推流（media_run）或一份评分素材（scoring）绑到场次：项目 / 室 / 源来自 2.1 的 PhaseBinding，
素材起点 UTC 与时长由调用方给，时间轴按 2.1 的阶段快照切段后存成 JSON。request_id 唯一防重放，(kind, subject_id) 唯一防重绑。
"""
from datetime import datetime, timezone
import uuid

from sqlalchemy import JSON, BigInteger, CheckConstraint, Column, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from app.models import Base

CONTRACT_VERSION = "2.0.0"
BINDING_KINDS = ("recording", "media_run", "scoring")
JSONType = JSON().with_variant(JSONB(), "postgresql")


def new_id() -> str:
    return str(uuid.uuid4())


class StageMediaBinding(Base):
    __tablename__ = "stage_media_bindings"

    binding_id = Column(String(36), primary_key=True, default=new_id)
    session_id = Column(String(36), ForeignKey("eval_sessions.session_id", ondelete="RESTRICT"), nullable=False, index=True)
    project_id = Column(String(36), nullable=False)
    room_id = Column(String(36), nullable=False)
    source_id = Column(String(256), nullable=False)
    kind = Column(String(16), nullable=False)
    subject_id = Column(String(256), nullable=False)
    asset_id = Column(String(256), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False)
    duration_ms = Column(BigInteger, nullable=True)
    timeline = Column(JSONType, nullable=False, default=list)
    request_id = Column(String(64), nullable=False, unique=True)
    request_digest = Column(String(64), nullable=False)
    created_by = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    contract_version = Column(String(16), nullable=False, default=CONTRACT_VERSION)

    __table_args__ = (
        UniqueConstraint("kind", "subject_id", name="stage_media_binding_subject"),
        CheckConstraint("kind IN ('recording','media_run','scoring')", name="stage_media_binding_kind"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="stage_media_binding_duration"),
        CheckConstraint("length(btrim(subject_id)) > 0", name="stage_media_binding_subject_text"),
        CheckConstraint("(kind = 'scoring' AND asset_id IS NOT NULL) OR kind <> 'scoring'", name="stage_media_binding_scoring_asset"),
    )
