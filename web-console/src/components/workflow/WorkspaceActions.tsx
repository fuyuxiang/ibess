import { lazy, Suspense, useRef, useState } from 'react';
import { Alert, Button, Drawer, Spin, Form, Input, Modal, Select, Space, Tag } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { fetchSources, supervisionRequest, type MonitoringSource } from '../../api/supervision';
import type { EventDetail } from '../../api/client';
import { beijingInput,beijingIso } from '../../api/workspace';
import { useRefreshingQuery } from './useRefreshingQuery';
import { newRequestId } from './workflow-model';
import { notifyWorkflowChange } from '../../store/useWorkflowStore';
import { useAuthStore } from '../../store/useAuthStore';
import { formatDateTime } from '../../utils/ui';

const AudioTools=lazy(()=>import('../audio/AudioTools'));

function token(ref:{current:{signature:string;id:string}|null},payload:unknown){const signature=JSON.stringify(payload);if(ref.current?.signature!==signature)ref.current={signature,id:newRequestId()};return ref.current.id;}

export function ManualMarkButton({source}:{source?:MonitoringSource}){
  const role=useAuthStore(s=>s.user?.role);
  const [open,setOpen]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const request=useRef<{signature:string;id:string}|null>(null),lock=useRef(false);
  const query=useRefreshingQuery('manual-sources',signal=>fetchSources(signal),open);
  const [form]=Form.useForm();const navigate=useNavigate();
  if(role==='viewer')return null;
  function show(){setError('');form.resetFields();form.setFieldsValue({binding_id:source?.binding_id,started_at:beijingInput(new Date().toISOString()),ended_at:beijingInput(new Date().toISOString()),event_type:'manual_observation',risk_level:'medium'});setOpen(true);}
  async function save(){if(lock.current)return;try{const values=await form.validateFields();const selected=query.data?.items.find(s=>s.binding_id===values.binding_id)||source;if(!selected)throw Error("请选择监测通道");
    const payload={session_id:selected.session_id,source_id:selected.source_id,started_at:beijingIso(values.started_at),ended_at:beijingIso(values.ended_at),reason:values.reason.trim(),event_type:values.event_type,risk_level:values.risk_level};
    lock.current=true;setBusy(true);setError('');const result=await supervisionRequest<{event_id:string}>('/supervision/events/manual',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,request_id:token(request,payload)})});
    setOpen(false);notifyWorkflowChange();navigate('/event-detail?event_id='+result.event_id);
  }catch(e){if(e instanceof Error)setError(e.message);}finally{lock.current=false;setBusy(false);}}
  return <><Button icon={<PlusOutlined/>} onClick={show}>人工标记</Button><Modal title="人工标记" open={open} confirmLoading={busy} onOk={()=>void save()} okText="保存并进入核查" cancelText="取消" onCancel={()=>{if(!busy)setOpen(false);}} maskClosable={false}>
    <Alert type="info" showIcon message="记录现场观察，提交后进入核查" description="请填写实际观察到的行为、位置和时间。"/>
    {(error||query.error)&&<Alert type="error" showIcon message={error||query.error}/>}
    <Form name="manual-mark" form={form} layout="vertical" disabled={busy}>
      <Form.Item name="binding_id" label="场次与监测来源" rules={[{required:true,message:'请选择来源'}]}><Select showSearch optionFilterProp="label" options={(query.data?.items||(source?[source]:[])).map(s=>({value:s.binding_id,label:`${s.project_name} / ${s.room_name} / ${s.session_name} / ${s.source_name||s.source_id}`}))}/></Form.Item>
      <Space wrap><Form.Item name="started_at" label="观察开始（北京时间）" rules={[{required:true}]}><Input type="datetime-local" step={1}/></Form.Item><Form.Item name="ended_at" label="观察结束（北京时间）" rules={[{required:true}]}><Input type="datetime-local" step={1}/></Form.Item></Space>
      <Form.Item name="event_type" label="线索类型" rules={[{required:true}]}><Select options={[['manual_observation','其他现场观察'],['phone_call','疑似使用通讯设备'],['seat_absence','工位空置'],['zone_intrusion','区域准入待核验'],['phone_capture','疑似拍摄'],['language_risk','交流内容待核查']].map(([value,label])=>({value,label}))}/></Form.Item>
      <Form.Item name="risk_level" label="核查优先级" rules={[{required:true}]}><Select options={[{value:'high',label:'优先核查'},{value:'medium',label:'常规核查'},{value:'low',label:'一般记录'}]}/></Form.Item>
      <Form.Item name="reason" label="观察事实与依据" rules={[{required:true,whitespace:true,message:'请填写观察到的具体动作、时间或原话'}]}><Input.TextArea rows={4} maxLength={4000} showCount placeholder="填写动作、位置或原话；身份不明时标注“未知”。"/></Form.Item>
    </Form>
  </Modal></>;
}

export function EvidenceSupplement({detail,reload}:{detail:EventDetail;reload:()=>void}){
  const role=useAuthStore(s=>s.user?.role);const context=detail.context_evidence;
  const [open,setOpen]=useState(false),[audioOpen,setAudioOpen]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const [version,setVersion]=useState(0);const [form]=Form.useForm();const kind=Form.useWatch('kind',form)||'video';
  const request=useRef<{signature:string;id:string}|null>(null),lock=useRef(false);
  if(!context||role==='viewer'||detail.workflow_status==='ARCHIVED')return null;
  function show(){form.resetFields();setVersion(detail.workflow_version||0);setError('');const first=context!.videos[0];form.setFieldsValue({kind:'video',source_id:first?.source_id||context!.video_candidates[0]?.source_id,
    started_at:beijingInput(first?.started_at||(context!.range.started_at?new Date(Date.parse(context!.range.started_at)-45000).toISOString():null)),
    ended_at:beijingInput(first?.ended_at||(context!.range.ended_at?new Date(Date.parse(context!.range.ended_at)+45000).toISOString():null))});setOpen(true);}
  async function save(){if(lock.current)return;try{const values=await form.validateFields();const payload={expected_version:version,note:values.note.trim(),...(values.kind==='video'?{source_id:values.source_id,started_at:beijingIso(values.started_at),ended_at:beijingIso(values.ended_at)}:values.kind==='audio'?{recording_id:values.recording_id}:{kind:values.reference.split('|')[0],reference:values.reference.split('|')[1]})};
    lock.current=true;setBusy(true);setError('');await supervisionRequest(`/supervision/events/${detail.event_id}/${values.kind==='video'?'video-window':values.kind==='audio'?'audio-links':'remove-link'}`,{method:values.kind==='video'?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,request_id:token(request,payload)})});
    setOpen(false);notifyWorkflowChange();reload();
  }catch(e){if(e instanceof Error)setError(e.message);}finally{lock.current=false;setBusy(false);}}
  return <><Button size="small" onClick={show}>补充证据</Button><Modal title="补充证据" open={open} confirmLoading={busy} okText={kind==='remove'?'撤销关联':'保存关联'} cancelText="取消" maskClosable={false} onOk={()=>void save()} onCancel={()=>{if(!busy)setOpen(false);}}>
    <Alert type="info" showIcon message="选择同场次、同评标室且时间重叠的证据" description="扩展时段将重新检查录像保全，原件保留。时间关联不能作为身份依据。"/>
    {error&&<Alert type="error" showIcon message={error}/>}
    <Form name="evidence-link" form={form} layout="vertical" disabled={busy}>
      <Form.Item name="kind" label="补充方式"><Select options={[{value:'video',label:"关联机位或扩展录像时段"},{value:'audio',label:"关联现场录音"},{value:'remove',label:"撤销补充关联"}]}/></Form.Item>
      {kind==='video'?<><Form.Item name="source_id" label="视频来源" rules={[{required:true,message:'请选择本场次机位'}]}><Select options={[...context.videos.map(v=>({value:v.source_id,label:v.source_id+'（已关联，可扩展）'})),...context.video_candidates.map(v=>({value:v.source_id,label:v.name+' / '+v.source_id}))]}/></Form.Item>
        <Space wrap><Form.Item name="started_at" label="保全开始（北京时间）" rules={[{required:true}]}><Input type="datetime-local" step={1}/></Form.Item><Form.Item name="ended_at" label="保全结束（北京时间）" rules={[{required:true}]}><Input type="datetime-local" step={1}/></Form.Item></Space>
        <Button size="small" onClick={()=>{const a=form.getFieldValue('started_at'),b=form.getFieldValue('ended_at');if(a&&b)form.setFieldsValue({started_at:beijingInput(new Date(Date.parse(beijingIso(a))-60000).toISOString()),ended_at:beijingInput(new Date(Date.parse(beijingIso(b))+60000).toISOString())});}}>前后各扩展1分钟</Button></>:kind==='audio'?<Form.Item name="recording_id" label="时间重叠的录音" rules={[{required:true,message:'请选择录音；缺少采集时间的录音需先登记'}]} extra={<Button type="link" size="small" onClick={()=>setAudioOpen(true)}>上传补充录音</Button>}><Select options={context.audio_candidates.map(a=>({value:a.recording_id,label:a.title+' / '+formatDateTime(a.capture_started_at)}))}/></Form.Item>:<Form.Item name="reference" label="要撤销的人工补充关联" rules={[{required:true,message:'请选择人工补充关联'}]} extra="仅可撤销人工补充的关联，原始文件保留，需填写撤销原因。"><Select options={[...context.videos.filter(v=>v.actor!=='system').map(v=>({value:'video|'+v.source_id,label:'视频 '+v.source_id})),...context.audio.filter(a=>a.actor!=='system').map(a=>({value:'audio|'+a.recording_id,label:"原始录音"+a.title}))]}/></Form.Item>}
      <Form.Item name="note" label="说明与依据" rules={[{required:true,whitespace:true,message:"请填写关联或撤销依据"}]}><Input.TextArea rows={3} maxLength={2000} showCount/></Form.Item>
    </Form>
  </Modal><Drawer title="补充录音" open={audioOpen} onClose={()=>{setAudioOpen(false);reload()}} width={1100} destroyOnHidden><Suspense fallback={<Spin/>}><AudioTools roomId={detail.room_id||undefined}/></Suspense></Drawer></>;
}

export function BusinessBasis({detail}:{detail:EventDetail}){
  const context=detail.context_evidence;if(!context)return null;
  const labels:Record<string,string>={check_in:'到场签到',check_out:'离场登记',change:'席位/人员变更',leave_permit:'离岗许可',clarification:'澄清记录'};
  return <details className="sv-context-details"><summary>事发时业务依据（{context.facts.length}）</summary>
    {context.authorization?<p>事发前最近授权快照：{formatDateTime(context.authorization.at)} · {context.authorization.visibility==='confidential'?'涉密资料':'公开资料'} · 已核验身份 {context.authorization.entries.filter(e=>e.identity_verified).length} 人</p>:<Alert type="warning" message="未找到事发前授权快照" description="暂无登记依据，请结合现场记录核实许可情况。"/>}
    {context.facts.length?context.facts.map(f=><div className="sv-basis-row" key={f.fact_id}><Space wrap><Tag>{labels[f.type]||f.type}</Tag><span>{formatDateTime(f.occurred_at)}</span>{f.registered_after_event&&<Tag color="gold">事后登记</Tag>}</Space><p>{f.person_name||f.seat_code||'场次记录'} · {f.note||'未填写说明'}</p><small>登记时间：{formatDateTime(f.registered_at)} · 事实编号：{f.fact_id}</small></div>):<p>此时段暂无业务记录，请补充核查依据。</p>}
  </details>;
}

export function TaskReceipt({detail,reload}:{detail:EventDetail;reload:()=>void}){
  const user=useAuthStore(s=>s.user);const [open,setOpen]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('');const [form]=Form.useForm();
  const [version,setVersion]=useState(0);const lock=useRef(false),request=useRef<{signature:string;id:string}|null>(null);
  const receipt=detail.task_receipt;const canReceive=detail.workflow_status==='ASSIGNED'&&!receipt?.received_at&&user?.role!=='viewer'&&(user?.role!=='operator'||detail.assignee===user.username);
  async function save(){if(lock.current)return;try{const values=await form.validateFields();const payload={expected_version:version,note:values.note.trim()};lock.current=true;setBusy(true);setError('');await supervisionRequest(`/supervision/events/${detail.event_id}/receive`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,request_id:token(request,payload)})});setOpen(false);notifyWorkflowChange();reload();}catch(e){if(e instanceof Error)setError(e.message);}finally{lock.current=false;setBusy(false);}}
  if(!detail.assignee)return null;
  return <div className="sv-task-receipt">{receipt?.received_at?<p><Tag color="blue">已接收</Tag>{receipt.received_by} · {formatDateTime(receipt.received_at)}</p>:<Alert type="warning" showIcon message="待接收" description="分派后请通知责任人接收；系统不自动发送短信或广播。"/>}
    {canReceive&&<Button onClick={()=>{setVersion(detail.workflow_version||0);form.resetFields();setError('');setOpen(true);}}>接收任务</Button>}
    <Modal title="接收任务" open={open} confirmLoading={busy} okText="确认接收" cancelText="取消" maskClosable={false} onOk={()=>void save()} onCancel={()=>{if(!busy)setOpen(false);}}>
      <p>本次由当前账号接收 {user?.display_name}（{user?.username}），接收记录将留存。</p>{error&&<Alert type="error" message={error}/>}
      <Form name="task-receipt" form={form} layout="vertical" disabled={busy}><Form.Item name="note" label="接收说明" rules={[{required:true,whitespace:true}]}><Input.TextArea rows={3} maxLength={2000}/></Form.Item></Form>
    </Modal>
  </div>;
}
