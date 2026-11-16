"""单管理员身份、摘要会话及不含请求正文的管理审计。"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Integer, String, Text

from app.models import Base


class AdminAccount(Base):
    __tablename__ = "admin_accounts"
    account_id = Column(Integer, primary_key=True, default=1, autoincrement=False)
    username = Column(String(32), nullable=False, unique=True)
    display_name = Column(String(64), nullable=False)
    password_hash = Column(Text, nullable=False)
    __table_args__ = (
        CheckConstraint("account_id = 1", name="admin_account_singleton"),
        CheckConstraint("username ~ '^[A-Za-z0-9_]{3,32}$'", name="admin_username_format"),
        CheckConstraint("length(btrim(display_name)) > 0", name="admin_display_name_nonempty"),
    )


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    token_hash = Column(String(64), primary_key=True)
    account_id = Column(Integer, ForeignKey("admin_accounts.account_id", ondelete="CASCADE"), nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    csrf_token = Column(String(64), nullable=False)


class AdminAudit(Base):
    __tablename__ = "admin_audit"
    id = Column(Integer, primary_key=True, autoincrement=True)
    actor = Column(String(32), nullable=False)
    action = Column(String(64), nullable=False)
    path = Column(String(2048), nullable=False)
    method = Column(String(16), nullable=False)
    at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class TeamAccount(Base):
    """本单位值守、处置及查阅账号；原单管理员账号与会话保持兼容。"""
    __tablename__ = 'supervision_team_accounts'
    account_id = Column(String(36), primary_key=True)
    username = Column(String(32), nullable=False, unique=True)
    display_name = Column(String(64), nullable=False)
    password_hash = Column(Text, nullable=False)
    role = Column(String(16), nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    created_by = Column(String(32), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))
    __table_args__ = (CheckConstraint("role IN ('supervisor','operator','viewer')"),)


class TeamSession(Base):
    __tablename__ = 'supervision_team_sessions'
    token_hash = Column(String(64), primary_key=True)
    account_id = Column(String(36), ForeignKey('supervision_team_accounts.account_id',ondelete='CASCADE'), nullable=False,index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    csrf_token = Column(String(64), nullable=False)
