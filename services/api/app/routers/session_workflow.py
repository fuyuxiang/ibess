"""Business operations are separate from privileged monitoring configuration."""

from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints
from typing import Annotated
from sqlalchemy.orm import Session
from app.db import get_session
from app.models import EvalSession, Event
from app.phase_models import PhaseBinding
from app.session_workflow_models import SessionWorkflow
from app.application import session_workflow as flow
from app.application.auth_context import actor
from app.application.monitoring_profiles import profile_plans

router = APIRouter(prefix="/supervision/workflows", tags=["session-workflow"])


class VersionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0, strict=True)


class NoteIn(VersionIn):
    note: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=2, max_length=2000)
    ]


class ChecksIn(VersionIn):
    checks: dict[str, StrictBool]


class JournalIn(NoteIn):
    kind: str = Field(pattern="^(patrol|handover|supplement)$")
    event_id: UUID | None = None


class ArchiveIn(NoteIn):
    evidence_gap_note: str | None = Field(default=None, max_length=4000)


class PhaseIn(NoteIn):
    phase: str


class PlanIn(VersionIn):
    phase_plans: dict[str, list[str]]


class IdentitySettingIn(VersionIn):
    enabled: StrictBool


class CreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=2, max_length=128)
    ]
    project_id: UUID
    room_id: UUID
    source_id: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"
    )
    preset_id: UUID | None = None
    scheme: str | None = Field(default=None, pattern=r"^(basic|standard)$")
    expected_people: int = Field(default=0, ge=0, le=1000)


def consumers(request):
    values = getattr(request.app.state, "phase_control_required_consumers", None)
    if not values:
        flow.fail(
            "CONTROL_NOT_CONFIGURED",
            "阶段同步尚未接入，请联系平台管理员检查部署配置",
            503,
        )
    return values


def apply_phase(db, session, book, phase, who, note, required):
    from app import phase_config, phase_control

    if phase not in flow.DEFAULT_PLANS:
        flow.fail("INVALID_PHASE", "请选择有效评标阶段", 422)
    binding = db.query(PhaseBinding).filter_by(session_id=session.session_id).one()
    latest = (
        db.query(flow.PhaseSnapshot)
        .filter_by(session_id=session.session_id)
        .order_by(flow.PhaseSnapshot.phase_version.desc())
        .first()
    )
    scenes = list((book.phase_plans or flow.DEFAULT_PLANS)[phase])
    version = latest.phase_version if latest else 0
    rules = {
        s: (latest.rule_versions or {}).get(s, s.lower() + "-v1")
        if latest
        else s.lower() + "-v1"
        for s in scenes
    }
    try:
        snapshot = phase_config.append_snapshot(
            db,
            session_id=session.session_id,
            phase_id=phase,
            expected_version=version,
            enabled_scenes=scenes,
            rule_versions=rules,
            reason=note,
            actor_id=who,
            assets=latest.assets if latest else [],
            exceptions=latest.exceptions if latest else [],
        )
        phase_control.enqueue_snapshot(
            db, snapshot["stage_snapshot_id"], required_consumers=required
        )
    except (phase_config.PhaseConfigError, phase_control.PhaseControlError) as exc:
        flow.fail(exc.code, str(exc))
    session.current_phase = phase
    flow.journal(
        db,
        session,
        book,
        "phase",
        note,
        who,
        {
            "phase_id": phase,
            "phase_version": snapshot["phase_version"],
            "stage_snapshot_id": snapshot["stage_snapshot_id"],
        },
    )


@router.get("")
def directory(
    keyword: str = Query("", max_length=128),
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=50),
    db: Session = Depends(get_session),
):
    from sqlalchemy import or_
    from app.business_models import BusinessProject, BusinessRoom

    query = (
        db.query(
            EvalSession, PhaseBinding, BusinessProject, BusinessRoom, SessionWorkflow
        )
        .outerjoin(PhaseBinding, PhaseBinding.session_id == EvalSession.session_id)
        .outerjoin(
            BusinessProject, BusinessProject.project_id == PhaseBinding.project_id
        )
        .outerjoin(BusinessRoom, BusinessRoom.room_id == PhaseBinding.room_id)
        .outerjoin(
            SessionWorkflow, SessionWorkflow.session_id == EvalSession.session_id
        )
    )
    if keyword.strip():
        term = (
            "%"
            + keyword.strip()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
            + "%"
        )
        query = query.filter(
            or_(
                *(
                    column.ilike(term, escape="\\")
                    for column in (
                        EvalSession.name,
                        BusinessProject.name,
                        BusinessRoom.name,
                    )
                )
            )
        )
    if status in ("draft", "running", "closed"):
        query = query.filter(EvalSession.status == status)
    total = query.count()
    items = []
    for session, binding, project, room, book in (
        query.order_by(EvalSession.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ):
        rows = flow.event_rows(db, session.session_id)
        items.append(
            {
                "session_id": session.session_id,
                "name": session.name,
                "status": session.status,
                "lifecycle": flow.state(
                    session,
                    book,
                    flow.fingerprint(db, session, rows)
                    if book and book.archived_report_id
                    else None,
                ),
                "project_name": project.name if project else "尚未关联项目",
                "room_name": room.name if room else "尚未绑定评标室",
                "room_id": room.room_id if room else None,
                "phase_id": session.current_phase,
                "started_at": flow.iso_utc(session.start_at),
                "ended_at": flow.iso_utc(session.end_at),
                "pending_events": sum(
                    (c.status if c else flow.legacy_status(e)) != "ARCHIVED"
                    for e, c, _, _ in rows
                ),
                "total_events": len(rows),
            }
        )
    counts = dict(
        db.query(EvalSession.status, flow.func.count(EvalSession.id))
        .group_by(EvalSession.status)
        .all()
    )
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "summary": {
            "preparing": counts.get("draft", 0),
            "running": counts.get("running", 0),
            "closed": counts.get("closed", 0),
        },
    }


@router.post("", status_code=201)
def create(body: CreateIn, request: Request, db: Session = Depends(get_session)):
    from app import phase_config

    who = actor(request)
    from app.business_models import BusinessRoom
    from app.routers.room_monitoring import find_preset, serialize
    from app.zone_config_models import SourceZoneConfig
    from app.supervision_models import SupervisionChannel

    room = (
        db.query(BusinessRoom)
        .filter_by(room_id=str(body.room_id))
        .with_for_update()
        .first()
    )
    if not room or room.project_id != str(body.project_id) or not room.enabled:
        flow.fail("INVALID_ROOM", "请选择所属项目下的启用评标室", 422)
    preset = find_preset(db, body.preset_id, room) if body.preset_id else None
    source = preset.primary_source_id if preset else body.source_id
    if not source or (preset and body.source_id and source != body.source_id):
        flow.fail("SOURCE_REQUIRED", "请选择现场资源版本或明确配置主视频通道", 422)
    scheme = body.scheme or (preset.scheme if preset else "basic")
    row = EvalSession(
        session_id=str(uuid4()),
        name=body.name.strip(),
        status="draft",
        current_phase="SIGNIN",
        expected_people=body.expected_people,
    )
    db.add(row)
    db.flush()
    try:
        phase_config.bind_session(
            db,
            session_id=row.session_id,
            project_id=str(body.project_id),
            room_id=str(body.room_id),
            source_id=source,
        )
    except phase_config.PhaseConfigError as exc:
        flow.fail(exc.code, str(exc), 422)
    book = SessionWorkflow(
        session_id=row.session_id,
        version=0,
        checks={},
        phase_plans=profile_plans(scheme),
        preparation_snapshot={
            **(serialize(preset) if preset else {}),
            "scheme": scheme,
            "profile_version": scheme + "-v1",
        },
    )
    db.add(book)
    if preset:
        for channel in preset.channels:
            if channel["source_id"] != source:
                db.add(
                    SupervisionChannel(
                        channel_id=str(uuid4()),
                        session_id=row.session_id,
                        source_id=channel["source_id"],
                        name=channel["name"],
                        kind=channel["kind"],
                        actor=who,
                        active=True,
                    )
                )
            if channel["kind"] == "video" and (
                channel.get("zones") or channel.get("entry_lines")
            ):
                db.add(
                    SourceZoneConfig(
                        config_id=str(uuid4()),
                        session_id=row.session_id,
                        project_id=room.project_id,
                        room_id=room.room_id,
                        source_id=channel["source_id"],
                        config_version=1,
                        zones=deepcopy(channel["zones"]),
                        entry_lines=deepcopy(channel["entry_lines"]),
                    )
                )
    flow.journal(db, row, book, "preparation", "建立监管场次并绑定现场范围", who)
    required = getattr(request.app.state, "phase_control_required_consumers", None)
    if required:
        apply_phase(db, row, book, "SIGNIN", who, "应用初始入场监管方案", required)
    db.commit()
    return flow.detail(db, row.session_id)


@router.get("/{session_id}")
def read(session_id: str, db: Session = Depends(get_session)):
    return flow.detail(db, session_id)


@router.post("/{session_id}/adopt")
def adopt(session_id: str, request: Request, db: Session = Depends(get_session)):
    session, book = flow.load(db, session_id, lock=True, managed=False)
    if book:
        return flow.detail(db, session_id)
    if not db.query(PhaseBinding).filter_by(session_id=session_id).first():
        flow.fail(
            "SCOPE_REQUIRED",
            "此历史场次缺少监管范围，请继续查阅原始事件档案，暂不能纳入场次归档流程",
        )
    book = SessionWorkflow(
        session_id=session_id,
        version=0,
        checks={},
        phase_plans=deepcopy(flow.DEFAULT_PLANS),
        identity_verification_enabled=session.status in ("running", "closed"),
    )
    db.add(book)
    flow.journal(
        db,
        session,
        book,
        "preparation",
        "将已有场次纳入监管流程，保留原始记录",
        actor(request),
    )
    db.commit()
    return flow.detail(db, session_id)


@router.put("/{session_id}/checks")
def checks(
    session_id: str,
    body: ChecksIn,
    request: Request,
    db: Session = Depends(get_session),
):
    session, book = flow.mutate(db, session_id, body.expected_version)
    if not body.checks or set(body.checks) - set(flow.CHECKS):
        flow.fail("INVALID_CHECK", "准备核验项无效", 422)
    allowed = (
        flow.START_CHECKS + flow.IDENTITY_CHECKS
        if session.status == "draft"
        else flow.CLOSE_CHECKS
        if session.status == "closed"
        else ()
    )
    if set(body.checks) - set(allowed):
        flow.fail("INVALID_STATE", "当前阶段不能修改这些核验项")
    values = deepcopy(book.checks or {})
    who = actor(request)
    for key, value in body.checks.items():
        values[key] = {
            "confirmed": value,
            "actor": who,
            "at": flow.iso_utc(datetime.utcnow()),
        }
    book.checks = values
    flow.journal(db, session, book, "preparation", "更新现场核验记录", who, body.checks)
    db.commit()
    return flow.detail(db, session_id)


@router.put("/{session_id}/identity-setting")
def identity_setting(
    session_id: str,
    body: IdentitySettingIn,
    request: Request,
    db: Session = Depends(get_session),
):
    if getattr(request.state, "admin", {}).get("role") != "admin":
        raise HTTPException(403, "身份核查选项由管理员在评前配置")
    session, book = flow.mutate(db, session_id, body.expected_version)
    if session.status != "draft":
        flow.fail("INVALID_STATE", "身份核查选项仅可在开始监管前修改")
    if book.identity_verification_enabled != body.enabled:
        book.identity_verification_enabled = body.enabled
        if (book.preparation_snapshot or {}).get("scheme") == "basic":
            plans = deepcopy(book.phase_plans)
            for scenes in plans.values():
                if body.enabled and "S04" not in scenes:
                    scenes.append("S04")
                elif not body.enabled and "S04" in scenes:
                    scenes.remove("S04")
            book.phase_plans = plans
            required = getattr(
                request.app.state, "phase_control_required_consumers", None
            )
            if required:
                apply_phase(
                    db,
                    session,
                    book,
                    "SIGNIN",
                    actor(request),
                    "同步本场身份核查选项",
                    required,
                )
        flow.journal(
            db,
            session,
            book,
            "preparation",
            "启用本场人员身份核查"
            if body.enabled
            else "关闭本场人员身份核查，使用基础行为监管",
            actor(request),
            {"identity_verification_enabled": body.enabled},
        )
    db.commit()
    return flow.detail(db, session_id)


class PreparationIn(VersionIn):
    scheme: str = Field(pattern=r"^(basic|standard)$")
    preset_id: UUID | None = None


@router.put("/{session_id}/preparation")
def preparation(
    session_id: str,
    body: PreparationIn,
    request: Request,
    db: Session = Depends(get_session),
):
    if getattr(request.state, "admin", {}).get("role") != "admin":
        raise HTTPException(403, "监测方案由管理员维护")
    session, book = flow.mutate(db, session_id, body.expected_version)
    if session.status != "draft":
        flow.fail("INVALID_STATE", "开始监管后不能修改整场监测方案")
    previous_plans = deepcopy(book.phase_plans)
    previous_preparation = deepcopy(book.preparation_snapshot)
    if body.preset_id:
        from app.routers.room_monitoring import find_preset, serialize
        from app.business_models import BusinessRoom
        from app.zone_config_models import SourceZoneConfig

        binding = db.query(PhaseBinding).filter_by(session_id=session_id).one()
        room = (
            db.query(BusinessRoom)
            .filter_by(room_id=binding.room_id)
            .with_for_update()
            .one()
        )
        preset = find_preset(db, body.preset_id, room)
        current = {
            c["source_id"]: c["kind"]
            for c in flow.session_channels(db, session_id)
            if c["active"]
        }
        if (
            preset.primary_source_id != binding.source_id
            or {c["source_id"]: c["kind"] for c in preset.channels} != current
        ):
            flow.fail(
                "RESOURCE_SCOPE_CHANGED", "资源通道已变化，请新建场次以独立保留监控范围"
            )
        old = []
        for c in preset.channels:
            if c["kind"] != "video":
                continue
            zone = (
                db.query(SourceZoneConfig)
                .filter_by(session_id=session_id, source_id=c["source_id"])
                .first()
            )
            if zone:
                old.append(
                    {
                        "source_id": zone.source_id,
                        "config_version": zone.config_version,
                        "zones": deepcopy(zone.zones),
                        "entry_lines": deepcopy(zone.entry_lines),
                    }
                )
                zone.config_version += 1
                zone.zones, zone.entry_lines = (
                    deepcopy(c["zones"]),
                    deepcopy(c["entry_lines"]),
                )
            elif c["zones"] or c["entry_lines"]:
                db.add(
                    SourceZoneConfig(
                        config_id=str(uuid4()),
                        session_id=session_id,
                        project_id=room.project_id,
                        room_id=room.room_id,
                        source_id=c["source_id"],
                        config_version=1,
                        zones=deepcopy(c["zones"]),
                        entry_lines=deepcopy(c["entry_lines"]),
                    )
                )
        flow.journal(
            db,
            session,
            book,
            "preparation",
            "显式载入现场资源的新版本，保留原配置",
            actor(request),
            {
                "previous": deepcopy(book.preparation_snapshot),
                "previous_zones": old,
                "preset_id": preset.preset_id,
            },
        )
        book.preparation_snapshot = serialize(preset)
    plans = profile_plans(body.scheme)
    if book.identity_verification_enabled:
        for scenes in plans.values():
            if "S04" not in scenes:
                scenes.append("S04")
    book.phase_plans = plans
    book.preparation_snapshot = {
        **(book.preparation_snapshot or {}),
        "scheme": body.scheme,
        "profile_version": body.scheme + "-v1",
    }
    flow.journal(
        db,
        session,
        book,
        "preparation",
        "保存本场监测方案版本",
        actor(request),
        {
            "previous_plans": previous_plans,
            "phase_plans": deepcopy(plans),
            "previous_preparation": previous_preparation,
            "preparation": deepcopy(book.preparation_snapshot),
        },
    )
    apply_phase(
        db,
        session,
        book,
        "SIGNIN",
        actor(request),
        "保存并应用本场监测方案",
        consumers(request),
    )
    db.commit()
    return flow.detail(db, session_id)


@router.post("/{session_id}/start")
def start(
    session_id: str,
    body: VersionIn,
    request: Request,
    db: Session = Depends(get_session),
):
    flow.start(db, session_id, body.expected_version, actor(request))
    db.commit()
    return flow.detail(db, session_id)


@router.post("/{session_id}/phase")
def phase(
    session_id: str, body: PhaseIn, request: Request, db: Session = Depends(get_session)
):
    session, book = flow.mutate(db, session_id, body.expected_version)
    if session.status not in ("draft", "running"):
        flow.fail("INVALID_STATE", "已结束的现场不能切换阶段")
    if session.status == "running":
        ids = [p for p, _ in flow.PHASES]
        current = (
            ids.index(session.current_phase) if session.current_phase in ids else -1
        )
        if current < 0 or current + 1 >= len(ids) or body.phase != ids[current + 1]:
            flow.fail("INVALID_PHASE_TRANSITION", "值守工作区仅允许按顺序推进下一阶段")
    elif body.phase != "SIGNIN":
        flow.fail("INVALID_PHASE_TRANSITION", "准备阶段请先应用入场监管方案")
    apply_phase(
        db, session, book, body.phase, actor(request), body.note, consumers(request)
    )
    db.commit()
    return flow.detail(db, session_id)


@router.put("/{session_id}/plan")
def plans(
    session_id: str, body: PlanIn, request: Request, db: Session = Depends(get_session)
):
    session, book = flow.mutate(db, session_id, body.expected_version)
    if session.status != "draft":
        flow.fail("INVALID_STATE", "开始监管后不可修改整场监测方案")
    if set(body.phase_plans) != set(flow.DEFAULT_PLANS):
        flow.fail("INVALID_PLAN", "请为六个阶段完整设置监测方案", 422)
    for scenes in body.phase_plans.values():
        if (
            not scenes
            or "S10" not in scenes
            or len(scenes) != len(set(scenes))
            or set(scenes) - {f"S{i:02}" for i in range(1, 11)}
        ):
            flow.fail("INVALID_PLAN", "场景配置无效，S10须全程启用", 422)
    previous_plans = deepcopy(book.phase_plans)
    book.phase_plans = body.phase_plans
    if book.preparation_snapshot:
        book.preparation_snapshot = {**book.preparation_snapshot, "scheme": "custom"}
    flow.journal(
        db,
        session,
        book,
        "preparation",
        "更新场次分阶段监测方案",
        actor(request),
        {"previous_plans": previous_plans, "phase_plans": deepcopy(body.phase_plans)},
    )
    db.commit()
    return flow.detail(db, session_id)


@router.post("/{session_id}/journal", status_code=201)
def add_journal(
    session_id: str,
    body: JournalIn,
    request: Request,
    db: Session = Depends(get_session),
):
    session, book = flow.mutate(db, session_id, body.expected_version)
    if body.kind in ("patrol", "handover") and session.status != "running":
        flow.fail("INVALID_STATE", "巡查和交接记录仅在现场监管期间登记")
    if (
        body.event_id
        and not db.query(Event)
        .filter_by(event_id=str(body.event_id), session_id=session_id)
        .first()
    ):
        flow.fail("EVENT_SCOPE_MISMATCH", "所关联事件不属于当前场次", 422)
    flow.journal(
        db,
        session,
        book,
        body.kind,
        body.note,
        actor(request),
        {"event_id": str(body.event_id) if body.event_id else None},
    )
    db.commit()
    return flow.detail(db, session_id)


@router.post("/{session_id}/finish")
def finish(
    session_id: str, body: NoteIn, request: Request, db: Session = Depends(get_session)
):
    flow.finish(db, session_id, body.expected_version, actor(request), body.note)
    db.commit()
    return flow.detail(db, session_id)


@router.post("/{session_id}/archive", status_code=201)
def archive(
    session_id: str,
    body: ArchiveIn,
    request: Request,
    db: Session = Depends(get_session),
):
    row = flow.archive(
        db,
        session_id,
        body.expected_version,
        actor(request),
        body.note,
        body.evidence_gap_note,
    )
    db.commit()
    return {"report_id": row.report_id, "workflow": flow.detail(db, session_id)}


class PermitIn(NoteIn):
    note: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=2, max_length=500)
    ]
    authorization_kind: str = Field(
        pattern="^(contact_allowed|document_access|photo_allowed|device_allowed|break)$"
    )
    valid_until: datetime
    seat_id: UUID | None = None


@router.post("/{session_id}/permit", status_code=201)
def permit(
    session_id: str,
    body: PermitIn,
    request: Request,
    db: Session = Depends(get_session),
):
    from app.routers.business_facts import FactIn, save_fact
    from app import phase_config, phase_control

    session, book = flow.mutate(db, session_id, body.expected_version)
    if session.status != "running":
        flow.fail("INVALID_STATE", "临时业务许可仅在现场监管期间登记")
    required = consumers(request)
    binding = db.query(PhaseBinding).filter_by(session_id=session_id).one()
    latest = (
        db.query(flow.PhaseSnapshot)
        .filter_by(session_id=session_id)
        .order_by(flow.PhaseSnapshot.phase_version.desc())
        .first()
    )
    if not latest:
        flow.fail("PHASE_REQUIRED", "当前场次没有生效阶段")
    if body.valid_until.tzinfo is None or body.valid_until <= datetime.now(
        timezone.utc
    ):
        flow.fail("INVALID_PERMIT", "请设置晚于当前时间且带时区的许可有效期", 422)
    fact_body = FactIn(
        fact_type="clarification",
        project_id=binding.project_id,
        room_id=binding.room_id,
        seat_id=body.seat_id,
        phase_id=latest.phase_id,
        note=body.note,
        payload={
            "authorization_kind": body.authorization_kind,
            "valid_until": body.valid_until.astimezone(timezone.utc).isoformat(),
        },
    )
    who = actor(request)
    fact = save_fact(fact_body, db, who, commit=False)
    try:
        snapshot = phase_config.append_snapshot(
            db,
            session_id=session_id,
            phase_id=latest.phase_id,
            expected_version=latest.phase_version,
            enabled_scenes=latest.enabled_scenes,
            rule_versions=latest.rule_versions,
            assets=latest.assets,
            exceptions=[
                *(latest.exceptions or []),
                {"fact_id": fact["fact_id"], "fact_version": 1},
            ],
            reason="登记临时业务许可：" + body.note,
            actor_id=who,
        )
        phase_control.enqueue_snapshot(
            db, snapshot["stage_snapshot_id"], required_consumers=required
        )
    except (phase_config.PhaseConfigError, phase_control.PhaseControlError) as exc:
        flow.fail(exc.code, str(exc))
    flow.journal(
        db,
        session,
        book,
        "permit",
        body.note,
        who,
        {
            "fact_id": fact["fact_id"],
            "authorization_kind": body.authorization_kind,
            "valid_until": body.valid_until.isoformat(),
            "seat_id": str(body.seat_id) if body.seat_id else None,
            "stage_snapshot_id": snapshot["stage_snapshot_id"],
        },
    )
    db.commit()
    return flow.detail(db, session_id)
