import { useEffect, useState, type ReactNode } from 'react'
import { BellOutlined, CalendarOutlined, FileProtectOutlined, SettingOutlined, VideoCameraOutlined } from '@ant-design/icons'
import { ConfigProvider, theme, type ThemeConfig } from 'antd'
import { Link, useLocation } from 'react-router-dom'
import AccountMenu from '../auth/AccountMenu'
import { useAuthStore } from '../../store/useAuthStore'
import { notifyWorkflowChange } from '../../store/useWorkflowStore'
import { activeWorkspace, workspaceMode } from './workspace-navigation'

const darkTheme: ThemeConfig = {
  algorithm: theme.darkAlgorithm,
  token: { colorPrimary: '#52c7b0', colorInfo: '#52c7b0', colorBgBase: '#121820', colorBgContainer: '#1b232d', colorBgElevated: '#222c37', colorBorder: '#35414d', colorBorderSecondary: '#303c48', colorText: '#dce5ed', colorTextSecondary: '#95a4b1', borderRadius: 4, fontSize: 13 },
  components: { Table: { headerBg: '#202b36', headerColor: '#8c9caa', borderColor: '#303c48', rowHoverBg: '#24313b' }, Button: { primaryColor: '#10231f' }, Card: { headerBg: 'transparent' }, Input: { activeShadow: '0 0 0 2px #52c7b020' } },
}
const lightTheme: ThemeConfig = {
  token: { colorPrimary: '#237f70', colorInfo: '#237f70', colorText: '#2b3c49', colorTextSecondary: '#778791', colorBgLayout: '#eef1f3', colorBorder: '#dce3e7', borderRadius: 4, fontSize: 13 },
  components: { Card: { headerHeight: 48 }, Table: { headerBg: '#f4f6f7' } },
}
const navigation = [
  { path: '/overview', label: '实时监控', icon: <VideoCameraOutlined /> },
  { path: '/events', label: '告警处置', icon: <BellOutlined /> },
  { path: '/archives', label: '监管档案', icon: <FileProtectOutlined /> },
  { path: '/sessions', label: '场次管理', icon: <CalendarOutlined /> },
]
const management = [
  ['/admin/sessions', '现场配置'],
  ['/admin/rules', '识别规则'], ['/admin/assurance', '运行保障'], ['/admin/team', '账号权限'],
]
function Clock() {
  const [now, setNow] = useState(new Date())
  useEffect(() => { const timer = window.setInterval(() => setNow(new Date()), 1000); return () => window.clearInterval(timer) }, [])
  return <time className="duty-clock" dateTime={now.toISOString()}><span>{now.toLocaleDateString('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' })}</span>{now.toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })}</time>
}
export default function ConsoleShell({ children }: { children: ReactNode }) {
  const { pathname } = useLocation()
  const role = useAuthStore(state => state.user?.role)
  const mode = workspaceMode(pathname), dark = mode === 'duty' || mode === 'operations'
  const [connection, setConnection] = useState('connecting')
  useEffect(() => {
    const stream = new EventSource('/api/events/stream')
    let debounce: number | undefined
    stream.onopen = () => setConnection('connecting')
    stream.onerror = () => setConnection('polling')
    stream.onmessage = event => {
      if (event.data === 'connected') return
      try {
        const data = JSON.parse(event.data)
        if (data.type === 'stream_status') setConnection(data.status)
        else if (data.event_id) { clearTimeout(debounce); debounce = window.setTimeout(notifyWorkflowChange, 200) }
      } catch { /* Periodic refresh remains available. */ }
    }
    const timer = window.setInterval(notifyWorkflowChange, 15000)
    return () => { stream.close(); clearTimeout(debounce); clearInterval(timer) }
  }, [])
  return <ConfigProvider theme={dark ? darkTheme : lightTheme}>
    <div className={`hj-workspace ${dark ? 'is-dark' : 'is-light hj-app'} ${mode === 'duty' ? 'is-duty hj-duty-app' : ''} ${mode === 'admin' ? 'is-admin' : ''}`}>
      <a className="sv-skip-link" href="#main-content">跳到主要内容</a>
      <ConfigProvider theme={darkTheme}><header className="duty-header">
        <Link to="/overview" className="duty-brand"><img src="/brand-mark.svg" alt="" /><strong>衡鉴</strong><span>评标现场监管</span></Link>
        <nav aria-label="主要功能">{navigation.map(item => <Link key={item.path} to={item.path} className={activeWorkspace(pathname) === item.path ? 'active' : ''} aria-current={activeWorkspace(pathname) === item.path ? 'page' : undefined}>{item.icon}{item.label}</Link>)}</nav>
        <div className="duty-header-tools"><Clock /><span className={'duty-feed-state ' + (connection === 'live' ? 'is-live' : '')}><i />{connection === 'live' ? '实时同步' : connection === 'connecting' ? '正在连接' : '定时同步'}</span>
          {role === 'admin' && <Link to="/admin" className="duty-settings" aria-label="管理中心" title="管理中心"><SettingOutlined /></Link>}<AccountMenu />
        </div>
      </header></ConfigProvider>
      {mode === 'admin' && role === 'admin' && <nav className="console-admin-nav" aria-label="管理功能"><span>管理中心</span>{management.map(([path, label]) => <Link key={path} to={path} className={pathname === path ? 'active' : ''} aria-current={pathname === path ? 'page' : undefined}>{label}</Link>)}</nav>}
      <main id="main-content" className={mode === 'duty' ? 'duty-main' : 'console-main'} tabIndex={-1}>{children}</main>
    </div>
  </ConfigProvider>
}
