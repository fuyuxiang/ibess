import { lazy, Suspense } from 'react'
import type { ReactNode } from 'react'
import { Button, Result, Spin } from 'antd'
import { BrowserRouter, Link, Navigate, Route, Routes, useLocation, useSearchParams } from 'react-router-dom'
import ConsoleShell from './components/layout/ConsoleShell'
import ErrorBoundary from './components/ErrorBoundary'
import AuthGate from './components/auth/AuthGate'
import PersonVerification from './pages/PersonVerification'
import { useAuthStore } from './store/useAuthStore'
const Workbench=lazy(()=>import('./pages/Workbench'))
const EventList=lazy(()=>import('./pages/Supervision').then(m=>({default:m.SupervisionList})))
const Assurance=lazy(()=>import('./pages/MonitoringAssurance'))
const EventDetail=lazy(()=>import('./pages/SupervisionDetail'))
const Sessions=lazy(()=>import('./pages/Sessions'))
const SessionWorkspace=lazy(()=>import('./pages/SessionWorkspace'))
const SessionSetup=lazy(()=>import('./pages/SiteConfiguration'))
const Maintenance=lazy(()=>import('./pages/Maintenance'))
const Rules=lazy(()=>import('./pages/SupervisionRules'))
const Archives=lazy(()=>import('./pages/SessionArchives'))
const Team=lazy(()=>import('./pages/SupervisionTeam'))

function AdminOnly({children}:{children:ReactNode}) {return useAuthStore(s=>s.user?.role)==='admin'?children:<Result status="403" title="需要管理员权限" subTitle="请联系管理员，或返回实时监控。" extra={<Link to="/overview"><Button type="primary">返回实时监控</Button></Link>}/>}
function LegacyRedirect({to}:{to:string}) {const location=useLocation();const [path,defaults]=to.split('?');const params=new URLSearchParams(location.search);new URLSearchParams(defaults).forEach((value,key)=>params.set(key,value));return <Navigate to={path+(params.size?'?'+params.toString():'')} replace/>}
function ProcessRoute(){const [params]=useSearchParams();return params.get('event_id')?<EventDetail/>:<Navigate to="/events?queue=dispatch" replace/>}
function EvidenceRoute(){const [params]=useSearchParams();return params.get('event_id')?<EventDetail/>:<Navigate to="/archives" replace/>}
function Shell(){
  const location=useLocation()
  return <ConsoleShell><ErrorBoundary key={location.pathname}><Suspense fallback={<div className="sv-loading"><Spin/><span>正在加载…</span></div>}><Routes>
        <Route path="/" element={<Workbench/>}/><Route path="/overview" element={<Workbench/>}/><Route path="/sessions" element={<Sessions/>}/><Route path="/sessions/:sessionId" element={<SessionWorkspace/>}/>
        <Route path="/monitor" element={<Workbench/>}/><Route path="/events" element={<EventList/>}/><Route path="/event-detail" element={<EventDetail/>}/><Route path="/archives" element={<Archives/>}/><Route path="/audio" element={<LegacyRedirect to="/events"/>}/>
        <Route path="/admin" element={<AdminOnly><LegacyRedirect to="/admin/sessions"/></AdminOnly>}/><Route path="/admin/sessions" element={<AdminOnly><SessionSetup/></AdminOnly>}/><Route path="/admin/registry" element={<AdminOnly><LegacyRedirect to="/admin/sessions?tab=registry"/></AdminOnly>}/><Route path="/admin/authorizations" element={<AdminOnly><LegacyRedirect to="/admin/sessions?setup=people"/></AdminOnly>}/><Route path="/admin/rules" element={<AdminOnly><Rules/></AdminOnly>}/><Route path="/admin/assurance" element={<AdminOnly><Assurance/></AdminOnly>}/><Route path="/admin/maintenance" element={<AdminOnly><Maintenance/></AdminOnly>}/><Route path="/admin/team" element={<AdminOnly><Team/></AdminOnly>}/><Route path="/admin/face-verify" element={<AdminOnly><LegacyRedirect to="/admin/authorizations"/></AdminOnly>}/><Route path="/admin/video-audit" element={<AdminOnly><LegacyRedirect to="/archives"/></AdminOnly>}/><Route path="/admin/advanced-stages" element={<AdminOnly><LegacyRedirect to="/admin/sessions"/></AdminOnly>}/>
        <Route path="/settings" element={<LegacyRedirect to="/admin/rules"/>}/><Route path="/registry" element={<LegacyRedirect to="/admin/registry"/>}/><Route path="/facts" element={<LegacyRedirect to="/admin/authorizations"/>}/><Route path="/team" element={<LegacyRedirect to="/admin/team"/>}/><Route path="/face-verify" element={<LegacyRedirect to="/admin/face-verify"/>}/><Route path="/video-audit" element={<LegacyRedirect to="/admin/video-audit"/>}/><Route path="/assurance" element={<Assurance/>}/><Route path="/stages/console" element={<LegacyRedirect to="/admin/sessions"/>}/><Route path="/stages" element={<LegacyRedirect to="/sessions"/>}/><Route path="/stages/guide" element={<LegacyRedirect to="/sessions"/>}/><Route path="/manual" element={<ProcessRoute/>}/><Route path="/process" element={<ProcessRoute/>}/><Route path="/evidence" element={<EvidenceRoute/>}/><Route path="/history" element={<EventList/>}/><Route path="*" element={<Result status="404" title="未找到页面" extra={<Link to="/overview"><Button>返回实时监控</Button></Link>}/>}/>
      </Routes></Suspense></ErrorBoundary></ConsoleShell>
}
function Entry(){const {pathname}=useLocation();if(pathname==='/verify'||pathname.startsWith('/participant-verification'))return <PersonVerification/>;return <AuthGate><Shell/></AuthGate>}
export default function App(){return <BrowserRouter future={{v7_startTransition:true,v7_relativeSplatPath:true}}><Entry/></BrowserRouter>}
