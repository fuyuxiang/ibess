"""基础配置序列化、数据库错误映射及一次性初始化。"""
from datetime import timezone

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.business_models import BusinessProject, BusinessRoom, RegistryBootstrap


def lock_registry(session):
    # 管理写事务很短；串行检查避免停用/移除成员与工位分配间的竞态。
    # 唯一键与外键仍由数据库独立兜底，锁在提交或回滚时自动释放。
    session.execute(text("SELECT pg_advisory_xact_lock(81234171)"))


def entity_dict(record):
    result = {}
    for column in record.__table__.columns:
        value = getattr(record, column.name)
        if column.name in ("start_at", "end_at") and value is not None:
            value = value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        result[column.name] = value
    return result


def commit_registry(session, deleting=False):
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        code = getattr(exc.orig, "sqlstate", None)
        if code == "23505":
            raise HTTPException(409, "编号、项目成员或项目内人员工位分配重复") from exc
        if code == "23503":
            raise HTTPException(409 if deleting else 422, "配置仍被子项、工位或历史事件引用" if deleting else "关联不存在或关联变更破坏已有引用") from exc
        raise HTTPException(422, "配置字段不符合数据库约束") from exc


def ensure_business_seed(engine):
    """在业务表创建之后调用；初始化标记与种子数据同一事务提交。"""
    with Session(engine) as session:
        lock_registry(session)
        created = session.execute(insert(RegistryBootstrap).values(key="business_registry_v1").on_conflict_do_nothing().returning(RegistryBootstrap.key)).scalar_one_or_none()
        if created is None:
            return
        project = BusinessProject(code="DEMO", name="业务演示项目", owner="演示见证员", status="running")
        session.add(project)
        session.flush()
        for code, name, mode in (
            ("A01", "评标室 A01", "centralized"), ("A02", "评标室 A02", "centralized"),
            ("B01", "异地专家组 B01", "remote"), ("A03", "评标室 A03", "centralized"),
            ("A04", "评标室 A04", "centralized"), ("A05", "评标室 A05", "centralized"),
        ):
            session.add(BusinessRoom(project_id=project.project_id, code=code, name=name, location="演示配置（视频未接入）", mode=mode, enabled=True))
        session.commit()
