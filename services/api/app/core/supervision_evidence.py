"""事件的行为时间、关联媒体与事发时业务依据。候选资料不自动成为证据。"""
from datetime import datetime, timedelta, timezone

from app.models import Event
from app.phase_models import PhaseBinding
from app.supervision_models import (SupervisionAudioLink, SupervisionChannel, SupervisionEventSpan,
    SupervisionEvidenceWindow, SemanticEventLink, RecordingCaptureTime)


def utc_naive(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value and value.tzinfo else value


def event_range(db, event):
    span = db.get(SupervisionEventSpan, event.event_id)
    start = span.started_at if span else event.occurred_at
    end = (span.ended_at or span.last_seen_at) if span else event.occurred_at
    if not span:
        link = db.query(SemanticEventLink).filter_by(event_id=event.event_id).first()
        if link:
            from app.semantic_models import SemanticAnalysis
            row = db.get(SemanticAnalysis,link.analysis_id)
            raw = (row.request_snapshot or {}) if row else {}
            if raw.get('time_basis')=='capture_start' and raw.get('occurred_at'):
                origin = utc_naive(datetime.fromisoformat(raw['occurred_at'].replace('Z','+00:00')))
                start,end = origin+timedelta(milliseconds=row.window_start_ms),origin+timedelta(milliseconds=row.window_end_ms)
    return utc_naive(start),utc_naive(end)


def is_audio_event(db,event):
    return bool(db.query(SemanticEventLink).filter_by(event_id=event.event_id).first()
        or (event.camera_id or '').startswith('audio') or (event.skill_code or '')=='manual_audio'
        or db.query(SupervisionChannel).filter_by(session_id=event.session_id,source_id=event.camera_id,kind='audio').first())


def video_windows(db,event):
    start,end = event_range(db,event)
    if not start or not end:
        return []
    result = {}
    if not is_audio_event(db,event):
        result[event.camera_id] = {'source_id':event.camera_id,'started_at':start-timedelta(seconds=45),
            'ended_at':end+timedelta(seconds=45),'note':'行为起止时段及前后45秒上下文','actor':'system'}
    for row in db.query(SupervisionEvidenceWindow).filter_by(event_id=event.event_id):
        result[row.source_id] = {'source_id':row.source_id,'started_at':min(row.started_at,start-timedelta(seconds=45)),
            'ended_at':max(row.ended_at,end+timedelta(seconds=45)),'note':row.note,'actor':row.actor}
    return list(result.values())


def audio_links(db,event):
    from app.audio_models import AudioRecording
    from app.semantic_models import SemanticAnalysis
    result = {}
    semantic = db.query(SemanticEventLink).filter_by(event_id=event.event_id).first()
    if semantic:
        row = db.get(SemanticAnalysis,semantic.analysis_id)
        if row:
            snapshot = row.request_snapshot or {}
            origin = snapshot.get('occurred_at') if snapshot.get('time_basis')=='capture_start' else None
            result[row.recording_id] = {'recording_id':row.recording_id,
                'capture_started_at':utc_naive(datetime.fromisoformat(origin.replace('Z','+00:00'))) if origin else None,
                'note':'原语义分析使用的录音','actor':'system','window_start_ms':row.window_start_ms,'window_end_ms':row.window_end_ms}
    start,end = event_range(db,event)
    for row in db.query(SupervisionAudioLink).filter_by(event_id=event.event_id):
        result.setdefault(row.recording_id,{'recording_id':row.recording_id,'capture_started_at':row.capture_started_at,
            'note':row.note,'actor':row.actor})
    for rid,item in result.items():
        recording = db.get(AudioRecording,rid)
        if not recording:
            continue
        item.update(title=recording.title,sha256=recording.sha256,duration_ms=recording.duration_ms,
            playback_url=f'/api/audio/recordings/{rid}/original')
        origin = item['capture_started_at']
        if 'window_start_ms' not in item:
            item['window_start_ms'] = max(0,int((start-origin).total_seconds()*1000)-15000) if origin and start else 0
            item['window_end_ms'] = min(recording.duration_ms,int((end-origin).total_seconds()*1000)+15000) if origin and end else recording.duration_ms
    return list(result.values())


def session_channels(db,session_id):
    binding = db.query(PhaseBinding).filter_by(session_id=session_id).first()
    result = []
    if binding:
        result.append({'source_id':binding.source_id,'name':'主监测机位','kind':'video','active':binding.active,'created_at':utc_naive(binding.created_at)})
        result.extend({'source_id':c.source_id,'name':c.name,'kind':c.kind,'active':c.active,'created_at':c.created_at}
            for c in db.query(SupervisionChannel).filter_by(session_id=session_id))
    return result


def event_context(db,event):
    from app.core.workflow import iso_utc
    from app.core.supervision import event_scope
    from app.business_facts_models import BusinessAttendanceFact
    from app.business_authz_models import BusinessAuthorizationSnapshot
    from app.audio_models import AudioRecording
    from app.media_binding_models import MediaBinding
    project,room,_ = event_scope(event,db)
    start,end = event_range(db,event)
    linked = audio_links(db,event)
    candidates = []
    if project and room and start:
        rows = (db.query(AudioRecording,RecordingCaptureTime).join(RecordingCaptureTime,RecordingCaptureTime.recording_id==AudioRecording.recording_id)
            .join(MediaBinding,MediaBinding.recording_id==AudioRecording.recording_id)
            .filter(MediaBinding.room_id==room,RecordingCaptureTime.started_at<=end.replace(tzinfo=timezone.utc)).all())
        for recording,capture in rows:
            origin = utc_naive(capture.started_at)
            if origin+timedelta(milliseconds=recording.duration_ms)<start or any(a['recording_id']==recording.recording_id for a in linked):
                continue
            candidates.append({'recording_id':recording.recording_id,'title':recording.title,'capture_started_at':iso_utc(origin),'duration_ms':recording.duration_ms})
    if not candidates and start and event.camera_id:
        # 单机/演示场次没有项目与评标室归属，按机位 + 时间窗兜底。
        # 录音的 created_at 是落库时间（转写完成后才写），比事件发生时刻晚几十秒到几分钟，
        # 所以用较宽的窗口取候选，再按"离事件最近"排序。
        # occurred_at 的历史数据里混有本地时间，created_at 统一是 UTC，用它做锚点更稳
        anchor = utc_naive(event.created_at) if getattr(event,'created_at',None) else start
        margin = timedelta(seconds=300)
        rows = (db.query(AudioRecording).filter(AudioRecording.source_id==event.camera_id,
                AudioRecording.status=='ready',
                AudioRecording.created_at>=anchor.replace(tzinfo=timezone.utc)-margin,
                AudioRecording.created_at<=anchor.replace(tzinfo=timezone.utc)+margin)
            .order_by(AudioRecording.created_at).limit(20).all())
        scored = []
        for recording in rows:
            origin = utc_naive(recording.created_at)-timedelta(milliseconds=recording.duration_ms or 0)
            distance = abs((utc_naive(recording.created_at)-anchor).total_seconds())
            scored.append((distance,{'recording_id':recording.recording_id,'title':recording.title,
                'capture_started_at':iso_utc(origin),'duration_ms':recording.duration_ms}))
        candidates = [item for _,item in sorted(scored,key=lambda x:x[0])]
    if not linked and candidates:
        # 没有人工/semantic 关联时，直接用时间最接近的一段作为"可看资料"
        for item in candidates[:2]:
            linked.append({'recording_id':item['recording_id'],'title':item.get('title'),
                'capture_started_at':utc_naive(datetime.fromisoformat(item['capture_started_at'].replace('Z','+00:00'))),
                'duration_ms':item.get('duration_ms'),'note':'按机位与时间窗自动关联','actor':'system',
                'window_start_ms':0,'window_end_ms':item.get('duration_ms') or 0})
    facts = []
    authorization = None
    if project and room:
        rows = db.query(BusinessAttendanceFact).filter_by(project_id=project,room_id=room)
        if start:
            rows = rows.filter(BusinessAttendanceFact.occurred_at>=start.replace(tzinfo=timezone.utc)-timedelta(days=1),
                               BusinessAttendanceFact.occurred_at<=end.replace(tzinfo=timezone.utc)+timedelta(hours=1))
        for fact in rows.order_by(BusinessAttendanceFact.occurred_at.desc()).limit(100):
            facts.append({'fact_id':fact.fact_id,'type':fact.fact_type,'person_id':fact.person_id,'seat_id':fact.seat_id,
                'person_name':fact.person.name if fact.person else None,'seat_code':fact.seat.code if fact.seat else None,
                'phase_id':fact.phase_id,'occurred_at':iso_utc(fact.occurred_at),'registered_at':iso_utc(fact.created_at),
                'registered_after_event':bool(start and utc_naive(fact.created_at)>start),'note':fact.note,'payload':fact.payload})
        if start:
            row = db.query(BusinessAuthorizationSnapshot).filter_by(project_id=project,room_id=room).filter(
                BusinessAuthorizationSnapshot.created_at<=start.replace(tzinfo=timezone.utc)).order_by(BusinessAuthorizationSnapshot.created_at.desc()).first()
            if row:
                authorization = {'snapshot_id':row.snapshot_id,'at':iso_utc(row.created_at),'phase_id':row.phase_id,
                    'visibility':row.information_visibility,'policy':row.confidentiality_policy_id,
                    'entries':[{'person_id':e.person_id,'name':e.person.name if e.person else e.person_id,'role':e.role,
                        'identity_verified':e.identity_verified,'recipient_authorized':e.recipient_authorized} for e in row.entries]}
    videos = video_windows(db,event)
    from app.supervision_models import SupervisionEvidenceAnnotation
    annotations=[{'annotation_id':a.annotation_id,'recording_id':a.recording_id,'segment_id':a.segment_id,'kind':a.kind,
        'original':a.original,'correction':a.correction,'actor':a.actor,'at':iso_utc(a.created_at)} for a in db.query(SupervisionEvidenceAnnotation).filter_by(event_id=event.event_id).order_by(SupervisionEvidenceAnnotation.created_at)]
    return {'range':{'started_at':iso_utc(start),'ended_at':iso_utc(end)},'annotations':annotations,
        'videos':[{**w,'started_at':iso_utc(w['started_at']),'ended_at':iso_utc(w['ended_at'])} for w in videos],
        'video_candidates':[{'source_id':s['source_id'],'name':s['name']} for s in session_channels(db,event.session_id)
            if s['kind']=='video' and not any(w['source_id']==s['source_id'] for w in videos)],
        'audio':[_audio_item(db,a) for a in linked],
        'audio_candidates':candidates,'facts':facts,'authorization':authorization,
        'identity_notice':'同场同时段的资料只表示时间关联，不证明说话人与画面人员身份一致'}


def _audio_item(db,item):
    """录音条目补充播放地址与转写片段，供事件详情直接查看。"""
    from app.audio_models import AudioSegment
    from app.core.workflow import iso_utc
    out = {**item,'capture_started_at':iso_utc(item['capture_started_at'])}
    rid = item['recording_id']
    out.setdefault('playback_url',f'/api/audio/recordings/{rid}/original')
    rows = (db.query(AudioSegment).filter(AudioSegment.recording_id==rid)
        .order_by(AudioSegment.start_ms).limit(8).all())
    out['segments'] = [{'segment_id':r.segment_id,'speaker':r.speaker,'start_ms':r.start_ms,
        'end_ms':r.end_ms,'text':r.text} for r in rows]
    return out

def reset_preservation(db,event_id):
    from app.supervision_models import SupervisionMediaHold
    hold = db.get(SupervisionMediaHold,event_id)
    if hold:
        hold.status = 'PARTIAL' if hold.manifest else 'PENDING'
        hold.next_retry_at = datetime.utcnow()
