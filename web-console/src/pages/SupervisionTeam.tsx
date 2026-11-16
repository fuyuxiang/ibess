import { useState } from 'react';
import { Alert, Button, Card, Form, Input, Modal, Popconfirm, Select, Space, Table, Tag } from 'antd';
import { authFetch, type AdminUser } from '../api/auth';
import { useRefreshingQuery } from '../components/workflow/useRefreshingQuery';
import { PageHeading, RequestError } from './Supervision';
import { passwordRules } from '../components/auth/Login';
const roleLabels:Record<string,string>={supervisor:'监管人员',operator:'处置人员',viewer:'只读人员'};
type Account=AdminUser&{account_id:string;active:boolean};
async function teamRequest<T>(path='',init?:RequestInit):Promise<T>{const r=await authFetch('/api/auth/team'+path,init);const b=await r.json();if(!r.ok)throw Error(typeof b.detail==='string'?b.detail:'账号配置无效，请核对输入');return b;}
export default function SupervisionTeam(){
  const query=useRefreshingQuery<Account[]>('team',signal=>teamRequest('',{signal}));const [open,setOpen]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('');const [form]=Form.useForm();
  async function save(){if(busy)return;try{const values=await form.validateFields();setBusy(true);setError('');await teamRequest('',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)});form.resetFields();setOpen(false);query.reload();}catch(e){if(e instanceof Error)setError(e.message);}finally{setBusy(false);}}
  async function patch(account:Account,values:Partial<Account>){if(busy)return;setBusy(true);setError('');try{await teamRequest('/'+account.account_id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)});query.reload();}catch(e){setError(e instanceof Error?e.message:'操作失败');}finally{setBusy(false);}}
  return <div className="sv-page"><PageHeading title="账号权限" description="管理账号、岗位及启用状态。" extra={<Button type="primary" onClick={()=>{setError('');form.resetFields();form.setFieldsValue({role:'operator'});setOpen(true);}}>新增账号</Button>}/>
    <RequestError error={error||query.error} reload={query.reload}/><Alert type="info" showIcon message="岗位权限说明" description="监管人员负责核查、分派、复核与导出；处置人员接收本人任务并反馈；只读人员可查阅。通知请按现场流程办理。"/>
    <Card><Table rowKey="account_id" dataSource={query.data||[]} loading={query.loading} pagination={false} columns={[{title:'账号',dataIndex:'username'},{title:"显示名称",dataIndex:'display_name'},{title:'岗位',render:(_,a:Account)=><Select aria-label={'岗位 '+a.username} value={a.role} disabled={busy} style={{width:150}} options={Object.entries(roleLabels).map(([value,label])=>({value,label}))} onChange={role=>void patch(a,{role})}/>},{title:'状态',render:(_,a:Account)=><Tag color={a.active?'green':'default'}>{a.active?'启用':'停用'}</Tag>},{title:'操作',render:(_,a:Account)=><Popconfirm title={`确认${a.active?'停用':'启用'}账号 ${a.username}？`} description="变更后，此账号的现有登录会话将失效。" onConfirm={()=>patch(a,{active:!a.active})}><Button disabled={busy} danger={a.active}>{a.active?'停用':'启用'}</Button></Popconfirm>}]}/></Card>
    <Modal title="新增账号" open={open} confirmLoading={busy} maskClosable={false} onCancel={()=>{if(!busy){setOpen(false);form.resetFields();}}} onOk={()=>void save()} okText="创建账号" cancelText="取消">
      {error&&<Alert type="error" message={error}/>}
      <Form form={form} layout="vertical" disabled={busy}><Form.Item name="username" label="登录账号" rules={[{required:true},{pattern:/^[A-Za-z0-9_]{3,32}$/,message:'3–32位字母、数字或下划线'}]}><Input autoComplete="off" maxLength={32}/></Form.Item><Form.Item name="display_name" label="显示名称" rules={[{required:true,whitespace:true}]}><Input maxLength={64}/></Form.Item><Form.Item name="role" label="工作岗位" rules={[{required:true}]}><Select options={Object.entries(roleLabels).map(([value,label])=>({value,label}))}/></Form.Item><Form.Item name="password" label="初始密码" rules={passwordRules}><Input.Password autoComplete="new-password" maxLength={128}/></Form.Item></Form>
      <Space><small>使用者可在账号菜单中修改密码。</small></Space>
    </Modal>
  </div>;
}
