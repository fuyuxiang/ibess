import { useEffect, useState } from 'react'
import { Alert, Button, Collapse, Form, Input, InputNumber, Modal, Select } from 'antd'
import { Link, useNavigate } from 'react-router-dom'
import { useRegistry } from '../../store/useRegistryStore'
import { useRefreshingQuery } from './useRefreshingQuery'
import { fetchRoomPresets, schemeLabels } from '../../api/roomMonitoring'
import { supervisionRequest } from '../../api/supervision'
import type { SessionWorkflow } from '../../api/sessionWorkflow'
import { notifyWorkflowChange } from '../../store/useWorkflowStore'

export default function CreateSession({open,onClose}:{open:boolean;onClose:()=>void}){
  const registry=useRegistry(),navigate=useNavigate(),[form]=Form.useForm()
  const roomId=Form.useWatch('room_id',form),projectId=Form.useWatch('project_id',form)
  const presets=useRefreshingQuery('create-presets:'+roomId,signal=>fetchRoomPresets(roomId,signal),!!roomId&&open)
  const [busy,setBusy]=useState(false),[error,setError]=useState('')
  useEffect(()=>{if(open){form.resetFields();setError('')}},[open,form])
  useEffect(()=>{if(!form.getFieldValue('preset_id'))form.setFieldsValue({preset_id:presets.data?.items[0]?.preset_id})},[roomId,presets.data,form])
  async function create(){
    try{const values=await form.validateFields();if(busy)return;setBusy(true);setError('')
      const result=await supervisionRequest<SessionWorkflow>('/supervision/workflows',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)})
      notifyWorkflowChange();onClose();navigate('/sessions/'+result.session_id+'?prepare=1')
    }catch(cause){if(cause instanceof Error)setError(cause.message)}finally{setBusy(false)}
  }
  return <Modal title="新建场次" open={open} onCancel={()=>{if(!busy)onClose()}} onOk={()=>void create()} confirmLoading={busy} okText="创建并准备" cancelText="取消" destroyOnHidden>
    {(error||registry.error||presets.error)&&<Alert type="error" showIcon message={error||registry.error||presets.error}/>}
    <Form form={form} layout="vertical" preserve={false} disabled={busy}>
      <Form.Item name="name" label="场次名称" rules={[{required:true,whitespace:true,min:2}]}><Input maxLength={128}/></Form.Item>
      <Form.Item name="project_id" label="项目" rules={[{required:true}]}><Select onChange={()=>form.resetFields(['room_id','preset_id'])} options={registry.data?.projects.filter(p=>p.status!=='closed').map(p=>({value:p.project_id,label:p.name}))}/></Form.Item>
      <Form.Item name="room_id" label="评标室" rules={[{required:true}]}><Select onChange={()=>form.resetFields(['preset_id'])} options={registry.data?.rooms.filter(r=>r.enabled&&r.project_id===projectId).map(r=>({value:r.room_id,label:r.name}))}/></Form.Item>
      <Form.Item name="preset_id" label="现场资源" rules={[{required:true,message:"请选择现场资源版本"}]} extra="复制所选机位与区域，保留本场独立配置。"><Select loading={presets.loading} options={presets.data?.items.map(p=>({value:p.preset_id,label:`版本 ${p.version} · ${p.channels.filter(c=>c.kind==='video').length} 路画面 · ${schemeLabels[p.scheme]}`}))}/></Form.Item>
      {roomId&&presets.data&&!presets.data.items.length&&<Alert type="info" message="此评标室暂无监测资源" action={<Link to={'/admin/sessions?room_id='+roomId} onClick={onClose}><Button size="small">配置现场资源</Button></Link>}/>}
      <Collapse ghost items={[{key:'extra',label:'补充信息（选填）',forceRender:true,children:<Form.Item name="expected_people" label="计划人数"><InputNumber min={0} max={1000}/></Form.Item>}]}/>
    </Form>
  </Modal>
}
