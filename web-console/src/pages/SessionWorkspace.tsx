import { lazy, Suspense, useEffect, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ArrowLeftOutlined, ArrowRightOutlined, CheckCircleFilled, CheckCircleOutlined, ClockCircleOutlined, FileDoneOutlined, FileProtectOutlined, FormOutlined, PlayCircleOutlined, ReloadOutlined, SafetyCertificateOutlined, VideoCameraOutlined } from '@ant-design/icons'
import { Alert, Button, Checkbox, Collapse, Empty, Form, Input, Modal, Select, Space, Spin, Steps, Table, Tabs, Tag, Timeline, message } from 'antd'
import { fetchWorkflow,workflowWrite,lifecycleColors,lifecycleLabels } from '../api/sessionWorkflow'
import { useRefreshingQuery } from '../components/workflow/useRefreshingQuery'
import { useAuthStore } from '../store/useAuthStore'
import { notifyWorkflowChange } from '../store/useWorkflowStore'
import { useRegistry } from '../store/useRegistryStore'
import { formatDateTime } from '../utils/ui'
import { fetchMonitoringHealth } from '../api/supervision'

const SessionPreparation=lazy(()=>import('../components/workflow/SessionPreparation'))
const SessionPersonnel=lazy(()=>import('../components/facts/SessionPersonnel'))
const journalLabels:Record<string,string>={preparation:'准备核验',start:'开始监管',phase:'阶段推进',patrol:'现场巡查',handover:'值守交接',finish:'现场结束',archive:'归档确认',supplement:'补充记录',permit:'临时许可'}
const controlLabels:Record<string,string>={pending:"方案已保存，等待应用",partially_applied:"部分服务已应用",missing_asset:"应用受阻：缺少素材",failed:"方案应用失败",applied:"方案已应用"}
const permitLabels:Record<string,string>={contact_allowed:'允许业务交流',document_access:'允许资料递接',photo_allowed:'允许业务拍摄',device_allowed:'允许应急通信',break:'批准临时休息'}
type Operation = 'start'|'phase'|'patrol'|'handover'|'finish'|'archive'|'permit'|'supplement'
export default function SessionWorkspace() {
  const {sessionId=''}=useParams();const [params]=useSearchParams();const navigate=useNavigate();const role=useAuthStore(s=>s.user?.role)
  const canOperate=role==='admin'||role==='supervisor'
  const query=useRefreshingQuery('session:'+sessionId,signal=>fetchWorkflow(sessionId,signal),Boolean(sessionId))
  const health=useRefreshingQuery('session-health:'+sessionId,signal=>fetchMonitoringHealth(signal))
  const registry=useRegistry()
  const [operation,setOperation]=useState<Operation|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('')
  const [form]=Form.useForm<{note:string;evidence_gap_note?:string;authorization_kind:string;minutes:number;seat_id?:string}>()
  const s=query.data;const current=s?.phases.findIndex(p=>p.id===s.phase_id)??0
  useEffect(()=>{const timer=window.setInterval(()=>{query.reload();health.reload()},15000);return()=>window.clearInterval(timer)},[query.reload,health.reload])
  async function update(action:string,body:Record<string,unknown>,method='POST') {
    if(!s||busy)return
    setBusy(true);setError('')
    try {await workflowWrite(s.session_id,action,{expected_version:s.version,...body},method);query.reload();notifyWorkflowChange();return true}
    catch(e){setError(e instanceof Error?e.message:'操作失败，请重试');return false}
    finally{setBusy(false)}
  }
  function open(kind:Operation) {form.resetFields();form.setFieldsValue({minutes:30,authorization_kind:'break',note:kind==='phase'?`现场进入${s?.phases[current+1]?.title||'下一阶段'}`:''});setOperation(kind)}
  async function submit() {
    if(!s||!operation)return
    const values=await form.validateFields();let action:string=operation
    const body:Record<string,unknown>={note:values.note}
    if(operation==='phase')body.phase=s.phases[current+1]?.id
    if(['patrol','handover','supplement'].includes(operation)){action='journal';body.kind=operation}
    if(operation==='archive')body.evidence_gap_note=values.evidence_gap_note
    if(operation==='permit')Object.assign(body,{authorization_kind:values.authorization_kind,valid_until:new Date(Date.now()+values.minutes*60000).toISOString(),seat_id:values.seat_id||null})
    const ok=await update(action,body)
    if(ok){setOperation(null);message.success(operation==='phase'||operation==='permit'?"已提交，请查看方案应用状态":'记录已保存')}
  }
  const runtime=health.data?.items.filter(item=>item.session_id===sessionId)||[]
  const closing=s?.status==='closed'
  const checks=(closing?s.archive_checks:s?.readiness.items||[]).filter(c=>c.required!==false&&(closing||c.manual))
  return <div className="sv-page hj-session-workspace"><Link className="hj-back" to="/sessions"><ArrowLeftOutlined/> 返回场次管理</Link>
    {query.loading&&!s?<div className="sv-loading"><Spin/></div>:!s?<Alert type="error" message={query.error||'场次不存在'}/>:<>
      <div className="hj-session-heading"><div><div className="hj-heading-meta"><span>{s.project_name}</span><Tag color={lifecycleColors[s.lifecycle]}>{lifecycleLabels[s.lifecycle]}</Tag></div><h1>{s.name}</h1><p>{s.room_name}<span>·</span>{s.started_at?`开始于 ${formatDateTime(s.started_at)}`:"完成评前准备后开始监管"}{s.started_by&&<><span>·</span>启动人 {s.started_by}</>}</p></div><Space wrap><Button icon={<ReloadOutlined/>} onClick={query.reload}>刷新</Button>{s.status==='running'&&<Link to={'/monitor?session_id='+s.session_id}><Button type="primary" icon={<VideoCameraOutlined/>}>进入实时监控</Button></Link>}{s.status==='draft'&&s.managed&&canOperate&&<Button type="primary" icon={<PlayCircleOutlined/>} disabled={!s.readiness.ready||busy} onClick={()=>void update('start',{})}>开始监管</Button>}</Space></div>
      {(error||query.error)&&<Alert type="error" showIcon message={error||query.error} action={<Button onClick={()=>{setError('');query.reload()}}>刷新核对</Button>}/>}
      {!s.managed&&<Alert type="info" showIcon message="历史场次尚未纳入监管流程" description="纳入后可进行场次办理，原有事件与证据保留。" action={role==='admin'?<Button loading={busy} onClick={()=>void update('adopt',{})}>纳入监管流程</Button>:undefined}/>}
      {s.lifecycle==='supplement_required'&&<Alert type="warning" showIcon message="归档内容有更新" description="请核对新增告警、证据或记录后补充归档，原版本保留。"/>}
      <section className="hj-panel hj-phase-panel"><div className="hj-panel-heading"><div><span className="hj-eyebrow">当前评标阶段</span><h2>{s.phases[current]?.title||s.phase_id}</h2></div><Space><Tag color={s.control?.sync_status==='applied'?'green':['failed','missing_asset'].includes(s.control?.sync_status||'')?'red':'gold'}>{s.control?controlLabels[s.control.sync_status]||'阶段应用待核验':s.phase_version?"方案已保存，应用状态待核验":'初始阶段待配置'}</Tag>{s.status==='running'&&canOperate&&current>=0&&current<s.phases.length-1&&<Button size="small" onClick={()=>open('phase')}>推进下一阶段 <ArrowRightOutlined/></Button>}</Space></div><Collapse ghost items={[{key:'phases',label:"查看阶段流程",children:<Steps current={Math.max(current,0)} size="small" items={s.phases.map(p=>({title:p.title}))}/>}]} /></section>
      <div className="hj-session-workspace-grid"><section className="hj-panel"><Tabs key={s.session_id+':'+s.status} defaultActiveKey={params.get('prepare')||s.status==='draft'||s.status==='closed'?'prepare':'overview'} items={[
        {key:'overview',label:'监测现场',children:<><div className="hj-inline-stats"><div><span>监测来源</span><strong>{s.channels.filter(c=>c.active).length}<small> 路</small></strong></div><div><span>累计告警</span><strong>{s.total_events}<small> 条</small></strong></div><div><span>未办结事件</span><strong className={s.pending_events?'hj-attention':''}>{s.pending_events}<small> 条</small></strong></div></div>
          <div className="hj-section-caption"><h3>监测范围</h3><Link to={'/events?session_id='+s.session_id}>查看本场事件 <ArrowRightOutlined/></Link></div>
          <Table rowKey="source_id" size="small" pagination={false} dataSource={s.channels.filter(c=>c.active)} columns={[{title:'监测来源',render:(_,c)=><span><VideoCameraOutlined/> {c.name}<small className="hj-source-id">{c.source_id}</small></span>},{title:'类型',render:(_,c)=>c.kind==='audio'?'拾音通道':'视频机位'},{title:'运行状态',render:(_,c)=>{const observed=runtime.find(r=>r.source_id===c.source_id);return <Tag color={observed?.analysis_status==='running'?'green':'gold'}>{observed?.analysis_status==='running'?"近期有分析结果":s.status==='draft'?'开始后核验':'等待核验'}</Tag>}}]} locale={{emptyText:<Empty description="尚未绑定监测来源"/>}}/>
          <p className="hj-help">{s.runtime_notice}</p><div className="hj-work-actions">{s.status==='running'&&canOperate&&<><Button icon={<FormOutlined/>} onClick={()=>open('patrol')}>记录巡查</Button><Button onClick={()=>open('handover')}>值守交接</Button><Button icon={<SafetyCertificateOutlined/>} onClick={()=>open('permit')}>临时许可</Button></>}{closing&&canOperate&&<Button onClick={()=>open('supplement')}>补充场次记录</Button>}</div></>},
        {key:'prepare',label:closing?'收尾核验':'评前准备',children:<>{!closing&&<Suspense fallback={<Spin/>}><SessionPreparation session={s} onSaved={query.reload}/></Suspense>}<p className="hj-help">{closing?"核对人员、资料和未办结事件后归档。":s.status==='draft'?"完成现场检查后开始监管，人员资料按需填写。":"查看本场准备记录，核对人员资料与身份依据。"}</p><div className="hj-check-list">{checks.map(c=><div className={'hj-check-row '+(c.passed?'is-done':'')} key={c.key}>{c.manual?<Checkbox checked={c.passed} disabled={!canOperate||busy||!s.managed||(closing?s.status!=='closed':s.status!=='draft')} onChange={e=>void update('checks',{checks:{[c.key]:e.target.checked}},'PUT')}>{c.label}</Checkbox>:<span>{c.passed?<CheckCircleFilled/>:<ClockCircleOutlined/>}{c.label}</span>}<Space>{s.checks[c.key]?.actor&&<small>{s.checks[c.key].actor}</small>}{c.wait_seconds? <Tag>约 {c.wait_seconds} 秒后复核</Tag>:<Tag color={c.passed?'success':'default'}>{c.passed?'已完成':'待完成'}</Tag>}{!c.passed&&c.action&&role==='admin'&&<Link to={c.action+(c.action.includes('?')?'&':'?')+'session_id='+s.session_id}>去准备</Link>}</Space></div>)}</div>{!closing&&!s.readiness.ready&&<div className="hj-subtle-notice">完成必要准备项后，即可开始现场监管。</div>}{role==='admin'&&s.managed&&<Collapse ghost defaultActiveKey={params.get('prepare')==='people'?['personnel']:[]} className="session-personnel-collapse" items={[{key:'personnel',label:s.identity_verification_enabled?'本场人员名单与身份核查':'人员资料（选填）',children:<Suspense fallback={<Spin/>}><SessionPersonnel key={s.session_id} session={s} onSaved={query.reload}/></Suspense>}]}/>}</>},
        {key:'records',label:`值守记录（${s.journal.length}）`,children:<><div className="hj-section-caption"><span>查阅巡查、交接和场次操作记录。</span>{s.status==='running'&&canOperate&&<Button size="small" onClick={()=>open('patrol')}>记录巡查</Button>}</div><Timeline className="hj-journal" items={s.journal.map(j=>({color:j.kind==='archive'?'green':'blue',children:<div><div className="hj-journal-meta"><b>{journalLabels[j.kind]||j.kind}</b><span>{formatDateTime(j.at)} · {j.actor}</span></div><p>{j.note}</p>{j.kind==='permit'&&<Tag>{permitLabels[String(j.detail.authorization_kind)]} · 至 {formatDateTime(String(j.detail.valid_until))}</Tag>}</div>}))}/>{!s.journal.length&&<Empty description="暂无操作记录"/>}</>},
      ]}/></section>
      <aside className="hj-workspace-aside"><section className="hj-panel"><div className="hj-panel-heading"><h2>本场待办</h2><FileDoneOutlined/></div><Link className="hj-task-link" to={'/events?session_id='+s.session_id+'&queue=active'}><span>未办结事件</span><b>{s.pending_events}</b><ArrowRightOutlined/></Link><Link className="hj-task-link" to={'/events?session_id='+s.session_id}><span>本场告警</span><b>{s.total_events}</b><ArrowRightOutlined/></Link><div className="hj-stage-note"><CheckCircleOutlined/> 告警经人工核查后形成结论，办理记录随场次归档。</div></section>
        <section className="hj-panel"><div className="hj-panel-heading"><h2>收尾与归档</h2><FileProtectOutlined/></div><p className="hj-help">现场结束后可继续处置告警，办结后确认归档。</p>{s.status==='running'&&canOperate&&<Button block onClick={()=>open('finish')}>结束监管</Button>}{closing&&<><div className="hj-close-summary"><span>未办结</span><b>{s.pending_events} 条</b><span>资料待核验</span><b>{s.media_pending} 条</b></div>{canOperate&&<Button block type="primary" disabled={!s.managed||s.lifecycle==='archived'||s.archive_checks.some(c=>!c.passed)} onClick={()=>open('archive')}>{s.lifecycle==='archived'?"已归档":s.archived_report_id?"补充归档":"确认归档"}</Button>}</>}{s.archived_report_id&&<Button block style={{marginTop:12}} onClick={()=>navigate('/archives?report_id='+s.archived_report_id+'&session_id='+s.session_id)}>查看归档记录</Button>}{s.status==='draft'&&<div className="hj-subtle-notice">结束监管后可办理收尾归档。</div>}</section>
        {role==='admin'&&<Collapse ghost items={[{key:'config',label:'管理与配置',children:<Space direction="vertical"><Link to={'/admin/sessions?room_id='+s.room_id}>现场资源配置</Link><Link to={'/admin/maintenance?session_id='+s.session_id}>运维设置</Link><Link to="/admin/rules">识别规则</Link></Space>}]}/>}
      </aside></div>
      <Modal title={operation?({start:'开始监管',phase:'推进评标阶段',patrol:"记录巡查",handover:"记录交接",finish:"结束监管",archive:"确认归档",permit:"登记临时许可",supplement:'补充场次记录'}[operation]):''} open={!!operation} onCancel={()=>{if(!busy)setOperation(null)}} onOk={()=>void submit().catch(()=>undefined)} confirmLoading={busy} okText={operation==='archive'?'确认归档':operation==='finish'?'结束监管':operation==='phase'?'确认推进':operation==='permit'?'登记许可':'保存记录'} cancelText="取消" destroyOnHidden>
        {error&&<Alert type="error" showIcon message={error}/>}
        {operation==='phase'&&<p>将从「{s.phases[current]?.title}」推进到「{s.phases[current+1]?.title}」，使用本场准备时保存的监测方案。</p>}
        {operation==='finish'&&<Alert type="info" message="结束监管后进入收尾" description="未办结告警可继续处置，资料收集完成后再归档。" showIcon/>}
        <Form form={form} layout="vertical" preserve={false} style={{marginTop:18}}>
          {operation==='permit'&&<><Form.Item name="authorization_kind" label="许可事项" rules={[{required:true}]}><Select options={Object.entries(permitLabels).map(([value,label])=>({value,label}))}/></Form.Item><Form.Item name="seat_id" label="许可范围"><Select allowClear placeholder="本评标室全部工位" options={registry.data?.seats.filter(seat=>seat.room_id===s.room_id).map(seat=>({value:seat.seat_id,label:seat.code}))}/></Form.Item><Form.Item name="minutes" label="有效期" rules={[{required:true}]}><Select options={[15,30,60].map(n=>({value:n,label:`从登记起 ${n} 分钟`}))}/></Form.Item></>}
          <Form.Item name="note" label={operation==='handover'?"交接事项":operation==='archive'?'归档确认说明':'办理说明'} rules={[{required:true,whitespace:true,min:2,message:"请填写至少 2 个字的办理说明"}]}><Input.TextArea rows={4} maxLength={500} showCount placeholder={operation==='patrol'?"填写现场秩序、设备状态和发现情况":"填写办理内容、依据及后续事项"}/></Form.Item>
          {operation==='archive'&&<Form.Item name="evidence_gap_note" label="资料缺失与覆盖说明" extra="存在未核验区间或资料缺失时必填，原资料状态保留。"><Input.TextArea rows={3} maxLength={4000}/></Form.Item>}
        </Form>
      </Modal>
    </>}
  </div>
}
