"""监管平台作用域与历史快照；schema 由 API 就绪阶段创建。"""
import json
import re
import hashlib
from datetime import datetime, timedelta, timezone
from sqlalchemy import text


def normalize_source(value):
    if value in ('camera-real-01','cam-ezviz'):
        return 'cam-ezviz'
    match = re.fullmatch(r'(?:camera-0*|cam)(\d+)', value or '')
    return 'cam'+str(int(match[1])) if match else value


def observation_scope(factory, data):
    when = datetime.fromtimestamp(float(data['occurred_at']), timezone.utc)
    with factory() as db:
        value = db.execute(text('select supervision_scope(:source,:at,:hint)'),
            {'source':data.get('source_id'), 'at':when, 'hint':data.get('session_id')}).scalar()
        return json.loads(value) if isinstance(value,str) else value


def save_context(db, event, scope):
    if not scope or event.risk_level == 'info':
        return
    db.execute(text('''INSERT INTO business_event_contexts(event_id,project_id,room_id,snapshot)
        VALUES (:event,:project,:room,CAST(:snapshot AS jsonb)) ON CONFLICT(event_id) DO NOTHING'''),
        {'event':event.event_id,'project':scope['project_id'],'room':scope['room_id'], 'snapshot':json.dumps(scope,ensure_ascii=False)})


def episode_key(event,subject,media_run=None):
    values=(event.session_id,normalize_source(event.camera_id),event.skill_code,event.skill_version,event.phase_id,subject,media_run)
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def persist_episode(db,event):
    """只归并明确工位、区域或轮次内轨迹；匿名人员不按同场同时间强行合并。"""
    payload=getattr(event,'_episode',None)
    if not payload or db.get_bind().dialect.name!='postgresql':
        return event,True
    from service.models import Event
    group=payload['group']
    db.execute(text('select pg_advisory_xact_lock(hashtext(:key))'),{'key':'episode:'+group})
    prior=db.execute(text('''SELECT event_id,last_seen_at,started_at FROM supervision_event_spans
        WHERE group_key=:group AND active=true ORDER BY last_seen_at DESC LIMIT 1 FOR UPDATE'''),{'group':group}).mappings().first()
    observed=event.occurred_at
    if prior and observed<prior['last_seen_at']:
        # 乱序重投不倒退行为状态，也不创建重复待办。
        return db.query(Event).filter_by(event_id=prior['event_id']).one(),False
    same=bool(prior and observed-prior['last_seen_at']<=timedelta(seconds=30))
    if same:
        target=db.query(Event).filter_by(event_id=prior['event_id']).with_for_update().one()
    else:
        if prior:
            db.execute(text("UPDATE supervision_event_spans SET active=false,end_basis='observation_gap' WHERE event_id=:id"),{'id':prior['event_id']})
        target=event
        db.add(target);db.flush()
        db.execute(text('''INSERT INTO supervision_event_spans(event_id,group_key,started_at,last_seen_at,active,trigger_count)
            VALUES (:id,:group,:start,:seen,true,0)'''),{'id':target.event_id,'group':group,'start':payload['started_at'],'seen':observed})
    occurrence=hashlib.sha256(json.dumps([group,observed.isoformat(),payload['detail']],sort_keys=True,default=str).encode()).hexdigest()
    inserted=db.execute(text('''INSERT INTO supervision_occurrences(occurrence_id,event_id,occurred_at,detail)
        VALUES (:key,:id,:seen,CAST(:detail AS jsonb)) ON CONFLICT DO NOTHING RETURNING occurrence_id'''),
        {'key':occurrence,'id':target.event_id,'seen':observed,'detail':json.dumps(payload['detail'],ensure_ascii=False,default=str)}).scalar()
    if inserted:
        db.execute(text('''UPDATE supervision_event_spans SET last_seen_at=:seen,started_at=least(started_at,:start),trigger_count=trigger_count+1
            WHERE event_id=:id'''),{'id':target.event_id,'seen':observed,'start':payload['started_at']})
        db.execute(text("UPDATE supervision_media_holds SET status=CASE WHEN manifest='[]'::jsonb THEN 'PENDING' ELSE 'PARTIAL' END,next_retry_at=:at WHERE event_id=:id"),
            {'id':target.event_id,'at':datetime.utcnow()})
    return target,not same


def finish_observed_episodes(factory,data,groups,applied_codes,scopes=None):
    if not applied_codes:return
    with factory() as db:
        if db.get_bind().dialect.name!='postgresql':return
        when=datetime.fromtimestamp(float(data['occurred_at']),timezone.utc).replace(tzinfo=None)
        ended=db.execute(text('''UPDATE supervision_event_spans sp SET active=false,ended_at=:at,end_basis='observed_clear'
            FROM events e WHERE e.event_id=sp.event_id AND sp.active=true AND e.session_id=:session
            AND supervision_channel(e.camera_id)=supervision_channel(:source) AND e.skill_code=ANY(:codes)
            AND (CAST(:scopes AS jsonb) IS NULL OR EXISTS (SELECT 1 FROM jsonb_array_elements(CAST(:scopes AS jsonb)) rule
                WHERE rule->>0=e.skill_code AND (rule->>1 IS NULL OR rule->>1=e.zone_id)))
            AND sp.last_seen_at<=:at AND sp.group_key!=ALL(:groups) RETURNING sp.event_id'''),
            {'at':when,'session':data.get('session_id'),'source':data.get('source_id'),'codes':list(applied_codes),'groups':list(groups),'scopes':json.dumps(list(scopes)) if scopes is not None else None}).scalars().all()
        if ended:
            db.execute(text("UPDATE supervision_media_holds SET status=CASE WHEN manifest='[]'::jsonb THEN 'PENDING' ELSE 'PARTIAL' END,next_retry_at=:at WHERE event_id=ANY(:ids)"),{'ids':ended,'at':datetime.utcnow()})
        db.commit()


def record_signal(factory,data):
    if not data.get('session_id'):return
    with factory() as db:
        if db.get_bind().dialect.name!='postgresql':return
        kind='core' if data.get('type')=='behavior_detected' else 'audio' if data.get('type') in ('transcript_text','sensitive_word','long_talking','audio_offline','audio_recovered') else 'video'
        when=datetime.fromtimestamp(float(data['occurred_at']),timezone.utc).replace(tzinfo=None)
        db.execute(text('''INSERT INTO supervision_signals(session_id,source_id,kind,observed_at,detail)
            VALUES (:session,:source,:kind,:at,CAST(:detail AS jsonb)) ON CONFLICT(session_id,source_id,kind)
            DO UPDATE SET observed_at=excluded.observed_at,detail=excluded.detail WHERE supervision_signals.observed_at<excluded.observed_at - interval '5 seconds' OR supervision_signals.detail IS DISTINCT FROM excluded.detail'''),
            {'session':data['session_id'],'source':normalize_source(data.get('source_id')),'kind':kind,'at':when,
             'detail':json.dumps({'observation_type':data.get('type'), **({'capabilities': json.loads(data.get('payload','{}')).get('capabilities',{})} if kind=='core' else {})})})
        db.commit()
