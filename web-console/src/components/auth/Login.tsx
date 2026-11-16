import { useRef, useState } from 'react'
import { LockOutlined, ReloadOutlined, UserOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Form, Input } from 'antd'
import { useLocation, useNavigate } from 'react-router-dom'
import { AuthError, safeReturnTo } from '../../api/auth'
import type { SetupInput } from '../../api/auth'
import { useAuthStore } from '../../store/useAuthStore'
import '../../auth.css'

interface LoginValues extends SetupInput { confirm_password?: string }
export const usernameRules = [{ required: true, message: '请输入登录账号' }, { pattern: /^[A-Za-z0-9_]{3,32}$/, message: "使用 3–32 位英文字母、数字或下划线" }]
export const passwordRules = [{ required: true, message: '请输入密码' }, { min: 10, max: 128, message: "密码长度为 10–128 个字符" }, { pattern: /^(?=.*[A-Za-z])(?=.*[0-9])[\s\S]+$/, message: "密码需包含英文字母和数字" }]

export function Login() {
  const initialized = useAuthStore((state) => state.initialized)
  const loading = useAuthStore((state) => state.loading)
  const notice = useAuthStore((state) => state.notice)
  const login = useAuthStore((state) => state.login)
  const setup = useAuthStore((state) => state.setup)
  const check = useAuthStore((state) => state.check)
  const [error, setError] = useState<string | null>(null)
  const lock = useRef(false)
  const [form] = Form.useForm<LoginValues>()
  const location = useLocation()
  const navigate = useNavigate()
  const returnTo = safeReturnTo(new URLSearchParams(location.search).get('returnTo'))

  async function submit(values: LoginValues) {
    if (lock.current) return
    lock.current = true
    setError(null)
    try {
      if (initialized) await login({ username: values.username, password: values.password })
      else await setup({ username: values.username, display_name: values.display_name.trim(), password: values.password })
      form.resetFields(['password', 'confirm_password'])
      if (useAuthStore.getState().user) navigate(returnTo, { replace: true })
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '登录失败，请重试')
      form.resetFields(['password', 'confirm_password'])
      if (cause instanceof AuthError && cause.status === 409) await check()
    } finally { lock.current = false }
  }

  return <main className="auth-screen"><div className="auth-layout">
    <header className="auth-brand"><img src="/brand-mark.svg" alt=""/><div><strong>衡鉴 <small>HENGJIAN</small></strong><span>智能评标现场监管平台</span></div></header>
    <section className="auth-intro"><span>FIELD SUPERVISION</span><h2>实时掌握现场，<br/>全程有据可查。</h2><p>统一管理评前准备、现场监控、<br/>告警处置与证据归档。</p><div className="auth-process"><b>01</b> 评前准备 <i>→</i><b>02</b> 现场监控 <i>→</i><b>03</b> 处置归档</div></section>
    <Card className="auth-login-card"><div className="auth-form-heading"><span className="auth-eyebrow">{initialized ? '账号登录' : '首次设置'}</span><h1>{initialized ? "登录衡鉴" : '创建管理员账号'}</h1><p>{initialized ? "查看现场、处理告警、查阅档案。" : "首次使用，请设置管理员账号。"}</p></div>
      {notice && <Alert className="auth-inline-alert" type="info" showIcon message={notice} />}
      {error && <Alert className="auth-inline-alert" type="error" showIcon message={error} />}
      <Form key={initialized ? 'login' : 'setup'} form={form} name="admin-login" layout="vertical" onFinish={submit} disabled={loading} requiredMark={false} preserve={false}>
        <Form.Item label={initialized ? "登录账号" : "管理员账号"} name="username" rules={usernameRules}><Input prefix={<UserOutlined />} autoComplete="username" maxLength={32} placeholder="3–32 位英文字母、数字或下划线" autoFocus /></Form.Item>
        {!initialized && <Form.Item label="管理员名称" name="display_name" rules={[{ required: true, whitespace: true, message: '请输入管理员名称' }, { max: 64, message: '管理员名称不能超过 64 个字符' }]}><Input maxLength={64} autoComplete="name" placeholder="用于账号显示和操作记录" /></Form.Item>}
        <Form.Item label="密码" name="password" rules={passwordRules}><Input.Password prefix={<LockOutlined />} autoComplete={initialized ? 'current-password' : 'new-password'} maxLength={128} placeholder="10–128 个字符，含英文字母和数字" /></Form.Item>
        {!initialized && <Form.Item label="确认密码" name="confirm_password" dependencies={['password']} rules={[{ required: true, message: '请再次输入密码' }, ({ getFieldValue }) => ({ validator(_, value: string) { return !value || value === getFieldValue('password') ? Promise.resolve() : Promise.reject(new Error('两次输入的密码不一致')) } })]}><Input.Password prefix={<LockOutlined />} autoComplete="new-password" maxLength={128} placeholder="再次输入密码" /></Form.Item>}
        <Button className="auth-submit" type="primary" htmlType="submit" loading={loading} block>{initialized ? '登录' : '创建管理员并登录'}</Button>
      </Form>
      {error && <Button className="auth-recheck" type="link" icon={<ReloadOutlined />} disabled={loading} onClick={async () => { await check(); const state = useAuthStore.getState(); setError(state.error) }}>刷新登录状态</Button>}
      <p className="auth-form-note">{initialized ? "请使用管理员分配的账号登录。" : "设置完成后，可为其他岗位创建账号。"}</p>
    </Card>
    <footer className="auth-footer">衡鉴 HengJian · 智能评标现场监管平台</footer>
  </div></main>
}

export default Login
