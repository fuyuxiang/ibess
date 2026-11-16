"""监测状态采样与场次监管记录。没有采样的数据明确计入待核验区间。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.request import urlopen

from app.models import Event, EvalSession
from app.supervision_models import SupervisionSignal, SupervisionCoverageSample, SupervisionSessionReport
from app.core.supervision import channel
from app.core.supervision_evidence import utc_naive, session_channels


COVERAGE_NOTICE = '每 30 秒采样，状态有效期最长 60 秒。缺少有效记录的区间标为待核验；录像完整性和现场情况需结合证据核查。'
LEGACY_COVERAGE_NOTICE = '状态记录为每30秒采样，单次有效期最多60秒；未采样、未接入或无有效回执均为待核验。近期录像活动不等于全程完整。无预警不代表现场无异常。'

def source_health(db,configured):
    now=datetime.utcnow()
    signals={(s.session_id,s.source_id,s.kind):s for s in db.query(SupervisionSignal).all()}
    def probe(item):
        state='not_applicable' if item.get('kind')=='audio' else 'unconfigured'
        name=channel(item['source_id'])
        if item.get('kind')!='audio' and name:
            try:
                with urlopen(f'http://mediamtx:8888/{name}/index.m3u8',timeout=1.2) as response:
                    state='online' if response.status==200 else 'offline'
            except Exception:
                state='offline'
        return item|{'stream_status':state}
    with ThreadPoolExecutor(max_workers=8) as pool:
        items=list(pool.map(probe,configured))
    from app.routers.media_clips import _segment_files
    for item in items:
        kind=item.get('kind','video')
        signal=signals.get((item['session_id'],channel(item['source_id']),kind))
        fresh=signal and 0<=(now-signal.observed_at).total_seconds()<=30
        failed=fresh and signal.detail.get('observation_type') in ('video_offline','audio_offline','camera_blacked')
        item['analysis_status']='input_error' if failed else 'running' if fresh else 'unknown'
        item['analysis_at']=signal.observed_at.replace(tzinfo=timezone.utc).isoformat() if signal else None
        item['audio_status']=('recent' if fresh and not failed else 'offline' if failed else 'unknown') if kind=='audio' else 'not_applicable'
        item['recording_status']='not_applicable' if kind=='audio' else 'unknown'
        if kind=='video':
            files=_segment_files(channel(item['source_id']))
            if files:
                try:
                    item['recording_status']='recent' if 0<=(now.replace(tzinfo=timezone.utc).timestamp()-files[-1][0])<=45 and files[-1][1].stat().st_size>0 else 'stale'
                except OSError:
                    pass
        item['status_basis']='画面为播放列表探测；录像为近期文件活动；分析为30秒内已收到的来源观察，不代表算法准确率或全程录像完整性'
    return items


def sample_coverage(factory):
    from app.routers.supervision import sources
    with factory() as db:
        items=source_health(db,sources(db)['items'])
        now=datetime.utcnow()
        for item in items:
            db.add(SupervisionCoverageSample(session_id=item['session_id'],source_id=item['source_id'],observed_at=now,
                states={key:item.get(key) for key in ('stream_status','recording_status','analysis_status','audio_status','kind','status_basis')}))
        db.commit()


def coverage_intervals(samples,start,end,field):
    """一次状态采样最多解释后续60秒，服务停机产生的空白不会被延长为正常。"""
    periods=[]
    cursor=start
    ordered=sorted(samples,key=lambda row:row.observed_at)
    for index,sample in enumerate(ordered):
        at=sample.observed_at
        if at>end:break
        until=min(end,at+timedelta(seconds=60))
        if until<=cursor:continue
        if at>cursor:
            periods.append((cursor,min(at,end),'unknown'))
        left=max(cursor,at,start)
        next_at=ordered[index+1].observed_at if index+1<len(ordered) else end
        right=min(until,next_at)
        if right>left:
            periods.append((left,right,sample.states.get(field) or 'unknown'))
            cursor=right
    if cursor<end:periods.append((cursor,end,'unknown'))
    merged=[]
    for left,right,status in periods:
        if right<=left:continue
        if merged and merged[-1][2]==status and merged[-1][1]==left:
            merged[-1]=(merged[-1][0],right,status)
        else:merged.append((left,right,status))
    return merged


def build_session_report(db,session):
    from app.core.workflow import event_dict,iso_utc
    from app.phase_models import PhaseBinding
    from app.business_models import BusinessProject,BusinessRoom
    from app.supervision_models import SupervisionArchive
    binding=db.query(PhaseBinding).filter_by(session_id=session.session_id).one()
    project,room=db.get(BusinessProject,binding.project_id),db.get(BusinessRoom,binding.room_id)
    start=utc_naive(session.start_at or session.created_at)
    end=min(datetime.utcnow(),utc_naive(session.end_at or datetime.utcnow()))
    end=max(start,end)
    channels=session_channels(db,session.session_id)
    coverage=[]
    for source in channels:
        samples=db.query(SupervisionCoverageSample).filter_by(session_id=session.session_id,source_id=source['source_id']).filter(
            SupervisionCoverageSample.observed_at>=start-timedelta(seconds=60),SupervisionCoverageSample.observed_at<=end).order_by(SupervisionCoverageSample.observed_at).all()
        fields=('audio_status','analysis_status') if source['kind']=='audio' else ('stream_status','recording_status','analysis_status')
        for field in fields:
            periods=coverage_intervals(samples,start,end,field)
            coverage.append({'source_id':source['source_id'],'name':source['name'],'metric':field,'samples':len(samples),
                'intervals':[{'started_at':iso_utc(a),'ended_at':iso_utc(b),'status':state,'seconds':round((b-a).total_seconds(),1)} for a,b,state in periods]})
    rows=db.query(Event).filter_by(session_id=session.session_id).filter(Event.risk_level.in_(['low','medium','high'])).order_by(Event.occurred_at).all()
    events=[]
    for row in rows:
        item=event_dict(row,db)
        item['archives']=[{'version':a.version,'resolution':a.resolution,'actor':a.actor,'at':iso_utc(a.created_at),
            'evidence_status':a.snapshot.get('evidence_status'),'evidence_note':a.evidence_note} for a in db.query(SupervisionArchive).filter_by(event_id=row.event_id).order_by(SupervisionArchive.version)]
        events.append(item)
    return {'session_id':session.session_id,'session_name':session.name,'session_status':session.status,
        'project_id':project.project_id,'project_name':project.name,'room_id':room.room_id,'room_name':room.name,
        'started_at':iso_utc(start),'ended_at':iso_utc(end),'channels':[{**s,'created_at':iso_utc(s['created_at'])} for s in channels],
        'events':events,'coverage':coverage,'unresolved':sum(e['workflow_status']!='ARCHIVED' for e in events),
        'notice':COVERAGE_NOTICE,
        'report_type':'现场结束记录（待归档核验）' if session.status=='closed' else '进行中场次快照'}


def report_html(report):
    from app.core.supervision import STATE_LABELS,RESOLUTION_LABELS
    data=report.snapshot
    def txt(value):return escape(str(value if value is not None else '未记录'))
    def stamp(value):
        if not value:return '未记录'
        return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
    def table(headers,rows):
        return '<table><thead><tr>'+''.join('<th>'+txt(h)+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+txt(c)+'</td>' for c in row)+'</tr>' for row in rows)+'</tbody></table>'
    status={'online':'画面可连接','offline':'不可用','running':'近期有分析结果','recent':'近期有媒体记录','stale':'近期无录像记录',
        'unknown':'待核验','input_error':'分析输入异常','not_applicable':'不适用','unconfigured':'未配置'}
    metrics={'stream_status':'视频连接','recording_status':'录像状态','analysis_status':'分析状态','audio_status':'语音输入'}
    evidence_status={'READY':'证据已保全','PARTIAL':'录像保全不完整','PENDING':'录像保全中',
        'UNAVAILABLE':'证据不可用','NOT_APPLICABLE':'模拟数据，无现场证据'}
    journal_names={'preparation':'评前准备','start':'开始监管','phase':'阶段推进','patrol':'现场巡查',
        'handover':'值守交接','finish':'结束监管','archive':'归档确认','supplement':'补充记录','permit':'临时许可'}
    body=f'<h1>衡鉴 HengJian · 场次报告</h1><p>{txt(data["report_type"])} · 编号 {txt(report.report_id)}</p>'
    body+=table(['项目','评标室','场次','监管时段（北京时间）','生成账号'],[[data['project_name'],data['room_name'],data['session_name'],stamp(data['started_at'])+' — '+stamp(data['ended_at']),report.actor]])
    if isinstance(data.get('identity_verification_enabled'), bool):
        body+='<p>人员身份核查：'+('已启用' if data['identity_verification_enabled'] else '未启用')+'</p>'
    if data.get('preparation_snapshot'):
        from app.application.monitoring_profiles import PROFILE_LABELS
        preparation=data['preparation_snapshot']
        body+='<p>监测方案：'+txt(PROFILE_LABELS.get(preparation.get('scheme'),'既有监测方案'))
        if preparation.get('version'):
            body+=' · 现场资源版本：'+txt(preparation['version'])
        body+='</p>'
    body+='<h2>监测覆盖情况</h2><p class="notice">'+txt(COVERAGE_NOTICE if data['notice']==LEGACY_COVERAGE_NOTICE else data['notice'])+'</p>'
    rows=[]
    for item in data['coverage']:
        for interval in item['intervals']:
            rows.append([item['name']+' / '+item['source_id'],metrics[item['metric']],stamp(interval['started_at']),stamp(interval['ended_at']),status.get(interval['status'],interval['status'])])
    body+=table(['来源','核验项目','开始','结束','状态'],rows) if rows else '<p>未登记监测来源，覆盖情况待核验。</p>'
    body+='<h2>告警与处置记录</h2>'
    body+=table(['事件编号','线索描述','来源','发生时间','处理状态','人工结论','责任/接收','归档时间'],[[e['event_id'],e['reason'],e['camera_id'],stamp(e['occurred_at']),STATE_LABELS.get(e['workflow_status'],e['workflow_status']),RESOLUTION_LABELS.get(e['resolution'],'尚未认定'),(e['assignee'] or '未分派')+' / '+((e.get('task_receipt') or {}).get('received_by') or '待接收'),stamp(e['archived_at'])] for e in data['events']]) if data['events'] else '<p>本报告无告警记录，现场情况请结合监测覆盖核查。</p>'
    body+='<h2>历次归档及资料缺失说明</h2>'+table(['事件编号','流程版本','人工结论','归档账号','归档资料状态','资料缺失说明'],[[e['event_id'],a['version'],RESOLUTION_LABELS.get(a['resolution'],'未记录'),a['actor'],evidence_status.get(a['evidence_status'],'待核验'),a['evidence_note']] for e in data['events'] for a in e['archives']])
    if data.get('archive_note'):
        body+='<h2>归档确认</h2><p>'+txt(data['archive_note'])+'</p>'
        if data.get('evidence_gap_note'):body+='<p class="notice">资料缺失与覆盖说明：'+txt(data['evidence_gap_note'])+'</p>'
    if data.get('session_journal'):
        body+='<h2>场次操作记录</h2>'+table(['记录类型','时间','记录账号','内容'],[[journal_names.get(j['kind'],'操作记录'),stamp(j['at']),j['actor'],j['note']] for j in reversed(data['session_journal'])])
    body+=f'<p>未办结事件：{data["unresolved"]} 条。生成时间：{stamp(report.created_at.replace(tzinfo=timezone.utc).isoformat())}（北京时间）。</p><p>本报告按生成时的记录归集，完整证据与处置详情请查阅事件档案。</p>'
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>场次报告</title><style>body{font:14px/1.7 system-ui,sans-serif;color:#20344a;margin:32px}h1{font-size:24px}h2{font-size:18px;margin-top:28px}table{border-collapse:collapse;width:100%;font-size:12px}th,td{border:1px solid #cbd5e1;padding:8px;text-align:left;overflow-wrap:anywhere}th{background:#eef3f8}.notice{padding:12px;background:#fff8df}@media print{body{margin:0}tr{break-inside:avoid}thead{display:table-header-group}}@page{size:A4 landscape;margin:16mm}</style>'+body+'</html>'
