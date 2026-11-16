"""S04人脸核验结果与业务事实的关联（6.2）。

将人员端人脸核验结果（person_verification_results，0.1独占）与签到/换人事实
（business_attendance_facts，6.1独占）建立唯一关联，供6.3授权快照证明"人员=本人"。
"""
import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import relationship

from app.models import Base


def new_id():
    return str(uuid.uuid4())


class BusinessFactVerification(Base):
    __tablename__ = "business_fact_verifications"
    link_id = Column(String(36), primary_key=True, default=new_id)
    fact_id = Column(String(36), ForeignKey("business_attendance_facts.fact_id", ondelete="CASCADE"), nullable=False, unique=True)
    result_id = Column(String(36), ForeignKey("person_verification_results.result_id", ondelete="RESTRICT"), nullable=False)
    actor = Column(String(64), nullable=False)  # 关联操作管理员
    created_at = Column(DateTime(timezone=True), nullable=False)
    result = relationship("PersonVerificationResult", foreign_keys=[result_id])
