"""2.1阶段快照的校验、追加和按发生时间读取。"""
from datetime import datetime, timezone
from copy import deepcopy
from typing import Any
import uuid

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.business_models import BusinessProject, BusinessRoom
from app.models import EvalSession
from app.phase_models import PhaseBinding, PhaseSnapshot


PHASES = ("SIGNIN", "DISCIPLINE", "PRELIM", "DETAIL", "CLARIFY", "REPORT")
SCENES = {f"S{i:02}" for i in range(1, 11)}


class PhaseConfigError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PhaseConfigError("INVALID_TIME", "effective_at必须是带时区的UTC时间")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise PhaseConfigError("INVALID_TIME", "effective_at必须使用UTC")
    return value.astimezone(timezone.utc)


def _dict(row: PhaseSnapshot) -> dict[str, Any]:
    return {
        "contract_version": "2.0.0",
        "project_id": row.project_id,
        "room_id": row.room_id,
        "session_id": row.session_id,
        "source_id": row.source_id,
        "stage_snapshot_id": row.stage_snapshot_id,
        "phase_id": row.phase_id,
        "phase_version": row.phase_version,
        "effective_at": row.effective_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "enabled_scenes": list(row.enabled_scenes or []),
        "always_on_scenes": list(row.always_on_scenes or []),
        "assets": deepcopy(row.assets or []),
        "rule_versions": dict(row.rule_versions or {}),
        "exceptions": deepcopy(row.exceptions or []),
        "sync_status": row.sync_status,
    }


def _binding(db: Session, session_id: str, *, lock: bool = False) -> PhaseBinding | None:
    stmt = select(PhaseBinding).where(PhaseBinding.session_id == session_id)
    if lock:
        stmt = stmt.with_for_update()
    stmt = stmt.execution_options(populate_existing=True)
    return db.scalar(stmt)


def _require_session(db: Session, session_id: str):
    if db.scalar(select(EvalSession.session_id).where(EvalSession.session_id == session_id)) is None:
        raise PhaseConfigError('SESSION_NOT_FOUND', '场次不存在')


def _text(value, limit=256):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def bind_session(db: Session, *, session_id: str, project_id: str, room_id: str, source_id: str) -> dict:
    if not all(_text(value, 36) for value in (session_id, project_id, room_id)) or not _text(source_id):
        raise PhaseConfigError("INVALID_BINDING", "场次、项目、评标室和源ID不能为空")
    session = db.scalar(select(EvalSession).where(EvalSession.session_id == session_id).with_for_update().execution_options(populate_existing=True))
    if session is None:
        raise PhaseConfigError("SESSION_NOT_FOUND", "场次不存在")
    project = db.get(BusinessProject, project_id)
    room = db.get(BusinessRoom, room_id)
    if project is None:
        raise PhaseConfigError("PROJECT_NOT_FOUND", "项目不存在")
    if room is None:
        raise PhaseConfigError("ROOM_NOT_FOUND", "评标室不存在")
    if room.project_id != project_id:
        raise PhaseConfigError("ROOM_PROJECT_MISMATCH", "评标室不属于该项目")
    if not room.enabled:
        raise PhaseConfigError('ROOM_DISABLED', '停用评标室不能绑定场次')
    if project.status == 'closed':
        raise PhaseConfigError('PROJECT_CLOSED', '已关闭项目不能绑定场次')
    if session.status == "closed":
        raise PhaseConfigError("SESSION_CLOSED", "已关闭场次不能重新绑定")
    current = _binding(db, session_id, lock=True)
    if current is not None:
        if (current.project_id, current.room_id, current.source_id) != (project_id, room_id, source_id):
            raise PhaseConfigError("BINDING_CONFLICT", "场次已有不同业务绑定")
        return {"binding_id": current.binding_id, "session_id": session_id, "project_id": project_id, "room_id": room_id, "source_id": source_id, "active": current.active}
    row = PhaseBinding(binding_id=str(uuid.uuid4()), session_id=session_id, project_id=project_id, room_id=room_id, source_id=source_id, active=True)
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise PhaseConfigError("BINDING_CONFLICT", "场次绑定并发冲突，请重新读取") from exc
    return {"binding_id": row.binding_id, "session_id": session_id, "project_id": project_id, "room_id": room_id, "source_id": source_id, "active": row.active}


def _validate_input(*, phase_id, expected_version, enabled_scenes, rule_versions, effective_at, assets, exceptions, source_id):
    if phase_id not in PHASES:
        raise PhaseConfigError("INVALID_PHASE", "未知评标阶段，不能把旧review静默映射为OPENING")
    if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
        raise PhaseConfigError("INVALID_VERSION", "expected_version必须是非负整数")
    if not isinstance(enabled_scenes, list) or any(not isinstance(scene, str) or scene not in SCENES for scene in enabled_scenes) or len(set(enabled_scenes)) != len(enabled_scenes):
        raise PhaseConfigError("INVALID_CONFIG", "enabled_scenes只能是去重后的S01～S10")
    if not isinstance(rule_versions, dict) or "S10" not in rule_versions or any(scene not in rule_versions for scene in set(enabled_scenes) | {"S10"}):
        raise PhaseConfigError("INVALID_CONFIG", "每个启用场景及S10必须有规则版本")
    if any(scene not in SCENES or not _text(version) for scene, version in rule_versions.items()):
        raise PhaseConfigError("INVALID_CONFIG", "规则版本不能为空")
    effective_at = _utc(effective_at)
    if not isinstance(assets, list):
        raise PhaseConfigError("INVALID_CONFIG", "assets必须是列表")
    for asset in assets:
        if not isinstance(asset, dict) or set(asset) != {'slot', 'source_id', 'asset_id'}:
            raise PhaseConfigError('INVALID_CONFIG', '素材条目字段不符合契约')
        if not _text(asset['source_id']) or not _text(asset['asset_id']) or asset['slot'] not in ('video', 'audio', 'scoring'):
            raise PhaseConfigError('INVALID_CONFIG', '素材ID或槽位无效')
        if asset['source_id'] != source_id:
            raise PhaseConfigError('SOURCE_MISMATCH', '素材必须绑定当前源')
    if not isinstance(exceptions, list):
        raise PhaseConfigError('INVALID_CONFIG', '例外必须为事实引用列表')
    for fact in exceptions:
        if (not isinstance(fact, dict) or set(fact) != {'fact_id', 'fact_version'}
                or not _text(fact['fact_id']) or type(fact['fact_version']) is not int or fact['fact_version'] < 1):
            raise PhaseConfigError('INVALID_CONFIG', '例外事实引用或版本无效')
    return effective_at


def append_snapshot(db: Session, *, session_id: str, phase_id: str, expected_version: int, enabled_scenes: list[str], rule_versions: dict[str, str], actor_id: str, reason: str, effective_at: datetime | None = None, assets: list[dict] | None = None, exceptions: list[dict] | None = None, sync_status: str = "pending") -> dict:
    if not _text(actor_id, 128) or not _text(reason, 1024):
        raise PhaseConfigError("INVALID_AUDIT", "actor_id和reason不能为空")
    if sync_status != 'pending':
        raise PhaseConfigError("INVALID_CONFIG", "新配置只能处于待同步；应用状态由2.2消费者确认")
    session = db.scalar(select(EvalSession).where(EvalSession.session_id == session_id).with_for_update().execution_options(populate_existing=True))
    if session is None:
        raise PhaseConfigError("SESSION_NOT_FOUND", "场次不存在")
    if session.status == "closed":
        raise PhaseConfigError("SESSION_CLOSED", "已关闭场次不能切换阶段")
    binding = _binding(db, session_id, lock=True)
    if binding is None:
        raise PhaseConfigError("BINDING_REQUIRED", "必须先明确项目、评标室和媒体源")
    if not binding.active:
        raise PhaseConfigError('BINDING_INACTIVE', '场次业务绑定已停用')
    room = db.get(BusinessRoom, binding.room_id, populate_existing=True)
    project = db.get(BusinessProject, binding.project_id, populate_existing=True)
    if room is None or room.project_id != binding.project_id:
        raise PhaseConfigError('ROOM_PROJECT_MISMATCH', '业务绑定关系无效')
    if not room.enabled:
        raise PhaseConfigError('ROOM_DISABLED', '停用评标室不能切换阶段')
    if project is None or project.status == 'closed':
        raise PhaseConfigError('PROJECT_CLOSED', '项目不存在或已经关闭')
    assets = [] if assets is None else assets
    exceptions = [] if exceptions is None else exceptions
    effective_at = datetime.now(timezone.utc) if effective_at is None else effective_at
    effective_at = _validate_input(phase_id=phase_id, expected_version=expected_version, enabled_scenes=enabled_scenes, rule_versions=rule_versions, effective_at=effective_at, assets=assets, exceptions=exceptions, source_id=binding.source_id)
    latest = db.scalar(select(PhaseSnapshot).where(PhaseSnapshot.session_id == session_id).order_by(desc(PhaseSnapshot.phase_version)).with_for_update().execution_options(populate_existing=True))
    current_version = latest.phase_version if latest else 0
    if expected_version != current_version:
        raise PhaseConfigError("VERSION_CONFLICT", f"期望版本{expected_version}与当前版本{current_version}不一致")
    if latest is not None and effective_at <= latest.effective_at:
        raise PhaseConfigError("TIME_CONFLICT", "新快照生效时间必须晚于上一版本")
    row = PhaseSnapshot(stage_snapshot_id=str(uuid.uuid4()), session_id=session_id, project_id=binding.project_id, room_id=binding.room_id, source_id=binding.source_id, phase_id=phase_id, phase_version=current_version + 1, effective_at=effective_at, enabled_scenes=list(enabled_scenes), always_on_scenes=["S10"], assets=deepcopy(assets or []), rule_versions=dict(rule_versions), exceptions=deepcopy(exceptions or []), sync_status=sync_status, actor_id=actor_id.strip(), reason=reason.strip())
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise PhaseConfigError("VERSION_CONFLICT", "阶段快照并发冲突，请重新读取") from exc
    return _dict(row)


def get_latest_config(db: Session, session_id: str) -> dict | None:
    _require_session(db, session_id)
    row = db.scalar(select(PhaseSnapshot).where(PhaseSnapshot.session_id == session_id).order_by(desc(PhaseSnapshot.phase_version)))
    return _dict(row) if row else None


def list_timeline(db: Session, session_id: str) -> list[dict]:
    _require_session(db, session_id)
    rows = db.scalars(select(PhaseSnapshot).where(PhaseSnapshot.session_id == session_id).order_by(PhaseSnapshot.phase_version)).all()
    return [_dict(row) | {'actor_id': row.actor_id, 'reason': row.reason, 'created_at': row.created_at.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')} for row in rows]


def get_snapshot(session_id: str, occurred_at: datetime, *, db: Session) -> dict | None:
    _require_session(db, session_id)
    occurred_at = _utc(occurred_at)
    row = db.scalar(select(PhaseSnapshot).where(PhaseSnapshot.session_id == session_id, PhaseSnapshot.effective_at <= occurred_at).order_by(desc(PhaseSnapshot.effective_at), desc(PhaseSnapshot.phase_version)))
    return _dict(row) if row else None
