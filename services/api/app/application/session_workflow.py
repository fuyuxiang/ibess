"""Operational session workflow; recognition and evidence pipelines remain authoritative."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, text
from app.models import EvalSession, Event
from app.business_models import BusinessProject, BusinessRoom
from app.business_authz_models import (
    BusinessAuthorizationSnapshot,
    BusinessAuthorizationEntry,
)
from app.phase_models import PhaseBinding, PhaseSnapshot
from app.supervision_models import (
    SupervisionCase,
    SupervisionMediaHold,
    SupervisionEventSpan,
    SupervisionSessionReport,
)
from app.session_workflow_models import SessionWorkflow, SessionJournal
from app.zone_config_models import SourceZoneConfig
from app.core.supervision_scope import normalize_source
from app.core.supervision_evidence import session_channels
from app.core.supervision import legacy_status
from app.application.monitoring_profiles import PHASES, DEFAULT_PLANS, PROFILE_LABELS
from app.core.workflow import iso_utc

CHECKS = {
    "room_inspected": "场地及设备巡检",
    "identity_checked": "入场身份与名单核对",
    "devices_stored": "通讯设备寄存与应急联络",
    "materials_reconciled": "资料与物品清点交接",
    "room_cleared": "人员离场与场地复核",
}
START_CHECKS = ("room_inspected", "devices_stored")
IDENTITY_CHECKS = ("identity_checked",)
CLOSE_CHECKS = ("materials_reconciled", "room_cleared")
FINALIZE_SECONDS = 90


def fail(code, message, status=409, **details):
    raise HTTPException(status, {"code": code, "message": message, **details})


def load(db, sid, *, lock=False, managed=True):
    query = db.query(EvalSession).filter_by(session_id=sid)
    session = (query.with_for_update() if lock else query).first()
    if not session:
        fail("SESSION_NOT_FOUND", "场次不存在", 404)
    book = db.get(SessionWorkflow, sid)
    if managed and not book:
        fail("WORKFLOW_REQUIRED", "请由管理员将此场次纳入监管流程")
    return session, book


def mutate(db, sid, expected):
    session, book = load(db, sid, lock=True)
    if book.version != expected:
        fail("VERSION_CONFLICT", "场次记录已更新，请刷新后再操作")
    return session, book


def journal(db, session, book, kind, note, who, detail=None):
    book.version += 1
    book.updated_at = datetime.utcnow()
    row = SessionJournal(
        journal_id=str(uuid4()),
        session_id=session.session_id,
        kind=kind,
        note=note,
        actor=who,
        detail=detail or {},
        created_at=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    return row


def event_rows(db, sid):
    return (
        db.query(Event, SupervisionCase, SupervisionMediaHold, SupervisionEventSpan)
        .outerjoin(SupervisionCase, SupervisionCase.event_id == Event.event_id)
        .outerjoin(
            SupervisionMediaHold, SupervisionMediaHold.event_id == Event.event_id
        )
        .outerjoin(
            SupervisionEventSpan, SupervisionEventSpan.event_id == Event.event_id
        )
        .filter(
            Event.session_id == sid, Event.risk_level.in_(["low", "medium", "high"])
        )
        .order_by(Event.event_id)
        .all()
    )


def fingerprint(db, session, rows=None):
    rows = rows if rows is not None else event_rows(db, session.session_id)
    payload = {
        "session": [
            session.status,
            session.current_phase,
            iso_utc(session.start_at),
            iso_utc(session.end_at),
        ],
        "journal": [
            [j.journal_id, j.kind, j.note, j.detail]
            for j in db.query(SessionJournal)
            .filter(
                SessionJournal.session_id == session.session_id,
                SessionJournal.kind != "archive",
            )
            .order_by(SessionJournal.journal_id)
        ],
        "events": [
            [
                e.event_id,
                e.reason,
                e.status,
                iso_utc(e.updated_at),
                c.status if c else None,
                c.version if c else None,
                hold.status if hold else None,
                hold.manifest if hold else None,
                [iso_utc(span.last_seen_at), span.trigger_count, span.active]
                if span
                else None,
            ]
            for e, c, hold, span in rows
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def state(session, book, current_fingerprint=None):
    if not book:
        return "unmanaged"
    if session.status == "draft":
        return "preparing"
    if session.status == "running":
        return "running"
    if book.archived_report_id:
        return (
            "archived"
            if book.archived_fingerprint == current_fingerprint
            else "supplement_required"
        )
    return "closing"


def occupied(db, session, binding, channels):
    """A running source must resolve to exactly one session, including auxiliary channels."""
    active = (
        db.query(PhaseBinding)
        .join(EvalSession, EvalSession.session_id == PhaseBinding.session_id)
        .filter(
            EvalSession.status == "running",
            PhaseBinding.active.is_(True),
            PhaseBinding.session_id != session.session_id,
        )
        .all()
    )
    sources = {
        normalize_source(c["source_id"]) for c in channels if c.get("active", True)
    }
    for other in active:
        if binding and other.room_id == binding.room_id:
            return True
        if sources & {
            normalize_source(c["source_id"])
            for c in session_channels(db, other.session_id)
            if c.get("active", True)
        }:
            return True
    return False


def readiness(db, session, book):
    identity_enabled = bool(book and book.identity_verification_enabled)
    binding = db.query(PhaseBinding).filter_by(session_id=session.session_id).first()
    project = db.get(BusinessProject, binding.project_id) if binding else None
    room = db.get(BusinessRoom, binding.room_id) if binding else None
    phase = (
        db.query(PhaseSnapshot)
        .filter_by(session_id=session.session_id)
        .filter(PhaseSnapshot.effective_at <= datetime.now(timezone.utc))
        .order_by(PhaseSnapshot.phase_version.desc())
        .first()
    )
    roster = (
        db.query(BusinessAuthorizationSnapshot)
        .filter_by(
            project_id=binding.project_id, room_id=binding.room_id, status="active"
        )
        .order_by(BusinessAuthorizationSnapshot.created_at.desc())
        .first()
        if binding
        else None
    )
    entries = (
        db.query(BusinessAuthorizationEntry)
        .filter_by(snapshot_id=roster.snapshot_id)
        .all()
        if roster
        else []
    )
    channels = session_channels(db, session.session_id) if binding else []
    video_ids = {
        normalize_source(c["source_id"])
        for c in channels
        if c["kind"] == "video" and c.get("active", True)
    }
    zones = db.query(SourceZoneConfig).filter_by(session_id=session.session_id).all()
    zones = [z for z in zones if normalize_source(z.source_id) in video_ids]
    seat_count = sum(sum(z.get("kind") == "seat" for z in c.zones) for c in zones)
    entrance_count = sum(len(c.entry_lines) for c in zones)
    scenes = {
        scene
        for plan in ((book.phase_plans if book else None) or DEFAULT_PLANS).values()
        for scene in plan
    }
    needs_seats = "S03" in scenes
    needs_entry = identity_enabled or "S04" in scenes
    checks = [
        {
            "key": "availability",
            "label": "评标室与监测通道可用性",
            "passed": not occupied(db, session, binding, channels),
            "action": "/sessions",
        },
        {
            "key": "binding",
            "label": "项目与评标室绑定",
            "passed": bool(
                binding
                and binding.active
                and project
                and project.status != "closed"
                and room
                and room.enabled
            ),
            "action": "/admin/sessions",
        },
        {
            "key": "roster",
            "label": "到场名单与计划人数",
            "passed": bool(entries)
            and (
                not session.expected_people or len(entries) == session.expected_people
            ),
            "required": identity_enabled,
            "action": "/admin/sessions?setup=people",
        },
        {
            "key": "identity",
            "label": "名单人员身份核验",
            "passed": bool(entries) and all(e.identity_verified for e in entries),
            "required": identity_enabled,
            "action": "/admin/sessions?setup=people",
        },
        {
            "key": "video",
            "label": "视频机位与监测范围",
            "passed": bool(video_ids),
            "action": "/admin/sessions",
        },
        {
            "key": "seat_zones",
            "label": "工位区域",
            "passed": bool(seat_count),
            "required": needs_seats,
            "action": "/admin/sessions",
        },
        {
            "key": "entry_lines",
            "label": "出入口线",
            "passed": bool(entrance_count),
            "required": needs_entry,
            "action": "/admin/sessions",
        },
        {
            "key": "phase",
            "label": "初始阶段与监测方案",
            "passed": bool(
                phase
                and (
                    session.status != "draft"
                    or phase.phase_id == "SIGNIN"
                    and (
                        not book
                        or set(phase.enabled_scenes)
                        == set((book.phase_plans or DEFAULT_PLANS)["SIGNIN"])
                    )
                )
            ),
            "action": "/admin/sessions",
        },
    ]
    for key in START_CHECKS + (IDENTITY_CHECKS if identity_enabled else ()):
        checks.append(
            {
                "key": key,
                "label": CHECKS[key],
                "passed": bool((book.checks or {}).get(key, {}).get("confirmed"))
                if book
                else False,
                "manual": True,
            }
        )
    return {
        "items": checks,
        "ready": all(c["passed"] for c in checks if c.get("required", True)),
        "roster_count": len(entries),
        "seat_count": seat_count,
        "entrance_count": entrance_count,
        "phase": phase,
        "binding": binding,
        "project": project,
        "room": room,
        "channels": channels,
    }


def detail(db, sid):
    session, book = load(db, sid, managed=False)
    ready = readiness(db, session, book)
    rows = event_rows(db, sid)
    pending = sum(
        (c.status if c else legacy_status(e)) != "ARCHIVED" for e, c, _, _ in rows
    )
    media_missing = sum(not h or h.status != "READY" for _, _, h, _ in rows)
    signature = (
        fingerprint(db, session, rows) if book and book.archived_report_id else None
    )
    archive_checks = []
    if session.status == "closed":
        wait = (
            max(
                0,
                int(
                    FINALIZE_SECONDS
                    - (datetime.utcnow() - session.end_at).total_seconds()
                )
                + 1,
            )
            if session.end_at
            else FINALIZE_SECONDS
        )
        archive_checks = [
            {
                "key": "settled",
                "label": "资料收集等待（90 秒）",
                "passed": wait == 0,
                "wait_seconds": wait,
            },
            {"key": "events", "label": "告警核查办结", "passed": pending == 0},
            *[
                {
                    "key": key,
                    "label": CHECKS[key],
                    "passed": bool((book.checks or {}).get(key, {}).get("confirmed"))
                    if book
                    else False,
                    "manual": True,
                }
                for key in CLOSE_CHECKS
            ],
        ]
    from app.phase_control import get_control_status

    try:
        control = get_control_status(db, sid) if ready["phase"] else None
    except ValueError:
        control = None
    logs = (
        db.query(SessionJournal)
        .filter_by(session_id=sid)
        .order_by(SessionJournal.created_at.desc())
        .limit(100)
        .all()
    )
    phase = ready["phase"]
    return {
        "session_id": sid,
        "name": session.name,
        "status": session.status,
        "lifecycle": state(session, book, signature),
        "version": book.version if book else 0,
        "managed": book is not None,
        "identity_verification_enabled": bool(
            book and book.identity_verification_enabled
        ),
        "preparation_snapshot": deepcopy(book.preparation_snapshot) if book else None,
        "monitoring_scheme": (book.preparation_snapshot or {}).get("scheme", "custom")
        if book
        else "custom",
        "monitoring_scheme_label": PROFILE_LABELS.get(
            (book.preparation_snapshot or {}).get("scheme"), "既有监测方案"
        )
        if book
        else "既有监测方案",
        "project_id": ready["project"].project_id if ready["project"] else None,
        "project_name": ready["project"].name if ready["project"] else "尚未关联项目",
        "room_id": ready["room"].room_id if ready["room"] else None,
        "room_name": ready["room"].name if ready["room"] else "尚未绑定评标室",
        "phase_id": phase.phase_id if phase else session.current_phase,
        "phase_version": phase.phase_version if phase else 0,
        "started_at": iso_utc(session.start_at),
        "ended_at": iso_utc(session.end_at),
        "started_by": book.started_by if book else None,
        "archived_report_id": book.archived_report_id if book else None,
        "archived_at": iso_utc(book.archived_at) if book else None,
        "checks": book.checks or {} if book else {},
        "readiness": {
            "items": ready["items"],
            "ready": ready["ready"],
            "roster_count": ready["roster_count"],
            "seat_count": ready["seat_count"],
            "entrance_count": ready["entrance_count"],
        },
        "channels": [
            {**c, "created_at": iso_utc(c.get("created_at"))} for c in ready["channels"]
        ],
        "pending_events": pending,
        "total_events": len(rows),
        "media_pending": media_missing,
        "archive_checks": archive_checks,
        "control": control,
        "phases": [
            {
                "id": p,
                "title": t,
                "scenes": ((book.phase_plans if book else None) or DEFAULT_PLANS).get(
                    p, []
                ),
            }
            for p, t in PHASES
        ],
        "journal": [
            {
                "journal_id": j.journal_id,
                "kind": j.kind,
                "note": j.note,
                "actor": j.actor,
                "at": iso_utc(j.created_at),
                "detail": j.detail,
            }
            for j in logs
        ],
        "runtime_notice": "开始监管后，请核对画面、录像和分析状态。",
    }


def start(db, sid, expected, who):
    session, book = mutate(db, sid, expected)
    if session.status != "draft":
        fail("INVALID_STATE", "只有准备中的场次可以开始监管")
    # Serialize start checks across rooms as a physical camera may be shared.
    db.execute(text("SELECT pg_advisory_xact_lock(84238601)"))
    binding = db.query(PhaseBinding).filter_by(session_id=sid).first()
    if binding:
        # Personnel writes use this room lock too: a different draft cannot
        # change the room roster between readiness validation and start.
        db.query(BusinessRoom).filter_by(
            room_id=binding.room_id
        ).with_for_update().first()
    checked = readiness(db, session, book)
    missing = [
        c["label"]
        for c in checked["items"]
        if c.get("required", True) and not c["passed"]
    ]
    if missing:
        fail("PREPARATION_INCOMPLETE", "请完成评前准备后再开始监管", missing=missing)
    session.status = "running"
    session.start_at = datetime.utcnow()
    book.started_by = who
    journal(
        db,
        session,
        book,
        "start",
        "开始现场监管",
        who,
        {
            "checks": deepcopy(book.checks),
            "identity_verification_enabled": book.identity_verification_enabled,
        },
    )


def finish(db, sid, expected, who, note):
    session, book = mutate(db, sid, expected)
    if session.status != "running":
        fail("INVALID_STATE", "只有进行中的场次可以结束现场监管")
    session.status = "closed"
    session.end_at = datetime.utcnow()
    book.ended_by = who
    book.end_note = note
    journal(db, session, book, "finish", note, who)


def archive(db, sid, expected, who, note, gap_note):
    session, book = mutate(db, sid, expected)
    if session.status != "closed":
        fail("SESSION_NOT_ENDED", "请先登记现场结束，再核对收尾资料")
    assessment = detail(db, sid)
    missing = [c["label"] for c in assessment["archive_checks"] if not c["passed"]]
    if missing:
        fail("ARCHIVE_INCOMPLETE", "场次尚不满足归档条件", missing=missing)
    from app.core.supervision_reporting import build_session_report

    snapshot = build_session_report(db, session)
    gaps = sum(
        i["seconds"]
        for c in snapshot["coverage"]
        for i in c["intervals"]
        if i["status"] not in ("online", "running", "recent", "not_applicable")
    )
    if (gaps > 0 or assessment["media_pending"] > 0) and len(
        (gap_note or "").strip()
    ) < 10:
        fail(
            "EVIDENCE_EXPLANATION_REQUIRED",
            "存在覆盖未核验区间或资料缺口，请填写不少于10字的核查说明",
        )
    signature = fingerprint(db, session)
    snapshot.update(
        report_type="归档监管记录",
        archive_note=note,
        evidence_gap_note=gap_note,
        content_fingerprint=signature,
        preparation=deepcopy(book.checks),
        identity_verification_enabled=book.identity_verification_enabled,
        preparation_snapshot=deepcopy(book.preparation_snapshot),
        session_journal=[
            {
                "journal_id": j.journal_id,
                "kind": j.kind,
                "note": j.note,
                "actor": j.actor,
                "at": iso_utc(j.created_at),
                "detail": j.detail,
            }
            for j in db.query(SessionJournal)
            .filter_by(session_id=sid)
            .order_by(SessionJournal.created_at)
        ],
        previous_report_id=book.archived_report_id,
    )
    row = SupervisionSessionReport(
        report_id=str(uuid4()), session_id=sid, actor=who, snapshot=snapshot
    )
    db.add(row)
    db.flush()
    book.archived_report_id = row.report_id
    book.archived_fingerprint = signature
    book.archived_at = datetime.utcnow()
    journal(
        db,
        session,
        book,
        "archive",
        note,
        who,
        {"report_id": row.report_id, "evidence_gap_note": gap_note},
    )
    return row
