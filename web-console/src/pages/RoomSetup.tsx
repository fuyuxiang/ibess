import { useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Collapse, Empty, Form, Input, Modal, Select, Space, Spin, Table, Tag, message } from 'antd'
import { useSearchParams } from 'react-router-dom'
import { PlusOutlined, UploadOutlined } from '@ant-design/icons'
import { useRegistry } from '../store/useRegistryStore'
import { useRefreshingQuery } from '../components/workflow/useRefreshingQuery'
import { fetchRoomPresets, saveRoomPreset, schemeLabels, type RoomChannel, type RoomPreset } from '../api/roomMonitoring'
import ZoneEditor from '../components/stages/ZoneEditor'
import type { ZoneConfig } from '../api/zones'
import { formatDateTime } from '../utils/ui'

export default function RoomSetup(){
  const registry=useRegistry(),[params,setParams]=useSearchParams()
  const roomId=params.get('room_id')||registry.data?.rooms.find(r=>r.enabled)?.room_id||''
  const room=registry.data?.rooms.find(r=>r.room_id===roomId)
  const query=useRefreshingQuery('room-resources:'+roomId,signal=>fetchRoomPresets(roomId,signal),!!room)
  const [channels,setChannels]=useState<RoomChannel[]>([]),[primary,setPrimary]=useState(''),[source,setSource]=useState('')
  const [scheme,setScheme]=useState<'basic'|'standard'>('basic'),[note,setNote]=useState(''),[version,setVersion]=useState(0)
  const [selected,setSelected]=useState<string>(),[dirty,setDirty]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('')
  const [geometryOpen,setGeometryOpen]=useState(false)
  const [open,setOpen]=useState(false),[snapshot,setSnapshot]=useState<string|null>(null),[channelForm]=Form.useForm()
  const loaded=useRef(''),upload=useRef<HTMLInputElement>(null)
  function load(p?:RoomPreset){
    setChannels(p?.channels||[]);setPrimary(p?.primary_source_id||'');setSource(p?.primary_source_id||'')
    setScheme(p?.scheme||'basic');setNote('');setSelected(p?.preset_id);setDirty(false);setError('');setSnapshot(null);setGeometryOpen(false)
  }
  useEffect(()=>{if(query.data&&loaded.current!==roomId){loaded.current=roomId;load(query.data.items[0]);setVersion(query.data.items[0]?.version||0)}},[query.data,roomId])
  useEffect(()=>()=>{if(snapshot)URL.revokeObjectURL(snapshot)},[snapshot])
  const active=channels.find(c=>c.source_id===source&&c.kind==='video')
  const geometry:ZoneConfig|null=useMemo(()=>room&&active?{project_id:room.project_id,room_id:room.room_id,session_id:'room-resource-draft',source_id:source,config_version:version,zones:active.zones,entry_lines:active.entry_lines}:null,[room,active,source,version])
  async function save(){
    if(busy)return
    if(!channels.length||!primary||note.trim().length<2){setError("请添加视频机位、选择主机位并填写配置说明");return}
    setBusy(true);setError('')
    try{const value=await saveRoomPreset(roomId,{expected_version:version,scheme,primary_source_id:primary,channels,note:note.trim()});load(value);setVersion(value.version);query.reload();message.success("资源版本已保存，已有场次配置不变")}
    catch(cause){setError(cause instanceof Error?cause.message:'保存失败')}finally{setBusy(false)}
  }
  return <div className="hj-panel room-resources">
    <div className="roster-toolbar"><Select aria-label="维护资源的评标室" disabled={busy} showSearch optionFilterProp="label" value={roomId||undefined} onChange={id=>setParams(previous=>{const next=new URLSearchParams(previous);next.set('room_id',id);return next})} options={registry.data?.rooms.filter(r=>r.enabled).map(r=>({value:r.room_id,label:`${registry.data?.projects.find(p=>p.project_id===r.project_id)?.name||''} / ${r.name}`}))}/><Button disabled={busy} onClick={()=>{loaded.current='';query.reload();setDirty(false)}}>刷新资源</Button></div>
    {(error||query.error||registry.error)&&<Alert type="error" showIcon message={error||query.error||registry.error}/>}
    {!room?<Empty description="请先在“项目与场地”中新增评标室"/>:query.loading||loaded.current!==roomId?<Spin/>:<>
      <div className="hj-section-caption"><div><h3>机位与区域 <Tag>最新版本 {version||'未配置'}</Tag></h3><p className="hj-help">创建场次时复制所选资源版本，后续调整不影响已有场次。</p></div><Button icon={<PlusOutlined/>} disabled={busy} onClick={()=>{channelForm.resetFields();channelForm.setFieldsValue({kind:'video'});setOpen(true)}}>添加监测通道</Button></div>
      {query.data?.items.length? <Select aria-label="资源版本" disabled={busy} value={selected} style={{width:'100%',marginBottom:16}} onChange={id=>load(query.data?.items.find(p=>p.preset_id===id))} options={query.data.items.map(p=>({value:p.preset_id,label:`版本 ${p.version} · ${formatDateTime(p.created_at)} · ${p.created_by} · ${p.note}`}))}/>:null}
      {query.data&&query.data.items[0]?.version>version&&<Alert type="warning" showIcon message="资源已有新版本，请刷新后保存"/>}
      <Table rowKey="source_id" size="small" pagination={false} scroll={{x:680}} dataSource={channels} columns={[
        {title:'名称',dataIndex:'name'},{title:'通道',dataIndex:'source_id'},{title:'类型',render:(_,c)=>c.kind==='video'?'视频':'拾音'},
        {title:'范围',render:(_,c)=>`${c.zones.length} 个区域 / ${c.entry_lines.length} 条出入口线`},
        {title:'操作',render:(_,c)=><Space>{c.kind==='video'&&<Button size="small" disabled={busy} onClick={()=>{setSource(c.source_id);setSnapshot(null);setGeometryOpen(true)}}>标定区域</Button>}<Button size="small" danger disabled={busy} onClick={()=>{setChannels(values=>values.filter(v=>v.source_id!==c.source_id));setDirty(true);if(primary===c.source_id)setPrimary('');if(source===c.source_id)setSource('')}}>移出草稿</Button></Space>},
      ]}/>
      <Space wrap style={{marginTop:16}}><span>主机位</span><Select aria-label="主机位" disabled={busy} style={{minWidth:200}} value={primary||undefined} onChange={value=>{setPrimary(value);setDirty(true)}} options={channels.filter(c=>c.kind==='video').map(c=>({value:c.source_id,label:c.name}))}/><span>默认监测方案</span><Select aria-label="现场默认方案" disabled={busy} value={scheme} onChange={value=>{setScheme(value);setDirty(true)}} options={Object.entries(schemeLabels).map(([value,label])=>({value,label}))}/></Space>
      <p className="hj-help">基础方案无需标定；完整方案需配置工位区域和出入口线。</p>
      {active&&<Collapse key={source} activeKey={geometryOpen?['geometry']:[]} onChange={keys=>setGeometryOpen(keys.includes('geometry'))} items={[{key:'geometry',label:'区域标定 · '+active.name,children:<>
        <input type="file" accept="image/jpeg,image/png,image/webp" hidden ref={upload} onChange={e=>{const file=e.target.files?.[0];if(file&&file.size<=20*1024*1024)setSnapshot(URL.createObjectURL(file));else if(file)message.error("请选择不超过 20 MB 的截图")}}/>
        <Button icon={<UploadOutlined/>} onClick={()=>upload.current?.click()}>选择机位截图</Button><p className="hj-help">选择此机位的截图用于标定，图片仅在本机浏览器使用。</p>
        <ZoneEditor snapshotUrl={snapshot} config={geometry} seats={registry.data?.seats} saveOverride={async draft=>{setChannels(values=>values.map(c=>c.source_id===source?{...c,zones:draft.zones,entry_lines:draft.entry_lines}:c));setDirty(true);return {...geometry!,...draft}}}/>
      </>}]} />}
      <Form layout="vertical" style={{marginTop:20}}><Form.Item label="配置说明"><Input.TextArea aria-label="资源配置说明" disabled={busy} value={note} onChange={e=>{setNote(e.target.value);setDirty(true)}} rows={2} maxLength={500} placeholder="填写新增机位、调整布局等变更内容"/></Form.Item></Form>
      <Button type="primary" loading={busy} disabled={!dirty||busy} onClick={()=>void save()}>保存资源版本</Button>
    </>}
    <Modal title="添加监测通道" open={open} onCancel={()=>setOpen(false)} okText="添加" cancelText="取消" onOk={()=>void channelForm.validateFields().then(values=>{if(channels.some(c=>c.source_id===values.source_id)){message.error("此通道已添加");return}setChannels(c=>[...c,{...values,zones:[],entry_lines:[]}]);if(!primary&&values.kind==='video'){setPrimary(values.source_id);setSource(values.source_id)}setDirty(true);setOpen(false)})}>
      <Form form={channelForm} layout="vertical"><Form.Item name="name" label="通道名称" rules={[{required:true,whitespace:true}]}><Input maxLength={128}/></Form.Item><Form.Item name="source_id" label="通道标识" rules={[{required:true,pattern:/^[A-Za-z0-9_-]{1,64}$/}]} extra="由部署人员提供，例如 camera-001；无需填写摄像头凭据。"><Input maxLength={64}/></Form.Item><Form.Item name="kind" label="类型" rules={[{required:true}]}><Select options={[{value:'video',label:'视频机位'},{value:'audio',label:'拾音通道'}]}/></Form.Item></Form>
    </Modal>
  </div>
}
