"""Authenticated supervision workbench. Detection conclusions never replace human decisions."""
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from urllib.request import urlopen
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, case, desc, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Event, Decision, POCEventMeta
from app.business_models import BusinessEventContext, BusinessProject, BusinessRoom
from app.phase_models import PhaseBinding
from app.models import EvalSession
from app.supervision_models import SupervisionCase, SupervisionOperation, SupervisionArchive, SupervisionMediaHold
from app.core.workflow import event_dict, event_detail, find_event, add_audit, publish_event, iso_utc
from app.core.supervision import TRANSITIONS, ACTION_LABELS, TECHNICAL_TYPES, legacy_status, channel, STATE_LABELS, RESOLUTION_LABELS

router = APIRouter(prefix='/supervision', tags=['supervision'])


from app.application.auth_context import actor


def expressions():
    status = func.coalesce(SupervisionCase.status, case((Event.status == 'CONFIRMED', 'CONFIRMED'),
        (Event.status == 'FALSE_POSITIVE', 'ARCHIVED'), (Event.status == 'CLOSED', 'LEGACY_CLOSED'), else_='PENDING'))
    technical = or_(Event.event_type.in_(TECHNICAL_TYPES), Event.skill_code.ilike('s10%'), Event.event_type.ilike('s10%'))
    return status, technical


@router.get('/events')
def events(response: Response, request:Request, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
           keyword: str = Query('', max_length=200), project_id: str | None = None, room_id: str | None = None,
           session_id: str | None = None, camera_id: str | None = None, workflow_status: str | None = None,
           queue: str | None = None, risk_level: str | None = None, category: str = 'behavior',
           date_from: date | None = None, date_to: date | None = None, assignee: str | None = None, resolution: str | None = None, mine:bool=False, overdue:bool=False,
           db: Session = Depends(get_session)):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, '开始日期不能晚于结束日期')
    if category not in ('behavior', 'technical', 'all') or queue not in (None, 'confirm', 'process', 'dispatch', 'review', 'active', 'archive'):
        raise HTTPException(422, '无效的事件分类或队列')
    if workflow_status and workflow_status not in TRANSITIONS:
        raise HTTPException(422, '无效的监管状态')
    status, technical = expressions()
    if mine:assignee=actor(request)
    if resolution and resolution not in ('confirmed','false_positive','authorized_exception','normal','unreviewed'):
        raise HTTPException(422,'无效的人工结论')
    # A session binding must also match the source; never put another camera into the selected room.
    same_source = or_(PhaseBinding.source_id == Event.camera_id,
        and_(PhaseBinding.source_id.in_(['camera-real-01','cam-ezviz']), Event.camera_id.in_(['camera-real-01','cam-ezviz'])), and_(
        PhaseBinding.source_id.op('~')('^(camera-|cam)[0-9]+$'), Event.camera_id.op('~')('^(camera-|cam)[0-9]+$'),
        func.ltrim(func.regexp_replace(PhaseBinding.source_id, '^(camera-|cam)', ''), '0') == func.ltrim(func.regexp_replace(Event.camera_id, '^(camera-|cam)', ''), '0')))
    query = (db.query(Event).outerjoin(SupervisionCase, SupervisionCase.event_id == Event.event_id)
        .outerjoin(BusinessEventContext, BusinessEventContext.event_id == Event.event_id)
        .outerjoin(PhaseBinding, and_(PhaseBinding.session_id == Event.session_id, same_source))
        .outerjoin(BusinessProject, BusinessProject.project_id == func.coalesce(BusinessEventContext.project_id, PhaseBinding.project_id))
        .outerjoin(BusinessRoom, BusinessRoom.room_id == func.coalesce(BusinessEventContext.room_id, PhaseBinding.room_id))
        .outerjoin(POCEventMeta, POCEventMeta.event_id == Event.event_id))
    query = query.filter(Event.risk_level.in_(['low','medium','high']))
    if overdue:query=query.filter(status!='ARCHIVED',SupervisionCase.due_at<datetime.utcnow())
    if category != 'all':
        query = query.filter(technical if category == 'technical' else and_(~func.coalesce(technical, False), Event.risk_level.in_(['low','medium','high'])))
    effective_resolution = func.coalesce(SupervisionCase.resolution,case((Event.status=='FALSE_POSITIVE','false_positive'),(Event.status=='CONFIRMED','confirmed'),else_=None))
    if resolution:
        query = query.filter(effective_resolution.is_(None) if resolution=='unreviewed' else effective_resolution==resolution)
    for column, value in ((BusinessProject.project_id, project_id), (BusinessRoom.room_id, room_id),
                          (Event.session_id, session_id), (status, workflow_status),
                          (Event.risk_level, risk_level), (SupervisionCase.assignee, assignee)):
        if value:
            query = query.filter(column == value)
    if queue:
        values = {'confirm': ['PENDING','SUPPLEMENT','LEGACY_CLOSED'], 'process': ['CONFIRMED','ASSIGNED','FEEDBACK'],'dispatch':['CONFIRMED','ASSIGNED'],
            'review':['FEEDBACK'],'active':['PENDING','SUPPLEMENT','LEGACY_CLOSED','CONFIRMED','ASSIGNED','FEEDBACK'],'archive': ['ARCHIVED']}[queue]
        query = query.filter(status.in_(values))
    if camera_id:
        # Camera aliases used by MediaMTX and detection workers describe the same source.
        import re
        normalized = channel(camera_id)
        if normalized == 'cam-ezviz':
            query = query.filter(Event.camera_id.in_(['camera-real-01','cam-ezviz']))
        elif normalized and re.fullmatch(r'cam\d+', normalized):
            query = query.filter(or_(Event.camera_id == camera_id, and_(Event.camera_id.op('~')('^(camera-|cam)[0-9]+$'),
                func.ltrim(func.regexp_replace(Event.camera_id, '^(camera-|cam)', ''), '0') == normalized[3:].lstrip('0'))))
        else:
            query = query.filter(Event.camera_id == camera_id)
    if keyword.strip():
        term = '%' + keyword.strip().replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%'
        query = query.filter(or_(*(col.ilike(term, escape='\\') for col in (Event.event_id, Event.reason, Event.event_type, Event.camera_id, BusinessProject.name, BusinessRoom.name, POCEventMeta.room, SupervisionCase.assignee))))
    if date_from:
        query = query.filter(Event.occurred_at >= datetime.combine(date_from, time.min) - timedelta(hours=8))
    if date_to:
        query = query.filter(Event.occurred_at < datetime.combine(date_to + timedelta(days=1), time.min) - timedelta(hours=8))
    groups = query.with_entities(status, func.count(Event.id)).group_by(status).all()
    counts = dict(groups)
    total = sum(counts.values())
    ordering = [desc(func.coalesce(SupervisionCase.archived_at,Event.updated_at,Event.created_at)),desc(Event.id)] if queue=='archive' else [desc(func.coalesce(Event.occurred_at,Event.created_at)),desc(Event.id)]
    if queue!='archive':
        ordering = [case((status.in_(['PENDING','SUPPLEMENT','LEGACY_CLOSED']),0),else_=1),case((Event.risk_level=='high',0),(Event.risk_level=='medium',1),else_=2)]+ordering
    rows = query.order_by(*ordering).offset((page-1)*page_size).limit(page_size).all()
    response.headers['Cache-Control'] = 'no-store'
    return {'items': [event_dict(row, db) for row in rows], 'total': total, 'page': page, 'page_size': page_size,
            'counts': counts, 'summary': {'confirm': sum(counts.get(s,0) for s in ['PENDING','SUPPLEMENT','LEGACY_CLOSED']),
                'process': sum(counts.get(s,0) for s in ['CONFIRMED','ASSIGNED','FEEDBACK']), 'archive': counts.get('ARCHIVED',0)}}


from app.schemas.supervision import OperationIn


@router.post('/events/{event_id}/actions')
def operate(event_id: str, body: OperationIn, request: Request, db: Session = Depends(get_session)):
    from app.application.supervision_operations import operate_event
    return operate_event(event_id, body, request, db)


@router.post('/events/{event_id}/preservation-retry')
def retry_preservation(event_id:str, request:Request, db:Session=Depends(get_session)):
    who = actor(request)
    find_event(db,event_id,lock=True)
    hold = db.get(SupervisionMediaHold,event_id)
    if hold:
        from app.core.media_preservation import hold_dict
        if hold_dict(db,event_id)['status']=='READY':
            return {'queued':False,'message':'关联录像已完整保全'}
        hold.status = 'PARTIAL' if hold.manifest else 'PENDING'
        hold.next_retry_at = datetime.utcnow()
    add_audit(db,event_id,'preservation_retry','申请重新核对并保全对应录像',who)
    db.commit()
    return {'queued':True}


@router.get('/sources')
def sources(db: Session = Depends(get_session)):
    rows = (db.query(PhaseBinding, EvalSession, BusinessProject, BusinessRoom)
            .join(EvalSession, EvalSession.session_id == PhaseBinding.session_id)
            .join(BusinessProject, BusinessProject.project_id == PhaseBinding.project_id)
            .join(BusinessRoom, BusinessRoom.room_id == PhaseBinding.room_id)
            .filter(PhaseBinding.active.is_(True), EvalSession.status != 'closed').order_by(desc(EvalSession.updated_at)).all())
    from app.core.supervision_evidence import session_channels
    return {'items': [{'binding_id':b.binding_id+':'+item['source_id'], 'session_id':b.session_id, 'session_name':s.name,
        'session_status':s.status, 'phase_id':s.current_phase, 'project_id':p.project_id, 'project_name':p.name,
        'room_id':r.room_id, 'room_name':r.name, 'source_id':item['source_id'],'kind':item['kind'],'source_name':item['name'],
        'hls_url':f'/hls/{channel(item["source_id"])}/index.m3u8' if item['kind']=='video' and channel(item['source_id']) else None}
        for b,s,p,r in rows for item in session_channels(db,b.session_id) if item['active']]}


@router.get('/health')
def monitoring_health(db: Session = Depends(get_session)):
    configured = sources(db)['items']
    from app.core.supervision_reporting import source_health
    items=source_health(db,configured)
    return {'items':items, 'checked_at':iso_utc(datetime.utcnow()), 'total':len(items), 'online':sum(i['stream_status']=='online' for i in items)}
