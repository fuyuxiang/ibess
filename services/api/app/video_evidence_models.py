"""视频证据资产（9.1扩展）：示例视频文件首次扫描登记SHA-256，后续校验可发现篡改。"""
import uuid

from sqlalchemy import Column, DateTime, String
from sqlalchemy import CheckConstraint

from app.models import Base


def new_id():
    return str(uuid.uuid4())


class EvidenceVideoAsset(Base):
    __tablename__ = "evidence_video_assets"
    asset_id = Column(String(36), primary_key=True, default=new_id)
    file_name = Column(String(160), nullable=False, unique=True)
    sha256 = Column(String(64), nullable=False)
    size = Column(String(32), nullable=False)
    source_label = Column(String(128), nullable=True)
    actor = Column(String(64), nullable=False)
    registered_at = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("length(btrim(file_name)) > 0", name="evidence_video_name"),
    )
