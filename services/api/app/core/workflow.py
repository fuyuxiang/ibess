"""事件序列化和无视频业务演示共享能力。"""
import logging
from datetime import datetime, timezone
import redis
from fastapi import HTTPException
from app.config import settings
from app.models import Decision, POCEventMeta, Event, Evidence, ReminderAttempt, ReminderExecution, WorkflowAudit
from app.business_models import BusinessEventContext
from app.core.supervision import case_dict, event_scope, structured_clues
from app.supervision_models import SemanticEventLink

log = logging.getLogger(__name__)
DEMO_SKILL = "demo_simulation"


def iso_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def event_dict(event, db):
    meta = db.get(POCEventMeta, event.event_id)
    context = db.get(BusinessEventContext, event.event_id)
    project_id, room_id, snapshot = event_scope(event, db)
    semantic = db.query(SemanticEventLink).filter(SemanticEventLink.event_id == event.event_id).first()
    machine_conclusion = None
    original = {}
    if semantic:
        from app.semantic_models import SemanticAnalysis
        analysis = db.get(SemanticAnalysis, semantic.analysis_id)
        machine_conclusion = (analysis.result or {}).get('conclusion') if analysis else None
        original = analysis.request_snapshot or {} if analysis else {}
    from app.core.supervision_evidence import is_audio_event
    from app.supervision_models import SupervisionEventSpan, SupervisionTaskReceipt
    span = db.get(SupervisionEventSpan,event.event_id)
    receipt = db.query(SupervisionTaskReceipt).filter_by(event_id=event.event_id).order_by(SupervisionTaskReceipt.assignment_version.desc()).first()
    return {
        "event_id": event.event_id, "event_type": event.event_type,
        "camera_id": event.camera_id, "session_id": event.session_id,
        "risk_level": event.risk_level, "confidence": event.confidence,
        "reason": event.reason, "status": event.status, "occurred_at": iso_utc(event.occurred_at),
        "created_at": iso_utc(event.created_at),
        "phase_id": event.phase_id,
        'machine_conclusion': machine_conclusion,
        "is_demo": event.skill_code == DEMO_SKILL,
        "source_type": "simulation" if meta else ("audio" if is_audio_event(db,event) else "video"),
        'origin': 'manual' if (event.skill_code or '').startswith('manual_') else 'machine',
        'behavior': {'started_at':iso_utc(span.started_at),'last_seen_at':iso_utc(span.last_seen_at),'ended_at':iso_utc(span.ended_at),
            'active':span.active and (datetime.utcnow()-span.last_seen_at).total_seconds()<30,'trigger_count':span.trigger_count,'end_basis':span.end_basis} if span else None,
        'task_receipt': {'assignee':receipt.assignee,'received_by':receipt.received_by,'received_at':iso_utc(receipt.received_at),'note':receipt.note} if receipt else None,
        "room": snapshot.get('room_name') or (meta.room if meta else event.camera_id),
        "model_name": original.get('model_name',event.model_name), "model_version": original.get('model_version',event.model_version),
        "skill_code": event.skill_code, "skill_version": original.get('rule_version',event.skill_version),
        'time_basis': ('captured' if event.occurred_at else 'unknown'),
        "project_id": project_id,
        "project_name": snapshot.get("project_name"),
        "room_id": room_id,
        "seat_id": context.seat_id if context else None,
        "seat_code": snapshot.get("seat_code"),
        "person_id": context.person_id if context else None,
        "person_name": snapshot.get("person_name"),
    } | case_dict(event, db)


def reminder_dict(record, db):
    attempts = db.query(ReminderAttempt).filter(ReminderAttempt.execution_id == record.execution_id).order_by(ReminderAttempt.created_at).all()
    return {key: getattr(record, key) for key in ("execution_id", "status", "asset_url", "asset_version", "error")} | {
        key: iso_utc(getattr(record, key)) for key in ("started_at", "completed_at", "created_at")
    } | {"attempt_id": attempts[-1].attempt_id if attempts else None, "attempts": [
        {"attempt_id": a.attempt_id, "status": a.status, "asset_url": a.asset_url, "asset_version": a.asset_version,
         "error": a.error, "started_at": iso_utc(a.started_at), "completed_at": iso_utc(a.completed_at), "created_at": iso_utc(a.created_at)}
        for a in attempts]}


def find_event(db, event_id, lock=False):
    query = db.query(Event).filter(Event.event_id == event_id)
    if lock:
        query = query.with_for_update()
    event = query.first()
    if event is None:
        raise HTTPException(404, "事件不存在")
    return event


def add_audit(db, event_id, action, note="", actor="demo-reviewer"):
    db.add(WorkflowAudit(event_id=event_id, action=action, actor=actor, note=note))


def evidence_dict(e) -> dict:
    """关键帧走平台鉴权缩略接口（w=320），录像片段仍直连对象存储。"""
    from urllib.parse import quote
    if e.evidence_type == "keyframe":
        url = f"/api/evidence/keyframes/object?w=320&key={quote(e.object_key)}"
        full_url = f"/api/evidence/keyframes/object?key={quote(e.object_key)}"
    else:
        url = f'/api/evidence/objects/{e.id}/content'
        full_url = url
    return {"id": e.id, "evidence_type": e.evidence_type, "object_key": e.object_key,
            "url": url, "full_url": full_url,
            "sha256": e.sha256, "captured_at": iso_utc(e.captured_at)}


def event_detail(db, event_id):
    event = find_event(db, event_id)
    decisions = db.query(Decision).filter(Decision.event_id == event_id).order_by(Decision.id).all()
    evidences = db.query(Evidence).filter(Evidence.event_id == event_id).order_by(Evidence.id).all()
    reminders = db.query(ReminderExecution).filter(ReminderExecution.event_id == event_id).order_by(ReminderExecution.created_at).all()
    audits = db.query(WorkflowAudit).filter(WorkflowAudit.event_id == event_id).order_by(WorkflowAudit.id).all()
    meta = db.get(POCEventMeta, event_id)
    semantic = None
    link = db.query(SemanticEventLink).filter(SemanticEventLink.event_id == event_id).first()
    if link:
        from app.semantic_models import SemanticAnalysis
        from app.audio_models import AudioSegment
        analysis = db.get(SemanticAnalysis, link.analysis_id)
        if analysis:
            segments = db.query(AudioSegment).filter(AudioSegment.recording_id == analysis.recording_id,
                AudioSegment.end_ms > analysis.window_start_ms, AudioSegment.start_ms < analysis.window_end_ms).order_by(AudioSegment.start_ms).all()
            semantic = {'analysis_id':analysis.analysis_id, 'recording_id':analysis.recording_id, 'scene_id':analysis.scene_id,
                'result':analysis.result, 'window_start_ms':analysis.window_start_ms, 'window_end_ms':analysis.window_end_ms,
                'context':(analysis.request_snapshot or {}).get('context', {}),
                'segments':[{'segment_id':s.segment_id, 'speaker':s.speaker, 'start_ms':s.start_ms, 'end_ms':s.end_ms, 'text':s.text} for s in segments]}
    from app.core.media_preservation import hold_dict, evidence_readiness
    from app.supervision_models import SupervisionArchive,SupervisionOperation
    archives = db.query(SupervisionArchive).filter_by(event_id=event_id).order_by(SupervisionArchive.version).all()
    corrections = db.query(SupervisionOperation).filter_by(event_id=event_id).order_by(SupervisionOperation.created_at).all()
    from app.core.supervision_evidence import event_context,video_windows
    return event_dict(event, db) | {
        'context_evidence': event_context(db,event),
        'media_hold':hold_dict(db,event_id) if not meta and video_windows(db,event) else None,
        'evidence_status':evidence_readiness(db,event),
        'archives':[{'version':a.version,'resolution':a.resolution,'actor':a.actor,'note':a.note,'evidence_note':a.evidence_note,'snapshot':a.snapshot,'at':iso_utc(a.created_at)} for a in archives],
        'corrections':[{'actor':c.actor,'at':iso_utc(c.created_at),'previous_case':c.result['previous_case']} for c in corrections if c.result.get('previous_case')],
        'clues': structured_clues(db, event_id), 'semantic': semantic,
        "demo_input": {"room": meta.room, "note": meta.note} if meta else None,
        "decisions": [{"id": d.id, "action": d.action, "reviewer_id": d.reviewer_id, "note": d.note, "decided_at": iso_utc(d.decided_at)} for d in decisions],
        "evidences": [evidence_dict(e) for e in evidences],
        "reminders": [reminder_dict(r, db) for r in reminders],
        "audit": [{"action": a.action, "actor": a.actor, "note": a.note, "at": iso_utc(a.at)} for a in audits],
    }


def publish_event(event, db):
    """数据库为事实源，推送失败时页面定时补查。"""
    data = event_dict(event, db)
    try:
        client = redis.from_url(settings.redis_url, socket_connect_timeout=0.5, socket_timeout=0.5)
        try:
            client.xadd("events", {k: str(v) if v is not None else "" for k, v in data.items()}, maxlen=1000)
        finally:
            client.close()
    except Exception as exc:
        log.warning("事件已保存，实时推送暂不可用: %s", type(exc).__name__)
