import { coverageNotice } from '../api/workspace'
import { schemeLabels } from '../api/roomMonitoring'
import { useEffect, useState } from 'react';
import { Alert, Button, Card, Checkbox, Empty, Input, Drawer, Select, Space, Table, Tabs, Tag } from 'antd';
import { Link,useSearchParams } from 'react-router-dom';
import { PageHeading,RequestError,SupervisionList } from './Supervision';
import { supervisionRequest } from '../api/supervision';
import { downloadWorkspace,healthLabels,type SessionItem,type SessionReport } from '../api/workspace';
import { useRefreshingQuery } from '../components/workflow/useRefreshingQuery';
import { useAuthStore } from '../store/useAuthStore';
import { formatDateTime } from '../utils/ui';
import { resolutionLabels,workflowLabels } from '../components/workflow/supervision-model';

export default function SessionArchives(){
  const [params,setParams]=useSearchParams();
  const role=useAuthStore(s=>s.user?.role),canExport=role==='admin'||role==='supervisor';
  const [keyword,setKeyword]=useState(''),[q,setQ]=useState(''),[page,setPage]=useState(1),[busy,setBusy]=useState(''),[error,setError]=useState('');
  const query=useRefreshingQuery<{items:SessionItem[];total:number}>('archive-sessions:'+JSON.stringify([q,page]),signal=>supervisionRequest(`/supervision/sessions?archived_only=true&keyword=${encodeURIComponent(q)}&page=${page}`,{signal}));
  const [report,setReport]=useState<SessionReport|null>(null),[reportOpen,setReportOpen]=useState(false),[showAll,setShowAll]=useState(false);
  const [reportSession,setReportSession]=useState('');
  const [versions,setVersions]=useState<{report_id:string;at:string;actor:string;report_type:string}[]>([]);
  async function view(id:string,session:string){setBusy(id);setError('');try{const [record,history]=await Promise.all([supervisionRequest<SessionReport>('/supervision/reports/'+id),supervisionRequest<typeof versions>(`/supervision/sessions/${session}/reports`)]);setReport(record);setReportSession(session);setVersions(history.filter(v=>v.report_type==='归档监管记录'||v.report_id===id));setShowAll(false);setReportOpen(true);}catch(e){setError(e instanceof Error?e.message:'报告读取失败');}finally{setBusy('');}}
  const requestedReport=params.get('report_id'),requestedSession=params.get('session_id');
  useEffect(()=>{if(requestedReport&&requestedSession)void view(requestedReport,requestedSession)},[requestedReport,requestedSession]);
  const metrics:Record<string,string>={stream_status:'视频连接',recording_status:'录像状态',analysis_status:'分析状态',audio_status:'语音输入'};
  const periods=report?.snapshot.coverage.flatMap((c,index)=>c.intervals.filter(i=>showAll||!['online','running','recent','not_applicable'].includes(i.status)).map((i,k)=>({...i,key:index+':'+k,source:c.name+' / '+c.source_id,metric:metrics[c.metric],samples:c.samples})))||[];
  return <div className="sv-page console-archives"><PageHeading title="监管档案" description="查阅场次档案、事件结论和证据。" extra={<Button onClick={query.reload}>刷新</Button>}/>
    <RequestError error={error||query.error} reload={()=>{setError('');query.reload();}}/>
    <Tabs activeKey={params.get('tab')==='events'?'events':'sessions'} onChange={key=>setParams(previous=>{const next=new URLSearchParams(previous);next.set('tab',key);return next;})} items={[{key:'sessions',label:'场次档案',children:<Card className="console-archive-directory"><Input.Search aria-label="检索场次档案" placeholder="项目、评标室或场次名称" value={keyword} onChange={e=>setKeyword(e.target.value)} onSearch={value=>{setQ(value.trim());setPage(1);}} allowClear style={{maxWidth:420,marginBottom:18}}/>
      <Table rowKey="session_id" loading={query.loading} dataSource={query.data?.items||[]} scroll={{x:1050}} pagination={{current:page,pageSize:20,total:query.data?.total||0,onChange:setPage,showSizeChanger:false}} columns={[
        {title:'项目 / 场次',render:(_,s:SessionItem)=><div className="sv-event-cell"><strong>{s.project_name}</strong><small>{s.name}</small></div>},
        {title:'评标室',dataIndex:'room_name'},
        {title:'状态',render:(_,s:SessionItem)=><Tag>{({running:'进行中',draft:'未开始',closed:'已结束'} as Record<string,string>)[s.status]||s.status}</Tag>},
        {title:'监测来源',render:(_,s:SessionItem)=><span>{s.channels.filter(c=>c.kind==='video').length} 路画面 / {s.channels.filter(c=>c.kind==='audio').length} 路拾音</span>},
        {title:"最近归档",render:(_,s:SessionItem)=>s.latest_report?formatDateTime(s.latest_report.at):'尚未生成'},
        {title:'操作',width:230,render:(_,s:SessionItem)=><Space>{s.latest_report&&<Button size="small" disabled={Boolean(busy)} onClick={()=>void view(s.latest_report!.report_id,s.session_id)}>查看归档记录</Button>}<Link to={'/sessions/'+s.session_id}>场次详情</Link></Space>},
      ]} locale={{emptyText:<Empty description="暂无已归档场次"/>}}/>
    </Card>},{key:'events',label:'已归档事件',children:<SupervisionList mode="archive" embedded/>}]}/>
    <Drawer className="console-report-drawer" title="场次报告" open={reportOpen} width={1100} footer={<Space>{canExport&&report&&<><Button onClick={()=>void downloadWorkspace('/reports/'+report.report_id+'/download',"场次报告-"+report.report_id+'.html').catch(e=>setError(e.message))}>导出监管报告</Button></>}<Button onClick={()=>setReportOpen(false)}>关闭</Button></Space>} onClose={()=>setReportOpen(false)}>
      {error&&<Alert type="error" showIcon message={error}/>}
      {report&&<>{report.snapshot.report_type!=='归档监管记录'&&<Alert type="info" showIcon message="历史查阅记录" description="此记录为阶段性查阅记录，场次尚未正式归档。"/>}<Space wrap><Tag color="blue">{report.snapshot.report_type}</Tag><strong>{report.snapshot.project_name} / {report.snapshot.room_name}</strong><span>{report.snapshot.session_name}</span></Space>
        {versions.length>1&&<Select aria-label="历史报告" value={report.report_id} style={{width:'100%',marginTop:12}} options={versions.map(v=>({value:v.report_id,label:formatDateTime(v.at)+' · '+v.actor+' · '+v.report_type}))} disabled={Boolean(busy)} onChange={id=>{setBusy(id);void supervisionRequest<SessionReport>('/supervision/reports/'+id).then(setReport).catch(e=>setError(e.message)).finally(()=>setBusy(''));}}/>}
        <p>{formatDateTime(report.snapshot.started_at)} — {formatDateTime(report.snapshot.ended_at)} · 生成账号 {report.actor} · 未办结 {report.snapshot.unresolved} 条</p>
        {typeof report.snapshot.identity_verification_enabled==='boolean'&&<Tag>人员身份核查：{report.snapshot.identity_verification_enabled?'已启用':'未启用'}</Tag>}
        {report.snapshot.preparation_snapshot&&<Space wrap><Tag>监测方案：{schemeLabels[report.snapshot.preparation_snapshot.scheme as keyof typeof schemeLabels]||'既有监测方案'}</Tag>{report.snapshot.preparation_snapshot.version&&<Tag>现场资源版本：{report.snapshot.preparation_snapshot.version}</Tag>}</Space>}
        <Alert type="warning" showIcon message="监测覆盖情况" description={coverageNotice(report.snapshot.notice)}/><Checkbox checked={showAll} onChange={e=>setShowAll(e.target.checked)}>显示全部监测区间</Checkbox>
        <Table size="small" rowKey="key" dataSource={periods} scroll={{x:800}} pagination={{pageSize:8,showSizeChanger:false}} columns={[{title:'来源',dataIndex:'source'},{title:'核验项目',dataIndex:'metric'},{title:'开始',dataIndex:'started_at',render:formatDateTime},{title:'结束',dataIndex:'ended_at',render:formatDateTime},{title:'状态',dataIndex:'status',render:s=><Tag color={s==='unknown'?'gold':'default'}>{healthLabels[s]||s}</Tag>}]} locale={{emptyText:"当前筛选无记录，可勾选显示全部区间"}}/>
        <h3>事件与人工结论</h3><Table size="small" rowKey="event_id" dataSource={report.snapshot.events} pagination={{pageSize:6,showSizeChanger:false}} columns={[{title:'线索',dataIndex:'reason'},{title:'流程',dataIndex:'workflow_status',render:s=>workflowLabels[s]||s},{title:'人工结论',dataIndex:'resolution',render:s=>resolutionLabels[s]||'尚未认定'},{title:'事件档案',render:(_,e)=><Link to={'/event-detail?event_id='+e.event_id} state={{returnTo:'/archives?report_id='+report.report_id+'&session_id='+reportSession}} onClick={()=>setReportOpen(false)}>查看</Link>}]} locale={{emptyText:"本报告无告警记录，现场情况请结合监测覆盖核查"}}/>
        {report.snapshot.archive_note&&<><h3>归档确认</h3><p>{report.snapshot.archive_note}</p>{report.snapshot.evidence_gap_note&&<Alert type="info" message="资料缺失与覆盖说明" description={report.snapshot.evidence_gap_note}/>}<h3>场次办理记录</h3><Table size="small" rowKey="journal_id" dataSource={report.snapshot.session_journal||[]} pagination={{pageSize:5}} columns={[{title:'时间',dataIndex:'at',render:formatDateTime},{title:'办理账号',dataIndex:'actor'},{title:'记录',dataIndex:'note'}]}/></>}
        <small>报告编号：{report.report_id} · 生成时间：{formatDateTime(report.at)}</small>
      </>}
    </Drawer>
  </div>;
}
