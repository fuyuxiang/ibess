"""私有人脸登记；仅保存标准化缩略图及特征，不保存上传原图。"""
from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Float, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.dialects.postgresql import JSONB
from app.models import Base


class FaceProfile(Base):
    __tablename__ = 'business_face_profiles'
    person_id = Column(String(36), ForeignKey('business_persons.person_id', ondelete='CASCADE'), primary_key=True)
    version = Column(String(36), nullable=False)
    model = Column(String(160), nullable=False)
    registered_at = Column(DateTime(timezone=True), nullable=False)
    embedding = Column(JSONB, nullable=False)
    photo = Column(LargeBinary, nullable=False)


class PersonVerificationLink(Base):
    __tablename__ = 'person_verification_links'
    link_id = Column(String(36), primary_key=True)
    person_id = Column(String(36), ForeignKey('business_persons.person_id', ondelete='CASCADE'), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True)
    reference_version = Column(String(36), nullable=False)
    project_id = Column(String(36), ForeignKey('business_projects.project_id', ondelete='CASCADE'), nullable=True)
    room_id = Column(String(36), ForeignKey('business_rooms.room_id', ondelete='CASCADE'), nullable=True)
    project_name = Column(String(128), nullable=True)
    room_name = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=5)
    __table_args__ = (CheckConstraint('attempts >= 0 AND attempts <= max_attempts AND max_attempts = 5', name='person_verification_attempt_limit'),)


class PersonVerificationResult(Base):
    __tablename__ = 'person_verification_results'
    result_id = Column(String(36), primary_key=True)
    link_id = Column(String(36), ForeignKey('person_verification_links.link_id', ondelete='SET NULL'), nullable=True, index=True)
    person_id = Column(String(36), ForeignKey('business_persons.person_id', ondelete='SET NULL'), nullable=True, index=True)
    status = Column(String(16), nullable=False)
    matched = Column(Boolean, nullable=True)
    similarity = Column(Float, nullable=True)
    threshold = Column(Float, nullable=True)
    checked_at = Column(DateTime(timezone=True), nullable=False)
    person_name = Column(String(128), nullable=False)
    project_name = Column(String(128), nullable=True)
    room_name = Column(String(128), nullable=True)
    model = Column(String(160), nullable=True)
    reference_version = Column(String(36), nullable=False)
    error = Column(String(512), nullable=True)
    __table_args__ = (CheckConstraint("status IN ('matched', 'unmatched', 'invalid')", name='person_verification_result_status'),)
