"""4.1 授权素材登记表：契约 AssetManifest 的字段 + 服务端探测结果与审计字段。

模型由 app/schema.py 集中注册，结构变更统一交给 API 的 Alembic 版本迁移。
JSON 列用通用 JSON 并在 PostgreSQL 上落 JSONB，同一份代码也能在 SQLite 上跑隔离测试。
"""
from datetime import datetime, timezone
import uuid

from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

from app.models import Base

CONTRACT_VERSION = "2.0.0"
ASSET_TYPES = ("video", "audio", "scoring")


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StageAsset(Base):
    """一条已登记的授权素材。(session_id, asset_id) 唯一；sha256 是服务端读文件算出来的。"""

    __tablename__ = "stage_assets"

    id = Column(String(36), primary_key=True, default=new_id)
    session_id = Column(String(36), ForeignKey("eval_sessions.session_id", ondelete="RESTRICT"), nullable=False, index=True)
    asset_id = Column(String(256), nullable=False)
    asset_type = Column(String(16), nullable=False)
    relative_path = Column(String(1024), nullable=False)
    sha256 = Column(String(64), nullable=False)
    duration_ms = Column(Integer, nullable=True)
    authorization_source = Column(String(256), nullable=False)
    visibility_notes = Column(String(4096), nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    probe = Column(JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict)
    registered_by = Column(String(128), nullable=False)
    registered_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    contract_version = Column(String(16), nullable=False, default=CONTRACT_VERSION)

    __table_args__ = (
        UniqueConstraint("session_id", "asset_id", name="stage_asset_session_asset"),
        CheckConstraint("asset_type IN ('video', 'audio', 'scoring')", name="stage_asset_type"),
        CheckConstraint("(asset_type = 'scoring' AND duration_ms IS NULL) OR (asset_type <> 'scoring' AND duration_ms >= 1)",
                        name="stage_asset_duration"),
        CheckConstraint("length(trim(asset_id)) > 0", name="stage_asset_id_text"),
        CheckConstraint("length(sha256) = 64", name="stage_asset_sha256_len"),
        CheckConstraint("size_bytes >= 0", name="stage_asset_size"),
    )
