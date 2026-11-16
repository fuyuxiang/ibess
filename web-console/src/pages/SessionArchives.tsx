import { useState } from 'react';
import { Alert, Button, Card, Checkbox, Empty, Input, Modal, Select, Space, Table, Tabs, Tag } from 'antd';
import { Link,useSearchParams } from 'react-router-dom';
import { PageHeading,RequestError,SupervisionList } from './Supervision';
import { supervisionRequest } from '../api/supervision';
import { downloadWorkspace,healthLabels,type SessionItem,type SessionReport } from '../api/workspace';
import { useRefreshingQuery } from '../components/workflow/useRefreshingQuery';
import { useAuthStore } from '../store/useAuthStore';
import { formatDateTime } from '../utils/ui';
import { notifyWorkflowChange } from '../store/useWorkflowStore';
import { resolutionLabels,workflowLabels } from '../components/workflow/supervision-model';

export default function SessionArchives(){
  const [params,setParams]=useSearchParams();
  const role=useAuthStore(s=>s.user?.role),canExport=role==='admin'||role==='supervisor';
  const [keyword,setKeyword]=useState(''),[q,setQ]=useState(''),[page,setPage]=useState(1),[busy,setBusy]=useState(''),[error,setError]=useState('');
  const query=useRefreshingQuery<{items:SessionItem[];total:number}>('archive-sessions:'+JSON.stringify([q,page]),signal=>supervisionRequest(`/supervision/sessions?keyword=${encodeURIComponent(q)}&page=${page}`,{signal}));
  const [report,setReport]=useState<SessionReport|null>(null),[reportOpen,setReportOpen]=useState(false),[showAll,setShowAll]=useState(false);
  const [versions,setVersions]=useState<{report_id:string;at:string;actor:string;report_type:string}[]>([]);
  async function view(id:string,session:string){setBusy(id);setError('');try{const [record,history]=await Promise.all([supervisionRequest<SessionReport>('/supervision/reports/'+id),supervisionRequest<typeof versions>(`/supervision/sessions/${session}/reports`)]);setReport(record);setVersions(history);setShowAll(false);setReportOpen(true);}catch(e){setError(e instanceof Error?e.message:'报告读取失败');}finally{setBusy('');}}
  async function generate(item:SessionItem){if(busy)return;setBusy(item.session_id);setError('');try{const record=await supervisionRequest<SessionReport>(`/supervision/sessions/${item.session_id}/reports`,{method:'POST'});setReport(record);setVersions(await supervisionRequest<typeof versions>(`/supervision/sessions/${item.session_id}/reports`));setShowAll(false);setReportOpen(true);query.reload();notifyWorkflowChange();}catch(e){setError(e instanceof Error?e.message:'生成失败');}finally{setBusy('');}}
  const metrics:Record<string,string>={stream_status:'画面连接',recording_status:'录像活动',analysis_status:'分析回执',audio_status:'语音输入观察'};
  const periods=report?.snapshot.coverage.flatMap((c,index)=>c.intervals.filter(i=>showAll||!['online','running','recent','not_applicable'].includes(i.status)).map((i,k)=>({...i,key:index+':'+k,source:c.name+' / '+c.source_id,metric:metrics[c.metric],samples:c.samples})))||[];
  return <div className="sv-page"><PageHeading title="监管档案" description="按场次归集监测范围、覆盖核验、事件和人工办理记录；已生成报告按版本保留。" extra={<Button onClick={query.reload}>刷新</Button>}/>
    <RequestError error={error||query.error} reload={()=>{setError('');query.reload();}}/>
    <Tabs activeKey={params.get('tab')==='events'?'events':'sessions'} onChange={key=>setParams(previous=>{const next=new URLSearchParams(previous);next.set('tab',key);return next;})} items={[{key:'sessions',label:'场次档案',children:<Card><Input.Search aria-label="检索场次档案" placeholder="项目、评标室或场次名称" value={keyword} onChange={e=>setKeyword(e.target.value)} onSearch={value=>{setQ(value.trim());setPage(1);}} allowClear style={{maxWidth:420,marginBottom:18}}/>
      <Table rowKey="session_id" loading={query.loading} dataSource={query.data?.items||[]} scroll={{x:1050}} pagination={{current:page,pageSize:20,total:query.data?.total||0,onChange:setPage,showSizeChanger:false}} columns={[
        {title:'项目 / 场次',render:(_,s:SessionItem)=><div className="sv-event-cell"><strong>{s.project_name}</strong><small>{s.name}</small></div>},
        {title:'评标室',dataIndex:'room_name'},
        {title:'状态',render:(_,s:SessionItem)=><Tag>{({running:'进行中',draft:'未开始',closed:'已结束'} as Record<string,string>)[s.status]||s.status}</Tag>},
        {title:'监测来源',render:(_,s:SessionItem)=><span>{s.channels.filter(c=>c.kind==='video').length} 路画面 / {s.channels.filter(c=>c.kind==='audio').length} 路拾音</span>},
        {title:'最近记录',render:(_,s:SessionItem)=>s.latest_report?formatDateTime(s.latest_report.at):'尚未生成'},
        {title:'操作',render:(_,s:SessionItem)=><Space wrap>{canExport&&<Button size="small" loading={busy===s.session_id} disabled={Boolean(busy)} onClick={()=>void generate(s)}>{s.status==='closed'?'生成场次记录':'生成当前快照'}</Button>}{s.latest_report&&<Button size="small" disabled={Boolean(busy)} onClick={()=>void view(s.latest_report!.report_id,s.session_id)}>查看记录</Button>}<Link to={'/events?session_id='+s.session_id}>场次事件</Link></Space>},
      ]} locale={{emptyText:<Empty description="暂无已绑定监管范围的场次"/>}}/>
    </Card>},{key:'events',label:'已归档事件',children:<SupervisionList mode="archive" embedded/>}]}/>
    <Modal title="场次监管记录" open={reportOpen} width={1100} footer={<Space>{canExport&&report&&<><Button onClick={()=>void downloadWorkspace('/reports/'+report.report_id+'/download','场次监管记录-'+report.report_id+'.html').catch(e=>setError(e.message))}>导出可打印记录</Button><Button onClick={()=>void downloadWorkspace('/reports/'+report.report_id+'/download?format=json','场次监管记录-'+report.report_id+'.json').catch(e=>setError(e.message))}>导出结构化记录</Button></>}<Button onClick={()=>setReportOpen(false)}>关闭</Button></Space>} onCancel={()=>setReportOpen(false)}>
      {error&&<Alert type="error" showIcon message={error}/>}
      {report&&<><Space wrap><Tag color="blue">{report.snapshot.report_type}</Tag><strong>{report.snapshot.project_name} / {report.snapshot.room_name}</strong><span>{report.snapshot.session_name}</span></Space>
        {versions.length>1&&<Select aria-label="历次监管报告" value={report.report_id} style={{width:'100%',marginTop:12}} options={versions.map(v=>({value:v.report_id,label:formatDateTime(v.at)+' · '+v.actor+' · '+v.report_type}))} disabled={Boolean(busy)} onChange={id=>{setBusy(id);void supervisionRequest<SessionReport>('/supervision/reports/'+id).then(setReport).catch(e=>setError(e.message)).finally(()=>setBusy(''));}}/>}
        <p>{formatDateTime(report.snapshot.started_at)} — {formatDateTime(report.snapshot.ended_at)} · 生成账号 {report.actor} · 未办结 {report.snapshot.unresolved} 条</p>
        <Alert type="warning" showIcon message="监测覆盖与未核验区间" description={report.snapshot.notice}/><Checkbox checked={showAll} onChange={e=>setShowAll(e.target.checked)}>同时显示近期正常活动的采样区间</Checkbox>
        <Table size="small" rowKey="key" dataSource={periods} scroll={{x:800}} pagination={{pageSize:8,showSizeChanger:false}} columns={[{title:'来源',dataIndex:'source'},{title:'核验项目',dataIndex:'metric'},{title:'开始',dataIndex:'started_at',render:formatDateTime},{title:'结束',dataIndex:'ended_at',render:formatDateTime},{title:'状态',dataIndex:'status',render:s=><Tag color={s==='unknown'?'gold':'default'}>{healthLabels[s]||s}</Tag>}]} locale={{emptyText:'当前筛选下没有记录；请按监测采样说明解释'}}/>
        <h3>事件与人工结论</h3><Table size="small" rowKey="event_id" dataSource={report.snapshot.events} pagination={{pageSize:6,showSizeChanger:false}} columns={[{title:'线索',dataIndex:'reason'},{title:'流程',dataIndex:'workflow_status',render:s=>workflowLabels[s]||s},{title:'人工结论',dataIndex:'resolution',render:s=>resolutionLabels[s]||'尚未认定'},{title:'事件档案',render:(_,e)=><Link to={'/event-detail?event_id='+e.event_id} onClick={()=>setReportOpen(false)}>查看</Link>}]} locale={{emptyText:'本次快照没有记录到预警事件；不代表现场全程监测正常'}}/>
        <small>报告编号：{report.report_id} · 生成时间：{formatDateTime(report.at)}</small>
      </>}
    </Modal>
  </div>;
}
