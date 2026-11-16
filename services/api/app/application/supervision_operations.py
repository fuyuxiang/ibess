"""Transactional supervision operations, independent of route registration."""

from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json

from fastapi import HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Decision
from app.supervision_models import (
    SupervisionCase,
    SupervisionOperation,
    SupervisionArchive,
)
from app.core.workflow import (
    event_detail,
    find_event,
    add_audit,
    publish_event,
    iso_utc,
)
from app.core.supervision import (
    TRANSITIONS,
    ACTION_LABELS,
    legacy_status,
    STATE_LABELS,
    RESOLUTION_LABELS,
)


from app.schemas.supervision import OperationIn
from app.application.auth_context import actor


def operate_event(event_id: str, body: OperationIn, request: Request, db: Session):
    who = actor(request)
    if request.state.admin.get("role") == "operator" and body.action not in (
        "supplement",
        "feedback",
    ):
        raise HTTPException(
            403,
            "处置岗位只能补充核查或提交本人负责的处置反馈，认定及复核由监管岗位办理",
        )
    if body.action not in ACTION_LABELS or not body.note.strip():
        raise HTTPException(422, "请选择有效操作并填写核查或处置说明")
    fingerprint = hashlib.sha256(
        json.dumps(
            body.model_dump(mode="json", exclude={"request_id"}),
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    event = find_event(db, event_id, lock=True)
    if event.risk_level == "info":
        raise HTTPException(422, "基础观察记录不进入异常确认与处置流程")
    previous_request = db.get(SupervisionOperation, body.request_id)
    if previous_request:
        if (
            previous_request.event_id,
            previous_request.actor,
            previous_request.fingerprint,
        ) != (event_id, who, fingerprint):
            raise HTTPException(409, "请求编号已用于其他操作")
        return previous_request.result | {"duplicate": True}
    record = db.get(SupervisionCase, event_id)
    if request.state.admin.get("role") == "operator" and body.action == "feedback":
        from app.supervision_models import SupervisionTaskReceipt

        receipt = (
            db.query(SupervisionTaskReceipt)
            .filter_by(event_id=event_id)
            .order_by(SupervisionTaskReceipt.assignment_version.desc())
            .first()
        )
        if (
            not record
            or record.assignee != who
            or not receipt
            or receipt.received_by != who
        ):
            raise HTTPException(403, "请先接收分派给本账号的任务，再提交处置反馈")
    status, version = (
        (record.status, record.version) if record else (legacy_status(event), 0)
    )
    if (body.expected_status, body.expected_version) != (status, version):
        raise HTTPException(
            409, "事件已被其他操作更新，请刷新后核对；当前填写内容仍保留"
        )
    if body.action not in TRANSITIONS.get(status, set()):
        raise HTTPException(409, "当前监管状态不允许此操作")
    from app.core.supervision_scope import capture_context

    capture_context(event, db)
    if body.action in ("archive", "dismiss"):
        from app.core.media_preservation import evidence_readiness

        evidence_state = evidence_readiness(db, event, verify=True)
        if evidence_state not in ("READY", "NOT_APPLICABLE") and (
            not body.acknowledge_missing_evidence
            or not (body.evidence_note or "").strip()
        ):
            raise HTTPException(
                422,
                "关联资料尚未完整保全；请等待保全完成，或核实缺失情况并填写资料缺失说明",
            )
    if body.evidence_note and body.action not in ("archive", "dismiss"):
        raise HTTPException(422, "只有归档时可以登记资料缺失说明")
    if body.action == "assign" and not (body.assignee or "").strip():
        raise HTTPException(422, "请填写实际处置责任人或责任岗位")
    if body.action == "dismiss" and body.resolution not in (
        "false_positive",
        "authorized_exception",
        "normal",
    ):
        raise HTTPException(422, "请明确排除原因：误报、授权例外或正常行为")
    if body.due_at and body.action != "assign":
        raise HTTPException(422, "只有登记处置任务时可以指定期限")
    due_at = None
    if body.due_at:
        if body.due_at.tzinfo is None:
            raise HTTPException(422, "处置期限必须包含时区")
        due_at = body.due_at.astimezone(timezone.utc).replace(tzinfo=None)
        if due_at <= datetime.utcnow():
            raise HTTPException(422, "处置期限必须晚于当前时间")
    if record is None:
        record = SupervisionCase(event_id=event_id, status=status, version=0)
        db.add(record)
    previous_case = {
        "status": status,
        "resolution": record.resolution
        or {"FALSE_POSITIVE": "false_positive", "CONFIRMED": "confirmed"}.get(
            event.status
        ),
        "assignee": record.assignee,
        "feedback": record.feedback,
        "due_at": iso_utc(record.due_at),
        "archived_at": iso_utc(record.archived_at),
    }
    record.status = {
        "confirm": "CONFIRMED",
        "supplement": "SUPPLEMENT",
        "dismiss": "ARCHIVED",
        "assign": "ASSIGNED",
        "feedback": "FEEDBACK",
        "return": "ASSIGNED",
        "archive": "ARCHIVED",
        "reopen": "SUPPLEMENT",
    }[body.action]
    record.version += 1
    record.updated_at = datetime.utcnow()
    if body.action == "confirm":
        record.resolution = "confirmed"
    if body.action == "assign":
        from app.auth_models import TeamAccount

        account = (
            db.query(TeamAccount).filter_by(username=body.assignee.strip()).first()
        )
        if account and (not account.active or account.role == "viewer"):
            raise HTTPException(422, "该账号已停用或没有处置权限")
        record.assignee, record.due_at = body.assignee.strip(), due_at
        record.feedback = None
        from app.supervision_models import SupervisionTaskReceipt

        db.add(
            SupervisionTaskReceipt(
                event_id=event_id,
                assignment_version=record.version,
                assignee=record.assignee,
            )
        )
    if body.action == "feedback":
        record.feedback = body.note.strip()
    if body.action == "dismiss":
        record.resolution = body.resolution
    if body.action == "reopen":
        record.resolution = record.assignee = record.due_at = record.feedback = (
            record.archived_at
        ) = None
    if record.status == "ARCHIVED":
        record.archived_at = datetime.utcnow()
    event.status = {
        "PENDING": "PENDING",
        "SUPPLEMENT": "PENDING",
        "CONFIRMED": "CONFIRMED",
        "ASSIGNED": "ESCALATED",
        "FEEDBACK": "ESCALATED",
        "ARCHIVED": "FALSE_POSITIVE" if body.action == "dismiss" else "CLOSED",
    }[record.status]
    note = body.note.strip()
    if body.action == "assign":
        note = f"责任人/岗位：{record.assignee}；期限：{iso_utc(record.due_at) or '未指定'}；{note}"
    if body.action == "dismiss":
        note = f"排除类型：{dict(false_positive='识别误报', authorized_exception='授权例外', normal='正常行为')[body.resolution]}；{note}"
    if body.action == "reopen":
        note = f"原人工结论：{RESOLUTION_LABELS.get(previous_case['resolution'], '尚未认定')}；原流程状态：{STATE_LABELS[status]}；重新核查依据：{note}"
    if body.action in ("archive", "dismiss") and evidence_state not in (
        "READY",
        "NOT_APPLICABLE",
    ):
        note += f"；资料缺失说明：{body.evidence_note.strip()}"
    db.add(Decision(event_id=event_id, action=body.action, reviewer_id=who, note=note))
    add_audit(
        db,
        event_id,
        body.action,
        f"{STATE_LABELS[status]} → {STATE_LABELS[record.status]}；{note}",
        who,
    )
    if record.status == "ARCHIVED":
        db.flush()
        snapshot = event_detail(db, event_id)
        snapshot.pop("archives", None)
        db.add(
            SupervisionArchive(
                event_id=event_id,
                version=record.version,
                resolution=record.resolution,
                actor=who,
                note=body.note.strip(),
                evidence_note=body.evidence_note.strip()
                if body.evidence_note
                else None,
                snapshot=json.loads(json.dumps(snapshot, default=str)),
            )
        )
    result = {
        "event_id": event_id,
        "workflow_status": record.status,
        "workflow_version": record.version,
        "duplicate": False,
    }
    if body.action == "reopen":
        result["previous_case"] = previous_case
    db.add(
        SupervisionOperation(
            request_id=body.request_id,
            event_id=event_id,
            fingerprint=fingerprint,
            actor=who,
            result=result,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "并发提交冲突，请用原请求重试")
    publish_event(event, db)
    return result
