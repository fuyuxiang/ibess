"""API 与采集事件服务共用的数据库作用域解析，按来源、场次和发生时间隔离。"""
import json
import re
from sqlalchemy import text, func, or_, exists


def normalize_source(value):
    if value in ('camera-real-01', 'cam-ezviz'):
        return 'cam-ezviz'
    match = re.fullmatch(r'(?:camera-0*|cam)(\d+)', value or '')
    return 'cam'+str(int(match[1])) if match else value


def install_scope_resolver(engine):
    """Compatibility helper for isolated tests; production DDL is owned by revision 0003."""
    from pathlib import Path
    sql = (Path(__file__).resolve().parents[2] / 'migrations/snapshots/0003_scope.sql').read_text()
    with engine.begin() as db:
        db.exec_driver_sql(sql)


def resolve_scope(db, source, occurred_at, session_id=None):
    if not occurred_at:
        return None
    from datetime import timezone
    when = occurred_at if occurred_at.tzinfo else occurred_at.replace(tzinfo=timezone.utc)
    value = db.execute(text('select supervision_scope(:source,:at,:hint)'),
                       {'source': source, 'at': when, 'hint': session_id}).scalar()
    return json.loads(value) if isinstance(value, str) else value


def capture_context(event, db):
    from app.business_models import BusinessEventContext
    if db.get(BusinessEventContext, event.event_id):
        return
    scope = resolve_scope(db, event.camera_id, event.occurred_at, event.session_id)
    if scope:
        event.session_id = scope['session_id']
        event.phase_id = event.phase_id or scope['phase_id']
        db.add(BusinessEventContext(event_id=event.event_id, project_id=scope['project_id'], room_id=scope['room_id'], snapshot=scope))


def freeze_bound_contexts(db, project_id=None, room_id=None, limit=None):
    """在基础资料变更前补齐历史事件快照，后续改名不重写历史名称。"""
    from app.models import Event
    from app.phase_models import PhaseBinding
    from app.supervision_models import SupervisionChannel
    from app.business_models import BusinessEventContext
    query = db.query(Event).join(PhaseBinding, PhaseBinding.session_id == Event.session_id).outerjoin(
        BusinessEventContext, BusinessEventContext.event_id == Event.event_id).filter(BusinessEventContext.event_id.is_(None),Event.risk_level!='info',
        or_(func.supervision_channel(PhaseBinding.source_id)==func.supervision_channel(Event.camera_id),
            exists().where(SupervisionChannel.session_id==Event.session_id,func.supervision_channel(SupervisionChannel.source_id)==func.supervision_channel(Event.camera_id))))
    if project_id:
        query = query.filter(PhaseBinding.project_id == project_id)
    if room_id:
        query = query.filter(PhaseBinding.room_id == room_id)
    if limit:
        query = query.limit(limit)
    for event in query.all():
        capture_context(event, db)
