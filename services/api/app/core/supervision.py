"""监管状态机、事件上下文与语义线索接入。所有人工作用均留痕。"""
from datetime import datetime, timezone, timedelta
import logging
from uuid import uuid4
from sqlalchemy import inspect, text
from app.models import Event, Decision, WorkflowAudit, POCEventMeta
from app.business_models import BusinessEventContext, BusinessProject, BusinessRoom
from app.phase_models import PhaseBinding
from app.media_binding_models import MediaBinding
from app.semantic_models import SemanticAnalysis
from app.supervision_models import SupervisionCase, SemanticEventLink, SupervisionIntakeFailure

TRANSITIONS = {
    'PENDING': {'confirm', 'supplement', 'dismiss'},
    'SUPPLEMENT': {'confirm', 'supplement', 'dismiss'},
    'CONFIRMED': {'assign', 'reopen'},
    'ASSIGNED': {'assign', 'feedback', 'reopen'},
    'FEEDBACK': {'return', 'archive', 'reopen'},
    'LEGACY_CLOSED': {'confirm', 'supplement', 'dismiss'},
    'ARCHIVED': {'reopen'},
}
ACTION_LABELS = {'confirm': '确认异常', 'supplement': '补充核查', 'dismiss': '排除并归档',
                 'assign': '登记处置任务', 'feedback': '提交处置反馈', 'return': '退回继续处置', 'archive': '复核归档', 'reopen':'重新核查 / 更正认定'}
TECHNICAL_TYPES = ('camera_blacked', 'video_offline', 'audio_offline', 'camera_occlusion', 'stream_gap', 'recording_gap')
STATE_LABELS = {'PENDING':'待核查','SUPPLEMENT':'补充核查中','CONFIRMED':'待分派','ASSIGNED':'待反馈','FEEDBACK':'待复核','ARCHIVED':'已归档','LEGACY_CLOSED':'历史关闭待核查'}
RESOLUTION_LABELS = {'confirmed':'确认异常','false_positive':'识别误报','authorized_exception':'授权例外','normal':'正常行为'}


def technical_event(event):
    return event.event_type in TECHNICAL_TYPES or (event.skill_code or '').lower().startswith('s10') or (event.event_type or '').lower().startswith('s10')


def legacy_status(event):
    return {'CONFIRMED': 'CONFIRMED', 'FALSE_POSITIVE': 'ARCHIVED', 'CLOSED': 'LEGACY_CLOSED'}.get(event.status, 'PENDING')


def case_dict(event, db):
    from app.core.workflow import iso_utc
    case = db.get(SupervisionCase, event.event_id)
    return {'workflow_status': case.status if case else legacy_status(event),
            'workflow_version': case.version if case else 0,
            'resolution': case.resolution if case else ({'FALSE_POSITIVE':'false_positive','CONFIRMED':'confirmed'}.get(event.status)),
            'assignee': case.assignee if case else None, 'due_at': iso_utc(case.due_at) if case else None,
            'feedback': case.feedback if case else None, 'archived_at': iso_utc(case.archived_at) if case else None,
            'category': 'technical' if technical_event(event) else 'observation' if event.risk_level == 'info' else 'behavior'}


def channel(source):
    import re
    if source == 'camera-real-01':
        return 'cam-ezviz'
    match = re.fullmatch(r'(?:camera-0*|cam)(\d+)', source or '')
    if match:
        return 'cam' + str(int(match.group(1)))
    # Explicitly supported deployment channel; never accept arbitrary URLs / path traversal.
    return source if re.fullmatch(r'[A-Za-z0-9_-]{1,100}', source or '') else None


def event_scope(event, db):
    context = db.get(BusinessEventContext, event.event_id)
    if context:
        return context.project_id, context.room_id, context.snapshot or {}
    from app.core.supervision_scope import resolve_scope
    scope = resolve_scope(db,event.camera_id,event.occurred_at,event.session_id)
    if scope:
        return scope['project_id'],scope['room_id'],scope
    return None, None, {}


def structured_clues(db, event_id):
    # The event worker owns this optional table. Older deployments do not have it.
    if not inspect(db.get_bind()).has_table('risk_clues'):
        return []
    rows = db.execute(text('select clue_id, scene_id, rule_id, rule_version, conclusion, reason, missing_facts, quality_flags, evidence_refs, start_ms, end_ms, occurred_at from risk_clues where event_id = :eid order by occurred_at'), {'eid': event_id}).mappings().all()
    return [dict(row) for row in rows]


def sync_semantic_events(db, limit=100):
    """后台消费已完成语义任务。分析ID唯一+行锁保证重启/并发不重复入队。"""
    rows = (db.query(SemanticAnalysis).outerjoin(SemanticEventLink, SemanticAnalysis.analysis_id == SemanticEventLink.analysis_id)
            .outerjoin(SupervisionIntakeFailure,SupervisionIntakeFailure.analysis_id==SemanticAnalysis.analysis_id)
            .filter(SemanticAnalysis.status == 'ready', SemanticEventLink.analysis_id.is_(None),
                    (SupervisionIntakeFailure.analysis_id.is_(None)) | (SupervisionIntakeFailure.next_retry_at<=datetime.utcnow()),
                    SemanticAnalysis.result['conclusion'].astext.in_(['suspected', 'insufficient']))
            .order_by(SemanticAnalysis.completed_at).with_for_update(of=SemanticAnalysis, skip_locked=True).limit(limit).all())
    created = []
    for row in rows:
        try:
            with db.begin_nested():
                event = _intake_semantic(db,row)
                failure = db.get(SupervisionIntakeFailure,row.analysis_id)
                if failure:
                    db.delete(failure)
                db.flush()
            created.append(event)
        except Exception:
            logging.getLogger(__name__).exception('语义事件接入失败：analysis_id=%s',row.analysis_id)
            failure = db.get(SupervisionIntakeFailure,row.analysis_id)
            if failure is None:
                failure = SupervisionIntakeFailure(analysis_id=row.analysis_id,attempts=0)
                db.add(failure)
            failure.attempts += 1
            failure.error = '线索接入失败，请核对任务来源、场次和输入字段，其他任务继续处理'
            failure.next_retry_at = datetime.utcnow()+timedelta(seconds=min(300,15*2**min(failure.attempts,5)))
            db.flush()
    return created


def _intake_semantic(db,row):
    result, snapshot = row.result or {}, row.request_snapshot or {}
    for key in ('source_id','session_id'):
        value = snapshot.get(key)
        if value is not None and (not isinstance(value,str) or len(value)>64):
            raise ValueError('无效的事件作用域字段')
    def short(key,length):
        value = snapshot.get(key)
        if value is not None and not isinstance(value,str):
            raise ValueError('无效的模型或规则元数据')
        return value[:length] if value else None
    occurred = None
    if snapshot.get('time_basis') == 'capture_start':
        origin = _parse_time(snapshot.get('occurred_at'))
        evidence = result.get('evidence') or []
        offset = min((item['start_ms'] for item in evidence),default=row.window_start_ms)
        occurred = origin+timedelta(milliseconds=offset) if origin else None
    binding = db.query(MediaBinding).filter(MediaBinding.recording_id == row.recording_id).first()
    event = Event(event_id=str(uuid4()), session_id=snapshot.get('session_id') or 'unbound',
                  camera_id=snapshot.get('source_id') or 'audio', skill_code=f'semantic_{row.scene_id}',
                  skill_version=short('rule_version',32), model_name=short('model_name',64),
                  model_version=short('model_version',32), event_type=f'semantic_{row.scene_id}',
                  phase_id=short('phase_id',32), risk_level=result.get('risk_level') if result.get('risk_level') in ('low','medium','high') else 'low',
                  confidence=None, reason=result.get('reason') or '语义线索待人工核查',
                  occurred_at=occurred, status='PENDING')
    db.add(event); db.flush()
    db.add(SemanticEventLink(analysis_id=row.analysis_id, event_id=event.event_id))
    from app.supervision_models import SemanticBusinessSnapshot
    business = db.get(SemanticBusinessSnapshot,row.analysis_id)
    if business:
        db.add(BusinessEventContext(event_id=event.event_id,project_id=business.project_id,room_id=business.room_id,snapshot=business.snapshot))
    elif binding and snapshot.get('project_id') == binding.project_id and snapshot.get('room_id') == binding.room_id:
        project, room = db.get(BusinessProject, binding.project_id), db.get(BusinessRoom, binding.room_id)
        db.add(BusinessEventContext(event_id=event.event_id, project_id=binding.project_id, room_id=binding.room_id,
                snapshot={'project_name': project.name, 'room_name': room.name, 'analysis_id': row.analysis_id}))
    if result.get('conclusion') == 'insufficient':
        db.add(SupervisionCase(event_id=event.event_id, status='SUPPLEMENT', version=0))
    db.add(WorkflowAudit(event_id=event.event_id, action='semantic_intake', actor='system', note=f'语义分析 {row.analysis_id}；机器结论：{result.get("conclusion")}，需人工核查'))
    return event

def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00')) if value else None
        if parsed and parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed else None
    except (ValueError, AttributeError):
        return None
