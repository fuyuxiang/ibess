"""场次工作区：人工标记、辅助通道、联合证据和处置接收。"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Event, EvalSession
from app.phase_models import PhaseBinding
from app.supervision_models import (SupervisionCase, SupervisionOperation, SupervisionChannel,
    SupervisionEventSpan, SupervisionEvidenceWindow, SupervisionAudioLink, SupervisionTaskReceipt)
from app.core.supervision_evidence import event_context, event_range, session_channels, utc_naive, reset_preservation, video_windows
from app.core.supervision_scope import resolve_scope, capture_context
from app.core.supervision import legacy_status
from app.core.workflow import find_event, add_audit, event_dict, iso_utc, publish_event
from app.routers.supervision import actor

router = APIRouter(prefix='/supervision',tags=['supervision'])


@router.get('/summary')
def work_summary(db:Session=Depends(get_session)):
    from app.routers.supervision import expressions
    from sqlalchemy import func
    state,technical=expressions()
    query=db.query(Event).outerjoin(SupervisionCase,SupervisionCase.event_id==Event.event_id).filter(
        Event.risk_level.in_(['low','medium','high']),~func.coalesce(technical,False))
    counts=dict(query.with_entities(state,func.count(Event.id)).group_by(state).all())
    overdue=query.filter(state!='ARCHIVED',SupervisionCase.due_at<datetime.utcnow()).count()
    return {'confirm':sum(counts.get(s,0) for s in ('PENDING','SUPPLEMENT','LEGACY_CLOSED')),
        'dispatch':sum(counts.get(s,0) for s in ('CONFIRMED','ASSIGNED')),'review':counts.get('FEEDBACK',0),'archive':counts.get('ARCHIVED',0),
        'overdue':overdue,'sessions':db.query(EvalSession).join(PhaseBinding,PhaseBinding.session_id==EvalSession.session_id).filter(
            EvalSession.status=='running',PhaseBinding.active.is_(True)).count()}


@router.get('/operators')
def operator_directory(db:Session=Depends(get_session)):
    from app.auth_models import TeamAccount,AdminAccount
    accounts=db.query(TeamAccount).filter(TeamAccount.active.is_(True),TeamAccount.role.in_(['supervisor','operator'])).all()
    admin=db.get(AdminAccount,1)
    return [{'username':a.username,'display_name':a.display_name,'role':getattr(a,'role','admin')} for a in ([admin] if admin else [])+accounts]


@router.get('/sessions/{session_id}/audio')
def session_audio(session_id:str,db:Session=Depends(get_session)):
    from app.audio_models import AudioRecording
    from app.media_binding_models import MediaBinding
    from app.supervision_models import RecordingCaptureTime
    from sqlalchemy import or_
    session=db.query(EvalSession).filter_by(session_id=session_id).first()
    binding=db.query(PhaseBinding).filter_by(session_id=session_id).first()
    if not session or not binding:raise HTTPException(404,'未找到场次业务范围')
    start=utc_naive(session.start_at or session.created_at)
    end=utc_naive(session.end_at or datetime.utcnow())
    query=db.query(AudioRecording,RecordingCaptureTime).join(MediaBinding,MediaBinding.recording_id==AudioRecording.recording_id).outerjoin(
        RecordingCaptureTime,RecordingCaptureTime.recording_id==AudioRecording.recording_id).filter(MediaBinding.room_id==binding.room_id,
            or_((AudioRecording.created_at>=start.replace(tzinfo=timezone.utc)) & (AudioRecording.created_at<=end.replace(tzinfo=timezone.utc)),
                (RecordingCaptureTime.started_at>=start.replace(tzinfo=timezone.utc)-timedelta(minutes=10)) & (RecordingCaptureTime.started_at<=end.replace(tzinfo=timezone.utc))))
    rows = query.order_by(AudioRecording.created_at.desc()).limit(30).all()
    if not rows:
        # 单机/演示场次没有登记媒体绑定，按场次的拾音通道 + 时间窗兜底，
        # 否则"场次原音与转写"永远是空的（录音明明在库里）
        from app.supervision_models import SupervisionChannel
        sources = [c.source_id for c in db.query(SupervisionChannel).filter_by(
            session_id=session_id, kind='audio', active=True)]
        if sources:
            rows = (db.query(AudioRecording, RecordingCaptureTime)
                .outerjoin(RecordingCaptureTime, RecordingCaptureTime.recording_id == AudioRecording.recording_id)
                .filter(AudioRecording.source_id.in_(sources),
                        AudioRecording.created_at >= start.replace(tzinfo=timezone.utc) - timedelta(minutes=10),
                        AudioRecording.created_at <= end.replace(tzinfo=timezone.utc) + timedelta(minutes=5))
                .order_by(AudioRecording.created_at.desc()).limit(30).all())
    return {'items':[{'recording_id':a.recording_id,'title':a.title,'status':a.status,'source_id':a.source_id,'duration_ms':a.duration_ms,
        'capture_started_at':iso_utc(c.started_at) if c else None,'created_at':iso_utc(a.created_at)} for a,c in rows]}


@router.get('/sessions')
def session_directory(archived_only:bool=False,project_id:str|None=None,keyword:str=Query('',max_length=200),page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100),db:Session=Depends(get_session)):
    from app.business_models import BusinessProject,BusinessRoom
    from app.supervision_models import SupervisionSessionReport
    query=db.query(EvalSession,PhaseBinding,BusinessProject,BusinessRoom).join(PhaseBinding,PhaseBinding.session_id==EvalSession.session_id).join(
        BusinessProject,BusinessProject.project_id==PhaseBinding.project_id).join(BusinessRoom,BusinessRoom.room_id==PhaseBinding.room_id)
    if archived_only:
        query=query.filter(db.query(SupervisionSessionReport.report_id).filter(
            SupervisionSessionReport.session_id==EvalSession.session_id,
            SupervisionSessionReport.snapshot['report_type'].astext=='归档监管记录').exists())
    if project_id:query=query.filter(PhaseBinding.project_id==project_id)
    if keyword.strip():
        from sqlalchemy import or_
        term='%'+keyword.strip().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        query=query.filter(or_(*(c.ilike(term,escape='\\') for c in (EvalSession.name,BusinessProject.name,BusinessRoom.name))))
    total=query.count()
    items=[]
    for session,binding,project,room in query.order_by(EvalSession.updated_at.desc()).offset((page-1)*page_size).limit(page_size):
        reports=db.query(SupervisionSessionReport).filter_by(session_id=session.session_id)
        if archived_only:reports=reports.filter(SupervisionSessionReport.snapshot['report_type'].astext=='归档监管记录')
        last=reports.order_by(SupervisionSessionReport.created_at.desc()).first()
        items.append({'session_id':session.session_id,'name':session.name,'status':session.status,'phase_id':session.current_phase,
            'project_id':project.project_id,'project_name':project.name,'room_id':room.room_id,'room_name':room.name,
            'started_at':iso_utc(session.start_at or session.created_at),'ended_at':iso_utc(session.end_at),
            'channels':[{**s,'created_at':iso_utc(s['created_at'])} for s in session_channels(db,session.session_id)],
            'latest_report':{'report_id':last.report_id,'at':iso_utc(last.created_at),'actor':last.actor} if last else None})
    return {'items':items,'total':total,'page':page,'page_size':page_size}


@router.post('/sessions/{session_id}/reports',status_code=201)
def generate_report(session_id:str,request:Request,db:Session=Depends(get_session)):
    from app.core.supervision_reporting import build_session_report
    from app.supervision_models import SupervisionSessionReport
    session=db.query(EvalSession).filter_by(session_id=session_id).with_for_update().first()
    if not session or not db.query(PhaseBinding).filter_by(session_id=session_id).first():raise HTTPException(404,'未找到已绑定监管范围的场次')
    row=SupervisionSessionReport(report_id=str(uuid4()),session_id=session_id,actor=actor(request),snapshot=build_session_report(db,session))
    db.add(row);db.commit()
    return {'report_id':row.report_id,'at':iso_utc(row.created_at),'snapshot':row.snapshot,'actor':row.actor}


@router.get('/sessions/{session_id}/reports')
def report_history(session_id:str,db:Session=Depends(get_session)):
    from app.supervision_models import SupervisionSessionReport
    return [{'report_id':r.report_id,'at':iso_utc(r.created_at),'actor':r.actor,'report_type':r.snapshot['report_type']}
        for r in db.query(SupervisionSessionReport).filter_by(session_id=session_id).order_by(SupervisionSessionReport.created_at.desc()).limit(30)]


@router.get('/reports/{report_id}')
def read_report(report_id:str,db:Session=Depends(get_session)):
    from app.supervision_models import SupervisionSessionReport
    row=db.get(SupervisionSessionReport,report_id)
    if not row:raise HTTPException(404,'监管报告不存在')
    return {'report_id':row.report_id,'at':iso_utc(row.created_at),'actor':row.actor,'snapshot':row.snapshot}


@router.get('/reports/{report_id}/download')
def download_report(report_id:str,format:str='html',db:Session=Depends(get_session)):
    from app.supervision_models import SupervisionSessionReport
    from app.core.supervision_reporting import report_html
    row=db.get(SupervisionSessionReport,report_id)
    if not row:raise HTTPException(404,'监管报告不存在')
    if format not in ('html','json'):raise HTTPException(422,'不支持的导出格式')
    body=report_html(row) if format=='html' else json.dumps({'report_id':row.report_id,'actor':row.actor,'at':iso_utc(row.created_at),'snapshot':row.snapshot},ensure_ascii=False,indent=2)
    return Response(body,media_type='text/html' if format=='html' else 'application/json',headers={
        'Content-Disposition':f'attachment; filename="supervision-session-{row.report_id}.{format}"','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})


class Mutation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    expected_version: int = Field(ge=0)
    note: str = Field(min_length=1,max_length=2000)


def begin_mutation(db,event_id,body,request,action):
    event = find_event(db,event_id,lock=True)
    who = actor(request)
    fingerprint = hashlib.sha256(json.dumps({'action':action,**body.model_dump(mode='json')},sort_keys=True).encode()).hexdigest()
    previous = db.get(SupervisionOperation,str(body.request_id))
    if previous:
        if (previous.actor,previous.event_id,previous.fingerprint)!=(who,event_id,fingerprint):
            raise HTTPException(409,'请求编号已被其他操作使用')
        return event,None,previous.result,fingerprint
    record = db.get(SupervisionCase,event_id)
    status = record.status if record else legacy_status(event)
    if status=='ARCHIVED':
        raise HTTPException(409,'已归档资料只读，请先发起重新核查后再补充关联证据')
    if body.expected_version!=(record.version if record else 0):
        raise HTTPException(409,'事件已更新，请刷新核对后再提交')
    if not body.note.strip():
        raise HTTPException(422,'请填写实际核查或关联依据')
    if record is None:
        record = SupervisionCase(event_id=event_id,status=status,version=0)
        db.add(record)
    return event,record,None,fingerprint


def finish_mutation(db,event,record,body,request,action,fingerprint):
    record.version+=1
    record.updated_at=datetime.utcnow()
    result={'event_id':event.event_id,'workflow_version':record.version,'workflow_status':record.status,'duplicate':False}
    db.add(SupervisionOperation(request_id=str(body.request_id),event_id=event.event_id,actor=actor(request),fingerprint=fingerprint,result=result))
    add_audit(db,event.event_id,action,body.note.strip(),actor(request))
    db.commit()
    publish_event(event,db)
    return result


class ManualEvent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    session_id: str = Field(min_length=1,max_length=36)
    source_id: str = Field(min_length=1,max_length=64)
    started_at: datetime
    ended_at: datetime
    reason: str = Field(min_length=1,max_length=4000)
    event_type: str = Field(default='manual_observation',pattern=r'^(manual_observation|phone_call|seat_absence|zone_intrusion|phone_capture|language_risk)$')
    risk_level: str = Field(default='medium',pattern=r'^(low|medium|high)$')


@router.post('/events/manual',status_code=201)
def manual_event(body:ManualEvent,request:Request,db:Session=Depends(get_session)):
    who=actor(request)
    fingerprint=hashlib.sha256(json.dumps(body.model_dump(mode='json'),sort_keys=True).encode()).hexdigest()
    from sqlalchemy import text
    db.execute(text('select pg_advisory_xact_lock(hashtext(:key))'),{'key':'manual:'+str(body.request_id)})
    prior=db.get(SupervisionOperation,str(body.request_id))
    if prior:
        if prior.actor!=who or prior.fingerprint!=fingerprint:
            raise HTTPException(409,'请求编号已被其他操作使用')
        return prior.result|{'duplicate':True}
    if body.started_at.tzinfo is None or body.ended_at.tzinfo is None:
        raise HTTPException(422,'现场时间必须包含时区')
    start,end=utc_naive(body.started_at),utc_naive(body.ended_at)
    if end<start or end>datetime.utcnow()+timedelta(seconds=5) or end-start>timedelta(hours=24) or not body.reason.strip():
        raise HTTPException(422,'请填写有效的现场起止时间和观察事实，单次标记最多24小时')
    scope=resolve_scope(db,body.source_id,start,body.session_id)
    if not scope or scope['session_id']!=body.session_id:
        raise HTTPException(422,'该来源在所选时间没有唯一有效的场次绑定，请先核对场次与时间')
    kind=next((s['kind'] for s in session_channels(db,body.session_id) if s['source_id']==body.source_id),'video')
    event=Event(event_id=str(uuid4()),session_id=body.session_id,camera_id=body.source_id,event_type=body.event_type,
        phase_id=scope['phase_id'],skill_code='manual_'+kind,skill_version='v1',risk_level=body.risk_level,
        reason=body.reason.strip(),occurred_at=start,status='PENDING',model_name='human_observation')
    db.add(event);db.flush()
    capture_context(event,db)
    db.add(SupervisionEventSpan(event_id=event.event_id,started_at=start,last_seen_at=end,ended_at=end,
        active=False,trigger_count=1,end_basis='manual'))
    add_audit(db,event.event_id,'manual_mark','人工观察事实：'+body.reason.strip(),who)
    result={'event_id':event.event_id,'workflow_version':0,'workflow_status':'PENDING','duplicate':False}
    db.add(SupervisionOperation(request_id=str(body.request_id),event_id=event.event_id,actor=who,fingerprint=fingerprint,result=result))
    db.commit();publish_event(event,db)
    return result


class VideoWindow(Mutation):
    source_id: str = Field(min_length=1,max_length=64)
    started_at: datetime
    ended_at: datetime


@router.put('/events/{event_id}/video-window')
def video_window(event_id:str,body:VideoWindow,request:Request,db:Session=Depends(get_session)):
    event,record,previous,fingerprint=begin_mutation(db,event_id,body,request,'video_window')
    if previous:return previous|{'duplicate':True}
    if not event.occurred_at or body.started_at.tzinfo is None or body.ended_at.tzinfo is None:
        raise HTTPException(422,'需要已登记的现场发生时间及带时区的证据起止时间')
    start,end=utc_naive(body.started_at),utc_naive(body.ended_at)
    behavior_start,behavior_end=event_range(db,event)
    if end<=start or end-start>timedelta(hours=24) or start>behavior_start or end<behavior_end:
        raise HTTPException(422,'证据时段须包含完整行为起止时间，最长24小时')
    from app.core.supervision import event_scope
    project,room,_=event_scope(event,db)
    scope=resolve_scope(db,body.source_id,event.occurred_at,event.session_id)
    candidates=session_channels(db,event.session_id)
    if not scope or (scope['project_id'],scope['room_id'])!=(project,room) or not any(s['source_id']==body.source_id and s['kind']=='video' for s in candidates):
        raise HTTPException(422,'只能关联同一场次、评标室及事发时间有效的已登记视频机位')
    old=next((w for w in video_windows(db,event) if w['source_id']==body.source_id),None)
    if old and (start>old['started_at'] or end<old['ended_at']):
        raise HTTPException(422,'补充证据只能扩展原范围，不能缩短已登记的上下文')
    row=db.get(SupervisionEvidenceWindow,(event_id,body.source_id))
    if row is None:
        row=SupervisionEvidenceWindow(event_id=event_id,source_id=body.source_id)
        db.add(row)
    row.started_at,row.ended_at,row.note,row.actor,row.updated_at=start,end,body.note.strip(),actor(request),datetime.utcnow()
    reset_preservation(db,event_id)
    return finish_mutation(db,event,record,body,request,'video_window',fingerprint)


class AudioAssociation(Mutation):
    recording_id: UUID


@router.post('/events/{event_id}/audio-links')
def link_audio(event_id:str,body:AudioAssociation,request:Request,db:Session=Depends(get_session)):
    event,record,previous,fingerprint=begin_mutation(db,event_id,body,request,'audio_link')
    if previous:return previous|{'duplicate':True}
    if db.get(SupervisionAudioLink,(event_id,str(body.recording_id))):
        raise HTTPException(409,'该录音已经关联，请刷新核对')
    candidate=next((a for a in event_context(db,event)['audio_candidates'] if a['recording_id']==str(body.recording_id)),None)
    if not candidate:
        raise HTTPException(422,'录音须绑定同一评标室、登记实际采集时间，并与行为时段重叠')
    db.add(SupervisionAudioLink(event_id=event_id,recording_id=str(body.recording_id),
        capture_started_at=utc_naive(datetime.fromisoformat(candidate['capture_started_at'].replace('Z','+00:00'))),note=body.note.strip(),actor=actor(request)))
    return finish_mutation(db,event,record,body,request,'audio_link',fingerprint)


class AnnotationIn(Mutation):
    recording_id: UUID
    segment_id: str = Field(min_length=1,max_length=36)
    kind: str = Field(pattern=r'^(transcript_correction|identity_note)$')
    correction: str = Field(min_length=1,max_length=4000)


@router.post('/events/{event_id}/annotations',status_code=201)
def annotate(event_id:str,body:AnnotationIn,request:Request,db:Session=Depends(get_session)):
    from app.audio_models import AudioSegment
    from app.supervision_models import SupervisionEvidenceAnnotation
    event,record,previous,fingerprint=begin_mutation(db,event_id,body,request,'evidence_annotation')
    if previous:return previous|{'duplicate':True}
    segment=db.query(AudioSegment).filter_by(recording_id=str(body.recording_id),segment_id=body.segment_id).first()
    context=event_context(db,event)
    if not segment or not any(a['recording_id']==str(body.recording_id) for a in context['audio']):
        raise HTTPException(422,'只能备注本事件已关联录音中的原始句段')
    if not body.correction.strip():raise HTTPException(422,'请填写勘误或核查内容')
    db.add(SupervisionEvidenceAnnotation(annotation_id=str(uuid4()),event_id=event_id,recording_id=str(body.recording_id),segment_id=segment.segment_id,
        kind=body.kind,correction=body.correction.strip(),original={'text':segment.text,'speaker':segment.speaker,'start_ms':segment.start_ms,'end_ms':segment.end_ms},actor=actor(request)))
    return finish_mutation(db,event,record,body,request,'evidence_annotation',fingerprint)


class RemoveLink(Mutation):
    kind: str=Field(pattern=r'^(video|audio)$')
    reference: str=Field(min_length=1,max_length=64)


@router.post('/events/{event_id}/remove-link')
def remove_link(event_id:str,body:RemoveLink,request:Request,db:Session=Depends(get_session)):
    event,record,previous,fingerprint=begin_mutation(db,event_id,body,request,'evidence_unlink')
    if previous:return previous|{'duplicate':True}
    model=SupervisionAudioLink if body.kind=='audio' else SupervisionEvidenceWindow
    link=db.get(model,(event_id,body.reference))
    if link is None:raise HTTPException(422,'只能撤销人工补充关联，原始来源和原语义录音不能移除')
    db.delete(link);reset_preservation(db,event_id)
    return finish_mutation(db,event,record,body,request,'evidence_unlink',fingerprint)


@router.get('/events/{event_id}/occurrences')
def occurrences(event_id:str,page:int=Query(1,ge=1),db:Session=Depends(get_session)):
    from app.supervision_models import SupervisionOccurrence
    find_event(db,event_id)
    rows=db.query(SupervisionOccurrence).filter_by(event_id=event_id)
    return {'total':rows.count(),'items':[{'id':r.occurrence_id,'at':iso_utc(r.occurred_at),'detail':r.detail}
        for r in rows.order_by(SupervisionOccurrence.occurred_at).offset((page-1)*50).limit(50)]}


@router.post('/events/{event_id}/receive')
def receive_task(event_id:str,body:Mutation,request:Request,db:Session=Depends(get_session)):
    event,record,previous,fingerprint=begin_mutation(db,event_id,body,request,'task_receive')
    if previous:return previous|{'duplicate':True}
    if record.status!='ASSIGNED':raise HTTPException(409,'当前没有待接收的处置任务')
    receipt=db.query(SupervisionTaskReceipt).filter_by(event_id=event_id).order_by(SupervisionTaskReceipt.assignment_version.desc()).first()
    if receipt is None:
        receipt=SupervisionTaskReceipt(event_id=event_id,assignment_version=record.version,assignee=record.assignee or '')
        db.add(receipt)
    if receipt.received_at:raise HTTPException(409,'本次处置任务已接收')
    user=request.state.admin
    if user.get('role')=='operator' and record.assignee!=user['username']:
        raise HTTPException(403,'只能接收分派给当前账号的处置任务')
    receipt.received_at,receipt.received_by,receipt.note=datetime.utcnow(),actor(request),body.note.strip()
    return finish_mutation(db,event,record,body,request,'task_receive',fingerprint)


class ChannelIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    source_id: str = Field(min_length=1,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')
    name: str = Field(min_length=1,max_length=128)
    kind: str = Field(default='video',pattern=r'^(video|audio)$')


@router.post('/sessions/{session_id}/channels',status_code=201)
def add_channel(session_id:str,body:ChannelIn,request:Request,db:Session=Depends(get_session)):
    from app.core.supervision import channel
    session=db.query(EvalSession).filter_by(session_id=session_id).with_for_update().first()
    if not session or not db.query(PhaseBinding).filter_by(session_id=session_id).first():raise HTTPException(404,'请先配置场次主绑定')
    if session.status=='closed':raise HTTPException(409,'已结束场次的来源配置只读')
    if not body.name.strip():raise HTTPException(422,'请填写机位或拾音源名称')
    from sqlalchemy import text
    # 串行检查跨场次来源，避免同时登记同一物理通道。
    db.execute(text('select pg_advisory_xact_lock(hashtext(:key))'),{'key':'channel:'+str(channel(body.source_id))})
    normalized=channel(body.source_id)
    if any(channel(s['source_id'])==normalized for s in session_channels(db,session_id)):
        raise HTTPException(409,'该来源已登记，摄像头别名不能重复绑定')
    occupied=db.execute(text('''SELECT 1 FROM (SELECT session_id,source_id,active FROM phase_bindings
        UNION ALL SELECT session_id,source_id,active FROM supervision_channels) b JOIN eval_sessions s ON s.session_id=b.session_id
        WHERE b.active AND s.status!='closed' AND supervision_channel(b.source_id)=supervision_channel(:source) LIMIT 1'''),{'source':body.source_id}).first()
    if occupied:raise HTTPException(409,'该来源已被其他在评场次使用')
    row=SupervisionChannel(channel_id=str(uuid4()),session_id=session_id,source_id=body.source_id,name=body.name.strip(),kind=body.kind,actor=actor(request))
    db.add(row);db.commit()
    return {'channel_id':row.channel_id,'source_id':row.source_id,'name':row.name,'kind':row.kind}
