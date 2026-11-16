"""S04业务事实：签到、换人、离岗许可与澄清事实（6.1）。

独立于0.4的基础配置模型；事实记录供6.2人脸结果关联、6.3授权快照与
语义研判业务上下文（speaker_bindings/recipient授权）消费。
"""
import uuid

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, ForeignKeyConstraint, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from app.models import Base


def new_id():
    return str(uuid.uuid4())


FACT_TYPE_CHECK = "fact_type IN ('check_in', 'check_out', 'change', 'leave_permit', 'clarification')"


class BusinessAttendanceFact(Base):
    __tablename__ = "business_attendance_facts"
    fact_id = Column(String(36), primary_key=True, default=new_id)
    project_id = Column(String(36), ForeignKey("business_projects.project_id", ondelete="RESTRICT"), nullable=False)
    room_id = Column(String(36), ForeignKey("business_rooms.room_id", ondelete="RESTRICT"), nullable=False)
    # check_in=到场签到; check_out=离场; change=换人; leave_permit=离岗许可; clarification=澄清事实
    fact_type = Column(String(16), nullable=False)
    person_id = Column(String(36), ForeignKey("business_persons.person_id", ondelete="RESTRICT"), nullable=True)
    seat_id = Column(String(36), ForeignKey("business_seats.seat_id", ondelete="RESTRICT"), nullable=True)
    phase_id = Column(String(64), nullable=True)  # 关联九阶段标识，如DETAILED
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    note = Column(Text, nullable=True)
    # 换人: {"replaced_person_id": str}; 澄清: {"question": str}; 其他类型可为空
    payload = Column(JSONB, nullable=True)
    actor = Column(String(64), nullable=False)  # 登记管理员用户名，登录身份留痕
    created_at = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint(FACT_TYPE_CHECK, name="business_fact_type"),
        ForeignKeyConstraint(["room_id", "project_id"], ["business_rooms.room_id", "business_rooms.project_id"], ondelete="RESTRICT", name="business_fact_room_project_fk"),
        ForeignKeyConstraint(["project_id", "person_id"], ["business_participants.project_id", "business_participants.person_id"], ondelete="RESTRICT", name="business_fact_membership_fk"),
        CheckConstraint("length(btrim(phase_id)) > 0", name="business_fact_phase_text"),
    )
    person = relationship("BusinessPerson", foreign_keys=[person_id])
    seat = relationship("BusinessSeat", foreign_keys=[seat_id])
    room = relationship("BusinessRoom", foreign_keys=[room_id])
