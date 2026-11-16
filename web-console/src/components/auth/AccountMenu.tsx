import { useRef, useState } from 'react'
import { DownOutlined, LockOutlined, LogoutOutlined, UserOutlined } from '@ant-design/icons'
import { Alert, Button, Dropdown, Form, Input, Modal } from 'antd'
import type { ChangePasswordInput } from '../../api/auth'
import { useAuthStore } from '../../store/useAuthStore'
import { passwordRules } from './Login'
import '../../auth.css'

interface PasswordValues extends ChangePasswordInput { confirm_password: string }

export function AccountMenu() {
  const user = useAuthStore((state) => state.user)
  const loading = useAuthStore((state) => state.loading)
  const logout = useAuthStore((state) => state.logout)
  const changePassword = useAuthStore((state) => state.changePassword)
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [logoutError, setLogoutError] = useState<string | null>(null)
  const lock = useRef(false)
  const [form] = Form.useForm<PasswordValues>()
  if (!user) return null

  async function exit() {
    if (lock.current) return
    lock.current = true
    setLogoutError(null)
    try { await logout() }
    catch (cause) { setLogoutError(cause instanceof Error ? cause.message : '退出失败，请重试') }
    finally { lock.current = false }
  }

  async function submit(values: PasswordValues) {
    if (lock.current) return
    lock.current = true
    setError(null)
    try {
      await changePassword({ current_password: values.current_password, new_password: values.new_password })
      form.resetFields()
      setOpen(false)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '修改密码失败，请重试')
      form.resetFields(['current_password', 'new_password', 'confirm_password'])
    } finally { lock.current = false }
  }

  return <div className="auth-account">
    <Dropdown trigger={['click']} menu={{ items: [{ key: 'password', label: '修改密码', icon: <LockOutlined />, disabled: loading }, { type: 'divider' }, { key: 'logout', label: '退出登录', icon: <LogoutOutlined />, danger: true, disabled: loading }], onClick: ({ key }) => { if (key === 'password') { setError(null); form.resetFields(); setOpen(true) } else if (key === 'logout') void exit() } }}>
      <button className="auth-account-trigger" type="button" aria-label="账户菜单" disabled={loading}><span className="auth-account-avatar"><UserOutlined /></span><span className="auth-account-name" title={`${user.username} · ${({admin:'系统管理',supervisor:'监标审核',operator:'现场处置',viewer:'只读查看'})[user.role]}`}>{user.display_name || user.username}</span><DownOutlined /></button>
    </Dropdown>
    {logoutError && <div className="auth-account-error"><Alert type="error" showIcon message="退出登录失败" description={logoutError} action={<Button size="small" loading={loading} onClick={() => void exit()}>重试</Button>} closable onClose={() => setLogoutError(null)} /></div>}
    <Modal title="修改登录密码" open={open} footer={null} closable={!loading} maskClosable={!loading} keyboard={!loading} onCancel={() => { if (!lock.current) { setOpen(false); form.resetFields() } }}>
      <Alert type="info" showIcon message="修改后需要重新登录" description="新密码保存成功后，所有标签页和现有会话都会退出。" />
      {error && <Alert className="auth-inline-alert" type="error" showIcon message={error} />}
      <Form className="auth-password-form" form={form} name="admin-password" layout="vertical" disabled={loading} onFinish={submit} preserve={false}>
        <Form.Item name="current_password" label="当前密码" rules={[{ required: true, message: '请输入当前密码' }]}><Input.Password autoComplete="current-password" maxLength={128} /></Form.Item>
        <Form.Item name="new_password" label="新密码" rules={passwordRules}><Input.Password autoComplete="new-password" maxLength={128} placeholder="10–128 个字符，含英文字母和数字" /></Form.Item>
        <Form.Item name="confirm_password" label="确认新密码" dependencies={['new_password']} rules={[{ required: true, message: '请再次输入新密码' }, ({ getFieldValue }) => ({ validator(_, value: string) { return !value || value === getFieldValue('new_password') ? Promise.resolve() : Promise.reject(new Error('两次输入的新密码不一致')) } })]}><Input.Password autoComplete="new-password" maxLength={128} /></Form.Item>
        <div className="auth-form-actions"><Button disabled={loading} onClick={() => { setOpen(false); form.resetFields() }}>取消</Button><Button type="primary" htmlType="submit" loading={loading}>保存新密码</Button></div>
      </Form>
    </Modal>
  </div>
}

export default AccountMenu
