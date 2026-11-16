import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { Alert, Button, Card, Checkbox, Collapse, Empty, Form, Input, Modal, Popconfirm, Select, Space, Spin, Table, Tabs, Tag, message } from 'antd'
import { Link } from 'react-router-dom'
import { ReloadOutlined, UserAddOutlined } from '@ant-design/icons'
import { createFact, deleteFact, factTypeLabels, listFacts, type AttendanceFact, type FactType } from '../../api/facts'
import { confirmRoster, fetchRoster, type RoomRoster } from '../../api/rosters'
import { beijingIso } from '../../api/workspace'
import { phaseTitle } from '../../api/stages'
import { useRegistry } from '../../store/useRegistryStore'
import { useRefreshingQuery } from '../workflow/useRefreshingQuery'
import { notifyWorkflowChange } from '../../store/useWorkflowStore'
import { formatDateTime } from '../../utils/ui'
const Registry=lazy(()=>import('../../pages/Registry'))
const FaceMatchModal=lazy(()=>import('../face/FaceMatchModal'))
const roles:Record<string,string>={expert:'评标专家',supervisor:'监督人员',staff:'工作人员',owner:'业主代表',agent:'代理机构',bidder:'投标人'}

interface PersonnelProps {sessionId:string;roomId:string;identityEnabled:boolean;readOnly?:boolean}

function RoomAttendance({sessionId,roomId,identityEnabled,readOnly=false}:PersonnelProps){
  const registry=useRegistry()
  const room=registry.data?.rooms.find(r=>r.room_id===roomId)
  const query=useRefreshingQuery('roster:'+sessionId+':'+roomId+':'+identityEnabled,signal=>fetchRoster(roomId,signal,sessionId),!!room)
  const records=useRefreshingQuery('attendance:'+roomId,()=>listFacts(room!.project_id,{room_id:roomId,limit:200}),!!room)
  const [baseline,setBaseline]=useState(''),[selections,setSelections]=useState<Record<string,string>>({}),[recipients,setRecipients]=useState<string[]>([])
  const [visibility,setVisibility]=useState<'public'|'confidential'>('public'),[policy,setPolicy]=useState('')
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[edit,setEdit]=useState(false),[personId,setPersonId]=useState('')
  const [form]=Form.useForm<{type:FactType;person_id:string;replaced_person_id?:string;seat_id?:string;note?:string;supplement?:boolean;occurred_at?:string}>()
  const type=Form.useWatch('type',form)||'check_in',supplement=Form.useWatch('supplement',form),lock=useRef(false)
  const data=query.data,changed=!!baseline&&baseline!==data?.version
  function accept(value:RoomRoster){setBaseline(value.version);setSelections(Object.fromEntries(value.items.filter(p=>p.verification_result_id).map(p=>[p.person_id,p.verification_result_id!])));setRecipients(value.items.filter(p=>p.recipient_authorized).map(p=>p.person_id));setVisibility(value.information_visibility);setPolicy(value.confidentiality_policy_id||'')}
  useEffect(()=>{setBaseline('');setSelections({});setRecipients([]);setError('');setEdit(false);setPersonId('')},[roomId,sessionId,identityEnabled])
  useEffect(()=>{if(data&&!baseline)accept(data)},[data,baseline])
  const members=registry.data?.persons.filter(p=>p.enabled&&registry.data?.participants.some(m=>m.person_id===p.person_id&&m.project_id===room?.project_id))||[]
  const seats=registry.data?.seats.filter(s=>s.room_id===roomId&&s.enabled)||[]
  function refresh(){query.reload();records.reload();notifyWorkflowChange()}
  async function confirm(){
    if(!data||changed||lock.current||readOnly)return
    lock.current=true;setBusy(true);setError('')
    try{const result=await confirmRoster(roomId,{expected_version:baseline,session_id:sessionId,selections:Object.entries(selections).filter(([,id])=>!!id).map(([person_id,verification_result_id])=>({person_id,verification_result_id})),recipients,information_visibility:visibility,...(visibility==='confidential'?{confidentiality_policy_id:policy.trim()}:{} )});accept(result);refresh();message.success("名单已确认，历史版本保留")}
    catch(e){setError(e instanceof Error?e.message:'名单确认失败')}
    finally{lock.current=false;setBusy(false)}
  }
  async function register(){
    if(!room||lock.current||readOnly)return
    const values=await form.validateFields();lock.current=true;setBusy(true);setError('')
    try{await createFact({fact_type:values.type,session_id:sessionId,project_id:room.project_id,room_id:roomId,person_id:values.person_id,seat_id:values.seat_id,note:values.note?.trim(),...(values.occurred_at?{occurred_at:beijingIso(values.occurred_at)}:{}),payload:{...(values.replaced_person_id?{replaced_person_id:values.replaced_person_id}:{}),...(values.supplement?{supplement:'true'}:{})}});setEdit(false);refresh();message.success(identityEnabled?"变动已登记，请重新确认名单":"变动已登记，可按需保存名单")}
    catch(e){setError(e instanceof Error?e.message:'登记失败')}
    finally{lock.current=false;setBusy(false)}
  }
  const allSelected=data?.items.every(p=>p.enabled&&(!identityEnabled||!!selections[p.person_id]))??false
  return <div className="sv-page roster-workspace">
    <div className="roster-toolbar"><span>{room?.name||"人员资料"}</span><Space><Button icon={<ReloadOutlined/>} onClick={refresh}>刷新</Button>{!readOnly&&<Button icon={<UserAddOutlined/>} disabled={!room||busy} onClick={()=>{form.resetFields();setError('');setEdit(true)}}>登记人员变动</Button>}</Space></div>
    {(error||query.error||records.error||registry.error)&&<Alert type="error" showIcon message={error||query.error||records.error||registry.error} action={<Button onClick={()=>{setError('');refresh()}}>刷新核对</Button>}/>}
    {changed&&<Alert type="warning" showIcon message="人员或核验记录已更新" description={identityEnabled?"请核对最新名单和核验依据后确认。":"请核对最新人员资料后保存。"} action={<Button disabled={busy} onClick={()=>data&&accept(data)}>已核对，继续办理</Button>}/>}
    <Card title="本室到场名单" extra={data&&<Tag color={identityEnabled?(data.needs_confirmation?'gold':'green'):undefined}>{identityEnabled?(data.needs_confirmation?"待确认":'已确认'):'选填资料'}</Tag>}>
      <Table rowKey="person_id" size="small" loading={query.loading} pagination={false} dataSource={data?.items||[]} scroll={{x:identityEnabled?850:400}} columns={[
        {title:'人员 / 岗位',width:180,render:(_,p)=><div className="sv-event-cell"><strong>{p.name}</strong><small>{roles[p.role]||p.role}{!p.enabled?' · 人员或工位已停用':''}</small></div>},
        {title:'工位',dataIndex:'seat_code',width:100,render:value=>value||'未分配'},
        {title:'身份核验依据',hidden:!identityEnabled,width:340,render:(_,p)=>!identityEnabled?<span>{p.verified?"已保留核验依据":'未启用身份核查'}</span>:<div className="sv-event-cell"><Select aria-label={'核验依据 '+p.name} placeholder={p.registered?'选择已通过的核验记录':"请先登记照片，完成核验"} disabled={busy||readOnly||changed||!p.enabled} value={selections[p.person_id]} onChange={value=>setSelections(s=>({...s,[p.person_id]:value}))} options={p.verifications.map(v=>({value:v.result_id,label:formatDateTime(v.checked_at)+' · '+v.source}))}/>{!p.verifications.length&&<small>当前登记照暂无有效核验记录</small>}</div>},
        {title:'操作',hidden:!identityEnabled||readOnly,width:150,render:(_,p)=>identityEnabled&&!readOnly?<Button size="small" disabled={busy||!p.enabled} onClick={()=>setPersonId(p.person_id)}>{p.registered?'现场核验':'登记照片并核验'}</Button>:<span>—</span>},
      ]} locale={{emptyText:<Empty description={room?'尚未登记到场人员':'请先配置项目与评标室'}/>}}/>
      <Collapse ghost className="roster-policy" items={[{key:'policy',label:"资料授权与名单历史",children:<><Space wrap><Select aria-label="本场资料级别" value={visibility} disabled={busy||readOnly||changed} onChange={setVisibility} options={[{value:'public',label:'公开资料'},{value:'confidential',label:'保密资料'}]}/>{visibility==='confidential'&&<Input aria-label="保密依据编号" value={policy} disabled={busy||readOnly||changed} placeholder="填写保密依据编号" maxLength={64} onChange={e=>setPolicy(e.target.value)}/>}</Space><p className="hj-help">选择获准接收保密资料的人员，随名单保存授权。</p><Checkbox.Group value={recipients} disabled={busy||readOnly||changed} options={data?.items.map(p=>({value:p.person_id,label:p.name,disabled:!p.enabled}))} onChange={setRecipients}/><Table size="small" rowKey="at" pagination={false} dataSource={data?.history||[]} columns={[{title:'确认时间',dataIndex:'at',render:formatDateTime},{title:'确认账号',dataIndex:'actor'},{title:'人数',dataIndex:'count'},{title:'状态',render:(_,row)=>row.current?'当前有效':'历史名单'}]}/></>}]}/>
      <div className="roster-confirm"><div>{data?.confirmed_at?<span>最近确认：{formatDateTime(data.confirmed_at)} · {data.confirmed_by}</span>:<span>{identityEnabled?'名单用于人员身份与准入核查。':"人员与工位信息按需填写。"}</span>}<small>{identityEnabled?"启用身份核查后，需选择有效核验依据并确认名单。":"基础行为监管无需确认人员名单。"}</small></div><Popconfirm title={data?.items.length?"确认保存到场名单、核验依据与资料授权？":"确认保存空名单？此操作将替换当前有效名单。"} onConfirm={()=>void confirm()} okText={identityEnabled?'确认名单':'保存资料'} cancelText="继续核对" disabled={!data||!allSelected||changed||busy||readOnly||visibility==='confidential'&&!policy.trim()}><Button type={identityEnabled?'primary':'default'} loading={busy} disabled={!data||!allSelected||changed||readOnly||(!identityEnabled&&!data.items.length&&!data.confirmed_at)||visibility==='confidential'&&!policy.trim()}>{identityEnabled?'确认本场名单':"保存名单"}</Button></Popconfirm></div>
    </Card>
    <p className="hj-help">临时离岗、业务交流和资料递接等许可，在<Link to={sessionId?'/sessions/'+sessionId:'/sessions'}>对应监管场次</Link>登记。</p>
    <Collapse items={[{key:'records',label:'人员变动记录',children:<Table rowKey="fact_id" loading={records.loading} size="small" scroll={{x:800}} dataSource={records.data?.items||[]} pagination={{pageSize:8,showSizeChanger:false}} columns={[{title:'时间',dataIndex:'occurred_at',render:formatDateTime},{title:'事项',dataIndex:'fact_type',render:(value:FactType)=>factTypeLabels[value]},{title:'人员',dataIndex:'person_name'},{title:'工位',dataIndex:'seat_code'},{title:'说明',dataIndex:'note'},{title:'阶段',dataIndex:'phase_id',render:value=>value?phaseTitle(value):'未记录'},{title:'登记账号',dataIndex:'actor'},{title:'操作',render:(_,row:AttendanceFact)=><Popconfirm title="确认撤销这条人员登记？" description="撤销后请重新确认名单，已有归档版本保留。" onConfirm={()=>void deleteFact(row.fact_id,sessionId).then(refresh).catch(e=>setError(e.message))}><Button type="link" size="small" disabled={busy||readOnly}>撤销登记</Button></Popconfirm>}]} />} ]}/>
    <Modal title="登记人员变动" open={edit} confirmLoading={busy} afterOpenChange={open=>{if(open){form.resetFields();form.setFieldsValue({type:'check_in'})}}} onCancel={()=>{if(!busy)setEdit(false)}} onOk={()=>void register().catch(()=>undefined)} okText="保存登记" cancelText="取消">
      {error&&<Alert type="error" message={error}/>}<Form form={form} layout="vertical" disabled={busy||readOnly} preserve={false} initialValues={{type:'check_in'}}>
        <Form.Item name="type" label="办理事项" rules={[{required:true}]}><Select onChange={()=>form.resetFields(['person_id','replaced_person_id','seat_id'])} options={[{value:'check_in',label:'到场签到'},{value:'check_out',label:'离场登记'},{value:'change',label:"人员替换"}]}/></Form.Item>
        <Form.Item name="person_id" label={type==='change'?'递补人员':'人员'} rules={[{required:true}]}><Select showSearch optionFilterProp="label" options={(type==='check_out'?members.filter(p=>data?.items.some(i=>i.person_id===p.person_id)):members).map(p=>({value:p.person_id,label:p.name+' · '+(roles[p.role]||p.role)}))}/></Form.Item>
        {type==='change'&&<Form.Item name="replaced_person_id" label="原到场人员" rules={[{required:true}]}><Select options={data?.items.map(p=>({value:p.person_id,label:p.name}))}/></Form.Item>}
        {type!=='check_out'&&<Form.Item name="seat_id" label="本室工位"><Select allowClear options={seats.map(s=>({value:s.seat_id,label:s.code}))}/></Form.Item>}
        <Form.Item name="note" label="办理说明"><Input.TextArea maxLength={500} rows={2}/></Form.Item>
        <Form.Item name="supplement" valuePropName="checked"><Checkbox>事后补录</Checkbox></Form.Item>
        {supplement&&<Form.Item name="occurred_at" label="实际发生时间（北京时间）" rules={[{required:true}]}><Input type="datetime-local"/></Form.Item>}
      </Form>
    </Modal>
    {personId&&<Suspense fallback={<Spin/>}><FaceMatchModal person={registry.data?.persons.find(p=>p.person_id===personId)||null} onClose={()=>{setPersonId('');query.reload()}}/></Suspense>}
  </div>
}
export default function FactsPanel(props:PersonnelProps){
  const [tab,setTab]=useState('attendance')
  return <Tabs activeKey={props.readOnly?'attendance':tab} onChange={setTab} items={[
    {key:'attendance',label:'本室到场名单',children:<RoomAttendance {...props}/>},
    ...(!props.readOnly?[{key:'people',label:'人员资料',children:<Suspense fallback={<Spin/>}><Registry scope="people" embedded allowFaceVerification={props.identityEnabled}/></Suspense>}]:[]),
  ]}/>
}
