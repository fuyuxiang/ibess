import { useEffect, useState } from 'react'
import { Alert, Button, Collapse, Select, Space, Table, Tag } from 'antd'
import { Link } from 'react-router-dom'
import { useAuthStore } from '../../store/useAuthStore'
import { fetchRoomPresets, schemeLabels } from '../../api/roomMonitoring'
import { workflowWrite, type SessionWorkflow } from '../../api/sessionWorkflow'
import { useRefreshingQuery } from './useRefreshingQuery'
import { notifyWorkflowChange } from '../../store/useWorkflowStore'

export default function SessionPreparation({session:s,onSaved}:{session:SessionWorkflow;onSaved:()=>void}){
  const admin=useAuthStore(state=>state.user?.role)==='admin'
  const presets=useRefreshingQuery('prepare-resources:'+s.room_id,signal=>fetchRoomPresets(s.room_id!,signal),admin&&!!s.room_id)
  const [scheme,setScheme]=useState(s.monitoring_scheme==='standard'?'standard':'basic')
  const [busy,setBusy]=useState(false),[error,setError]=useState('')
  useEffect(()=>setScheme(s.monitoring_scheme==='standard'?'standard':'basic'),[s.session_id,s.version,s.monitoring_scheme])
  async function save(presetId?:string){
    if(busy)return;setBusy(true);setError('')
    try{await workflowWrite(s.session_id,'preparation',{expected_version:s.version,scheme,...(presetId?{preset_id:presetId}:{})},'PUT');onSaved();notifyWorkflowChange()}
    catch(cause){setError(cause instanceof Error?cause.message:'准备配置未保存，请刷新核对')}finally{setBusy(false)}
  }
  const missing=s.readiness.items.filter(c=>!c.manual&&c.required!==false&&!c.passed)
  const optional=s.readiness.items.filter(c=>['seat_zones','entry_lines'].includes(c.key))
  const latest=presets.data?.items[0]
  return <div className="session-preparation">
    {(error||presets.error)&&<Alert type="error" showIcon message={error||presets.error}/>}
    <div className="hj-section-caption"><h3>本场监控范围</h3><Tag>{s.preparation_snapshot?.version?`现场资源 v${s.preparation_snapshot.version}`:'本场独立配置'}</Tag></div>
    <Table rowKey="source_id" size="small" pagination={false} dataSource={s.channels.filter(c=>c.active)} columns={[
      {title:'监测通道',render:(_,c)=>s.preparation_snapshot?.channels?.find(p=>p.source_id===c.source_id)?.name||c.name},
      {title:'类型',render:(_,c)=>c.kind==='audio'?'拾音通道':'视频画面'},
    ]}/>
    <Space wrap style={{marginTop:4}}><strong>监测方案</strong><Tag color="blue">{s.monitoring_scheme_label}</Tag><span className="hj-help">按评标阶段应用相应规则。</span></Space>
    {s.monitoring_scheme==='basic'&&<p className="hj-help">监测通话、拍摄、近距交流与资料递接。工位、区域及出入核查需另行配置。</p>}
    <p className="hj-help">{optional.map(c=>`${c.label}：${c.passed?'已配置':c.required?'待配置':"选填，未配置"}`).join('；')}。</p>
    {missing.length>0&&<Alert type="warning" showIcon message="以下配置未完成" description={missing.map(c=>c.label).join('、')}/>}
    {admin&&s.status==='draft'&&<Collapse ghost items={[{key:'adjust',label:"调整监测方案",children:<Space wrap>
      <Select aria-label="本场监测方案" value={scheme} onChange={setScheme} disabled={busy} options={Object.entries(schemeLabels).map(([value,label])=>({value,label}))}/>
      <Button loading={busy} type="primary" onClick={()=>void save()}>保存并应用方案</Button>
      {latest&&latest.preset_id!==s.preparation_snapshot?.preset_id&&<Button disabled={busy} onClick={()=>void save(latest.preset_id)}>载入最新资源版本</Button>}
      <Link to={'/admin/sessions?room_id='+s.room_id}>配置现场资源</Link>
    </Space>}]}/>}
    <Collapse ghost items={[{key:'checks',label:"查看准备检查",children:<div className="hj-check-list">{s.readiness.items.filter(c=>!c.manual).map(c=><div key={c.key} className="hj-check-row"><span>{c.label}</span><Tag color={c.passed?'success':c.required===false?'default':'warning'}>{c.passed?'已通过':c.required===false?"选填，未配置":"待完成"}</Tag></div>)}</div>}]}/>
  </div>
}
