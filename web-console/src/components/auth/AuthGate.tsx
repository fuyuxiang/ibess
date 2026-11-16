import { useEffect } from 'react'
import type { ReactNode } from 'react'
import { Alert, Button, Card, Spin } from 'antd'
import { ReloadOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { Navigate, useLocation } from 'react-router-dom'
import { safeReturnTo } from '../../api/auth'
import { useAuthStore } from '../../store/useAuthStore'
import Login from './Login'
import '../../auth.css'

export function AuthGate({ children }: { children: ReactNode }) {
  const user = useAuthStore((state) => state.user)
  const checked = useAuthStore((state) => state.checked)
  const loading = useAuthStore((state) => state.loading)
  const error = useAuthStore((state) => state.error)
  const check = useAuthStore((state) => state.check)
  const location = useLocation()
  useEffect(() => { if (!useAuthStore.getState().checked) void check() }, [check])

  if (!checked) return <main className="auth-screen"><Card className="auth-state-card"><SafetyCertificateOutlined className="auth-state-icon" /><h1>智能评标监管系统</h1>{error ? <><Alert type="error" showIcon message="暂时无法检查登录状态" description={error} /><Button type="primary" icon={<ReloadOutlined />} loading={loading} onClick={() => void check()}>重试</Button></> : <div role="status" className="auth-checking"><Spin /><p>正在检查登录状态…</p></div>}</Card></main>
  if (location.pathname === '/login') {
    if (user) return <Navigate replace to={safeReturnTo(new URLSearchParams(location.search).get('returnTo'))} />
    return <Login />
  }
  if (!user) {
    const returnTo = safeReturnTo(`${location.pathname}${location.search}${location.hash}`)
    return <Navigate replace to={`/login?returnTo=${encodeURIComponent(returnTo)}`} />
  }
  return <>{children}</>
}

export default AuthGate
