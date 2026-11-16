import { lazy, Suspense, useEffect, useState } from "react";
import {
  BellOutlined,
  DashboardOutlined,
  FileProtectOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
  ToolOutlined,
  VideoCameraOutlined,
  ApartmentOutlined,
  AudioOutlined,
  IdcardOutlined,
  FundProjectionScreenOutlined,
  FileDoneOutlined,
  PlayCircleOutlined,
} from "@ant-design/icons";
import {
  Badge,
  Breadcrumb,
  Button,
  Input,
  Layout,
  Menu,
  Result,
  Space,
  Spin,
} from "antd";
import {
  BrowserRouter,
  Link,
  Navigate,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useSearchParams,
} from "react-router-dom";
import ErrorBoundary from "./components/ErrorBoundary";
import AuthGate from "./components/auth/AuthGate";
import AccountMenu from "./components/auth/AccountMenu";
import PersonVerification from "./pages/PersonVerification";
import { notifyWorkflowChange } from "./store/useWorkflowStore";
import { useSupervision } from "./components/workflow/useSupervision";
import { useAuthStore } from './store/useAuthStore';
import type { ReactNode } from 'react';

const Workbench = lazy(() =>
  import("./pages/Supervision").then((m) => ({ default: m.Workbench })),
);
const EventList = lazy(() =>
  import("./pages/Supervision").then((m) => ({ default: m.SupervisionList })),
);
const Monitor = lazy(() => import("./pages/SupervisionMonitor"));
const Assurance = lazy(() =>
  import("./pages/SupervisionMonitor").then((m) => ({
    default: m.MonitoringAssurance,
  })),
);
const EventDetail = lazy(() => import("./pages/SupervisionDetail"));
const Rules = lazy(() => import("./pages/SupervisionRules"));
const Registry = lazy(() => import("./pages/Registry"));
const Stages = lazy(() => import("./pages/Stages"));
const StageConsole = lazy(() => import("./pages/StageConsole"));
const StageFlow = lazy(() => import("./pages/StageFlow"));
const VideoAudit = lazy(() => import("./pages/VideoAudit"));
const FaceVerify = lazy(() => import("./pages/FaceVerifyPage"));
const Facts = lazy(() => import("./components/facts/FactsPanel"));
const AudioMonitor = lazy(() => import("./pages/SupervisionAudio"));
const Archives = lazy(() => import('./pages/SessionArchives'));
const Team = lazy(() => import('./pages/SupervisionTeam'));
declare const __BUILD_TIME__: string;

const primary = [
  { key: "/overview", label: "监管工作台", icon: <DashboardOutlined /> },
  { key: "/monitor", label: "现场监测", icon: <VideoCameraOutlined /> },
  { key: "/events", label: "事件中心", icon: <BellOutlined /> },
  { key: "/archives", label: "监管档案", icon: <FileProtectOutlined /> },
];
const support = [
  { key: "/assurance", label: "监测保障", icon: <ToolOutlined /> },
  { key: "/settings", label: "规则管理", icon: <SettingOutlined /> },
  { key: "/registry", label: "项目与人员", icon: <ApartmentOutlined /> },
  { key: "/facts", label: "业务许可与例外", icon: <FileDoneOutlined /> },
];
const tools = [
  { key: "/audio", label: "音频分析", icon: <AudioOutlined /> },
  { key: "/video-audit", label: "历史视频审计", icon: <PlayCircleOutlined /> },
  { key: "/face-verify", label: "身份核验", icon: <IdcardOutlined /> },
  {
    key: "/stages",
    label: "评标阶段看板",
    icon: <FundProjectionScreenOutlined />,
  },
  {
    key: "/stages/console",
    label: "场次与场景配置",
    icon: <SettingOutlined />,
  },
];
function ProcessRoute() {
  const [params] = useSearchParams();
  return params.get("event_id") ? (
    <EventDetail />
  ) : (
    <Navigate to={'/events?'+new URLSearchParams({...Object.fromEntries(params),queue:params.get('workflow_status')==='FEEDBACK'?'review':'dispatch'}).toString()} replace/>
  );
}
function EvidenceRoute() {
  const [params] = useSearchParams();
  return params.get("event_id") ? (
    <EventDetail />
  ) : (
    <Archives />
  );
}
function Shell() {
  const role=useAuthStore(s=>s.user?.role);
  const navigate = useNavigate();
  const location = useLocation();
  const invalidate = notifyWorkflowChange;
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem("sider-collapsed") === "1",
  );
  const [narrow, setNarrow] = useState(
    () => window.matchMedia("(max-width: 991px)").matches,
  );
  const [mobileOpen, setMobileOpen] = useState(false);
  const siderCollapsed = narrow ? !mobileOpen : collapsed;
  const navigateMenu = (key: string) => {
    navigate(key);
    if (narrow) setMobileOpen(false);
  };
  const [connection, setConnection] = useState<
    "connecting" | "live" | "polling"
  >("connecting");
  const [search, setSearch] = useState("");
  const summary = useSupervision({ page_size: 1, category: "behavior" });
  useEffect(() => {
    localStorage.setItem("sider-collapsed", collapsed ? "1" : "0");
  }, [collapsed]);
  useEffect(() => {
    invalidate();
    const es = new EventSource("/api/events/stream");
    let debounce: number | undefined;
    es.onopen = () => {
      setConnection("connecting");
      invalidate();
    };
    es.onerror = () => setConnection("polling");
    es.onmessage = (e) => {
      if (e.data === "connected") return;
      try {
        const event = JSON.parse(e.data);
        if (event.type === "stream_status") {
          setConnection(event.status === "live" ? "live" : "polling");
          return;
        }
        if (event.event_id) {
          window.clearTimeout(debounce);
          debounce = window.setTimeout(invalidate, 200);
        }
      } catch {
        /* 定时补查 */
      }
    };
    const timer = window.setInterval(invalidate, 15000);
    return () => {
      es.close();
      window.clearTimeout(debounce);
      window.clearInterval(timer);
    };
  }, [invalidate]);
  const path = location.pathname === "/" ? "/overview" : location.pathname;
  const current = [...primary, ...support, ...tools]
    .sort((a, b) => b.key.length - a.key.length)
    .find((m) => path === m.key || path.startsWith(m.key + "/"));
  const selected =
    path === "/event-detail"
      ? "/events"
      : path === "/history"
        ? "/events"
        : path === "/evidence"
          ? "/archives"
          : current?.key || "";
  const label =
    path === "/event-detail" ? "事件核查" : path==='/team'?'岗位与账号':current?.label || "监管业务";
  return (
    <Layout className="supervision-shell">
      <a href="#main-content" className="sv-skip-link">
        跳到主要内容
      </a>
      <header className="sv-topbar">
        <button
          className="sv-menu-toggle"
          aria-label={siderCollapsed ? "展开导航" : "收起导航"}
          onClick={() =>
            narrow ? setMobileOpen((v) => !v) : setCollapsed((v) => !v)
          }
        >
          {siderCollapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
        </button>
        <Link to="/overview" className="sv-brand">
          <span className="sv-brand-mark">
            <SafetyCertificateOutlined />
          </span>
          <span>
            <strong>智能评标监管系统</strong>
            <small>评标现场音视频异常行为分析预警</small>
          </span>
        </Link>
        <Input.Search
          className="sv-global-search"
          aria-label="全局事件搜索"
          placeholder="检索事件、项目、评标室"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onSearch={(value) =>
            navigate(`/events?keyword=${encodeURIComponent(value.trim())}`)
          }
          allowClear
        />
        <Space className="sv-top-tools" size={20}>
          <span className="sv-connection">
            <Badge
              status={
                summary.error
                  ? "error"
                  : connection === "live"
                    ? "success"
                    : "warning"
              }
            />
            {summary.error
              ? "数据连接异常"
              : connection === "live"
                ? "实时更新"
                : connection === "polling"
                  ? "定时同步中"
                  : "连接中"}
          </span>
          <button
            className="sv-notice-button"
            aria-label={`待核查事件 ${summary.data?.summary.confirm ?? "未加载"} 条`}
            onClick={() => navigate("/events?queue=confirm")}
          >
            <Badge count={summary.data?.summary.confirm || 0} size="small">
              <BellOutlined />
            </Badge>
          </button>
          <AccountMenu />
        </Space>
      </header>
      <Layout>
        {narrow && mobileOpen && (
          <button
            className="sv-nav-mask"
            aria-label="关闭导航"
            onClick={() => setMobileOpen(false)}
          />
        )}
        <Layout.Sider
          className="sv-sidebar"
          width={212}
          collapsedWidth={0}
          collapsed={siderCollapsed}
          aria-hidden={siderCollapsed}
          trigger={null}
          breakpoint="lg"
          onBreakpoint={(broken) => {
            setNarrow(broken);
            setMobileOpen(false);
          }}
        >
          <div className="sv-sidebar-label">监管业务</div>
          <Menu
            mode="inline"
            selectedKeys={[selected]}
            items={primary}
            onClick={({ key }) => navigateMenu(key)}
          />
          <div className="sv-sidebar-label">管理支撑</div>
          <Menu
            mode="inline"
            selectedKeys={[selected]}
            items={[
              ...(role==='admin'?support:[support[0]]),
              ...(role==='admin'?[{key:'/team',label:'岗位与账号',icon:<ApartmentOutlined/>}]:[]),
              {
                key: "specialized",
                icon: <SettingOutlined />,
                label: "专项工具",
                children: role==='admin'?tools:tools.filter(t=>t.key==='/audio'),
              },
            ]}
            onClick={({ key }) => navigateMenu(key)}
          />
          <div className="sv-sidebar-footer">
            <SafetyCertificateOutlined />
            <span>发现 · 确认 · 处置 · 留痕</span>
            <small>以事实为依据，以记录可追溯</small>
          </div>
        </Layout.Sider>
        <Layout className="sv-main-layout">
          <div className="sv-breadcrumb">
            <Breadcrumb
              items={[
                { title: <Link to="/overview">监管首页</Link> },
                { title: label },
              ]}
            />
            <span>现场行为监管</span>
          </div>
          <main id="main-content" className="sv-main" tabIndex={-1}>

            <ErrorBoundary key={location.pathname}>
              <Suspense
                fallback={
                  <div className="sv-loading" role="status">
                    <Spin />
                    <span>正在加载业务页面…</span>
                  </div>
                }
              >
                <Routes>
                  <Route path="/" element={<Workbench />} />
                  <Route path="/overview" element={<Workbench />} />
                  <Route path="/monitor" element={<Monitor />} />
                  <Route path="/events" element={<EventList />} />
                  <Route path="/event-detail" element={<EventDetail />} />
                  <Route path="/manual" element={<ProcessRoute />} />
                  <Route
                    path="/archives"
                    element={<Archives />}
                  />
                  <Route path="/evidence" element={<EvidenceRoute />} />
                  <Route path="/history" element={<EventList />} />
                  <Route path="/assurance" element={<Assurance />} />
                  <Route path="/settings" element={<AdminOnly><Rules /></AdminOnly>} />
                  <Route path="/registry" element={<AdminOnly><Registry /></AdminOnly>} />
                  <Route path="/facts" element={<AdminOnly><Facts /></AdminOnly>} />
                  <Route path="/team" element={<AdminOnly><Team /></AdminOnly>} />
                  <Route path="/audio" element={<AudioMonitor />} />
                  <Route path="/face-verify" element={<AdminOnly><FaceVerify /></AdminOnly>} />
                  <Route path="/video-audit" element={<AdminOnly><VideoAudit /></AdminOnly>} />
                  <Route path="/stages" element={<AdminOnly><StageConsole /></AdminOnly>} />
                  <Route path="/stages/guide" element={<AdminOnly><StageFlow /></AdminOnly>} />
                  <Route path="/stages/console" element={<AdminOnly><Stages /></AdminOnly>} />
                  <Route
                    path="*"
                    element={
                      <Result
                        status="404"
                        title="页面不存在"
                        subTitle="请通过左侧导航进入业务页面。"
                        extra={
                          <Link to="/overview">
                            <Button type="primary">返回监管工作台</Button>
                          </Link>
                        }
                      />
                    }
                  />
                </Routes>
              </Suspense>
            </ErrorBoundary>
          </main>
          <footer className="sv-system-footer">
            <span>智能评标监管系统</span>
            <span>构建 {__BUILD_TIME__}</span>
          </footer>
        </Layout>
      </Layout>
    </Layout>
  );
}
function AdminOnly({children}:{children:ReactNode}){const role=useAuthStore(s=>s.user?.role);return role==='admin'?<>{children}</>:<Result status="403" title="此页面由系统管理员维护" subTitle="日常核查请使用事件中心，处置账号可接收本人负责的任务。" extra={<Link to="/events"><Button>返回事件中心</Button></Link>}/>;}
function Entry() {
  const { pathname } = useLocation();
  if (pathname === "/verify" || pathname === "/verify/")
    return <PersonVerification />;
  if (pathname === "/show" || pathname.startsWith("/show/"))
    return <Navigate to="/stages" replace />;
  return (
    <AuthGate>
      <Shell />
    </AuthGate>
  );
}
export default function App() {
  return (
    <BrowserRouter
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
    >
      <Entry />
    </BrowserRouter>
  );
}
