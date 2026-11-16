import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  ArrowLeftOutlined, ArrowRightOutlined, AuditOutlined, CheckCircleFilled, ClockCircleOutlined,
  CloseCircleFilled, ExportOutlined, FullscreenExitOutlined, FullscreenOutlined, ReloadOutlined, SettingOutlined, VideoCameraOutlined,
} from '@ant-design/icons'
import { Button, Empty, Segmented, Spin, Tag, message } from 'antd'
import Hls from 'hls.js'
import { fetchStageSummary } from '../api/stageSummary'
import type { StageSummary } from '../api/stageSummary'
import { fetchStageConfig, saveStageConfig, PHASE_TITLES } from '../api/stages'
import type { PhaseId, StageConfig } from '../api/stages'
import { authFetch } from '../api/auth'
import { fetchOpsHealth } from '../api/ops'
import type { OpsHealth } from '../api/ops'
import { eventTypeLabels, formatDateTime, formatTime, riskColor, riskText } from '../utils/ui'
import { STAGE_SPEC } from '../components/stageflow/stageSpec'
import '../stage-live.css'

type EventRow = {
  event_id: string
  event_type: string
  camera_id: string | null
  risk_level: string
  reason: string
  occurred_at: string | null
  status: string
  phase_id?: string | null
  thumb?: string | null
}

const PHASE_IDS: PhaseId[] = STAGE_SPEC.map((item) => item.id)

// 场景对应的事件类型：本阶段只显示这些类型的事件，基础态势（人员/聚集）始终保留
const SCENE_TYPES: Record<string, string[]> = {
  S01: ['phone_unstored'],
  S02: ['phone_call'],
  S03: ['seat_absence', 'seat_vacancy', 'frequent_absence', 'joint_absence'],
  S04: ['unauthorized_entry'],
  S05: ['zone_intrusion', 'whispering', 'workstation_visit', 'close_contact'],
  S06: ['long_talking', 'sensitive_word'],
  S07: ['sensitive_word'],
  S08: ['phone_capture', 'document_transfer', 'document_carry_out'],
  S09: ['sensitive_word', 'long_talking'],
  S10: ['video_offline', 'video_online', 'camera_blacked', 'camera_normal'],
}
// person_detected 是心跳式观察，不进阶段事件列表；人员聚集是真实信号，保留
const BASE_TYPES = ['crowd_gathering']

function CameraTile({ cam, label }: { cam: string; label: string }) {
  const ref = useRef<HTMLVideoElement>(null)
  const hlsRef = useRef<Hls | null>(null)
  const [failed, setFailed] = useState(false)
  const [info, setInfo] = useState('')
  useEffect(() => {
    const timer = window.setInterval(() => {
      const video = ref.current
      const hls = hlsRef.current
      const size = video && video.videoWidth ? `${video.videoWidth}×${video.videoHeight}` : ''
      const delay = hls && Number.isFinite(hls.latency) ? `${hls.latency.toFixed(1)}s` : ''
      const parts = [size, delay ? `延迟 ${delay}` : '', '解析中'].filter(Boolean)
      setInfo(parts.join(' · '))
    }, 3000)
    return () => window.clearInterval(timer)
  }, [])
  useEffect(() => {
    const video = ref.current
    if (!video) return
    setFailed(false)
    // Chromium 对 canPlayType 返回 "maybe" 但实际放不了 HLS，优先走 hls.js
    if (!Hls.isSupported()) {
      video.src = `/hls/${cam}/index.m3u8`
      return
    }
    const hls = new Hls({ lowLatencyMode: false, backBufferLength: 10, maxBufferLength: 10 })
    hlsRef.current = hls
    hls.loadSource(`/hls/${cam}/index.m3u8`)
    hls.attachMedia(video)
    hls.on(Hls.Events.ERROR, (_evt, data) => { if (data.fatal) { setFailed(true); hls.destroy() } })
    return () => hls.destroy()
  }, [cam])
  return <div className="sc-tile">
    <div className="sc-tile-head"><VideoCameraOutlined /><span>{label}</span><em>{cam}</em></div>
    {failed ? <div className="sc-tile-fail">画面未就绪</div>
      : <video ref={ref} muted autoPlay playsInline />}
    {info && !failed ? <div className="sc-tile-info">{info}</div> : null}
  </div>
}

function durationText(from: string | null, to?: string | null) {
  if (!from) return '—'
  const start = new Date(from).getTime()
  const end = to ? new Date(to).getTime() : Date.now()
  const seconds = Math.max(0, Math.round((end - start) / 1000))
  if (seconds >= 86400) return `${Math.floor(seconds / 86400)} 天 ${Math.floor((seconds % 86400) / 3600)} 小时`
  if (seconds >= 3600) return `${Math.floor(seconds / 3600)} 小时 ${String(Math.floor((seconds % 3600) / 60)).padStart(2, '0')} 分`
  const mm = String(Math.floor(seconds / 60)).padStart(2, '0')
  const ss = String(seconds % 60).padStart(2, '0')
  return `${mm}:${ss}`
}

export default function StageConsole() {
  const navigate = useNavigate()
  const [summary, setSummary] = useState<StageSummary | null>(null)
  const [config, setConfig] = useState<StageConfig | null>(null)
  const [events, setEvents] = useState<EventRow[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [risk, setRisk] = useState<'all' | 'high' | 'medium'>('all')
  const [tick, setTick] = useState(0)
  const [ops, setOps] = useState<OpsHealth | null>(null)
  // 投屏模式：隐藏平台导航和顶栏，整屏只留看板，用于投到评标室大屏
  const [kiosk, setKiosk] = useState(false)
  useEffect(() => {
    document.body.classList.toggle('sc-kiosk', kiosk)
    return () => document.body.classList.remove('sc-kiosk')
  }, [kiosk])

  async function load() {
    setLoading(true)
    try {
      const data = await fetchStageSummary()
      setSummary(data)
      fetchOpsHealth().then(setOps).catch(() => setOps(null))
      if (data.session?.session_id) {
        fetchStageConfig(data.session.session_id).then(setConfig).catch(() => setConfig(null))
      }
      // 事件按 events.phase_id 归集（落库时由生效快照打标），不用时间窗估算
      const phaseId = data.session?.current_phase
      const response = data.session?.session_id ? await authFetch(`/api/events?limit=200&session_id=${encodeURIComponent(data.session.session_id)}${phaseId ? `&phase_id=${encodeURIComponent(phaseId)}` : ''}`, { cache: 'no-store' }) : null
      if (response?.ok) {
        const rows = await response.json()
        setEvents(Array.isArray(rows) ? rows : [])
      } else setEvents([])
    } catch {
      message.error('阶段数据读取失败，请确认已登录')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [])
  // 每 30 秒自动拉一次，事件与计数不用手点刷新也能跟上
  useEffect(() => {
    const timer = window.setInterval(() => { setTick((v) => v + 1); void load() }, 30000)
    return () => window.clearInterval(timer)
  }, [])

  const currentIndex = useMemo(() => {
    const phase = summary?.session?.current_phase
    const index = phase ? PHASE_IDS.indexOf(phase as PhaseId) : -1
    return index >= 0 ? index : 0
  }, [summary])

  const live = summary?.phases?.[currentIndex]
  const stage = STAGE_SPEC[currentIndex]
  const hasScenes = Boolean(stage?.scenes?.length)

  const startRaw = summary?.session?.start_at
  const sessionStart = startRaw
    ? new Date(/[zZ]$|[+-]\d\d:?\d\d$/.test(startRaw) ? startRaw : `${startRaw}Z`).getTime()
    : 0
  const allowedTypes = useMemo(() => new Set([
    ...BASE_TYPES,
    ...(stage?.scenes || []).flatMap((scene) => SCENE_TYPES[scene] || []),
  ]), [stage])
  const phaseEvents = useMemo(() => events
    // 只算本场次开始之后的事件，避免把上一场的同阶段历史记录带进来
    .filter((item) => !sessionStart || !item.occurred_at || new Date(item.occurred_at).getTime() >= sessionStart)
    // 只保留本阶段启用的场景对应的事件类型
    .filter((item) => allowedTypes.has(item.event_type))
    .filter((item) => risk === 'all' || item.risk_level === risk)
    .sort((a, b) => new Date(b.occurred_at || 0).getTime() - new Date(a.occurred_at || 0).getTime()),
  [events, risk, sessionStart, allowedTypes])

  const cams = stage?.cams || []

  async function shift(delta: number) {
    const sessionId = summary?.session?.session_id
    if (!sessionId || busy) return
    const next = currentIndex + delta
    if (next < 0 || next >= PHASE_IDS.length) { message.info('已经是第一个/最后一个阶段'); return }
    setBusy(true)
    try {
      const latest = config ?? await fetchStageConfig(sessionId)
      const target = PHASE_IDS[next]
      // S10（见证/断流）是常开场景，后端要求每个启用场景都有规则版本，缺一个就整单拒绝
      const alwaysOn = latest.always_on_scenes?.length ? latest.always_on_scenes : ['S10']
      const scenes = Array.from(new Set([...(STAGE_SPEC[next]?.scenes || []), ...alwaysOn]))
      const rules: Record<string, string> = {}
      scenes.forEach((scene) => { rules[scene] = latest.rule_versions?.[scene] || `${scene.toLowerCase()}-v1` })
      await saveStageConfig(sessionId, {
        phase: target,
        expected_version: latest.phase_version,
        enabled_scenes: scenes,
        rule_versions: rules,
        reason: `演示推进：${PHASE_TITLES[PHASE_IDS[currentIndex]]} → ${PHASE_TITLES[target]}`,
        assets: latest.assets,
        exceptions: latest.exceptions,
      })
      message.success(`已进入「${PHASE_TITLES[target]}」`)
      await load()
    } catch (error) {
      message.error(error instanceof Error ? error.message : '阶段切换失败，请到阶段配置页检查')
    } finally {
      setBusy(false)
    }
  }

  const conclusion = `本场次「${summary?.session?.name || '未选择'}」当前处于「${live ? PHASE_TITLES[PHASE_IDS[currentIndex]] : '—'}」，` +
    `本阶段事件 ${phaseEvents.length} 起（高 ${live?.high ?? 0} / 中 ${live?.medium ?? 0} / 低 ${live?.low ?? 0}），` +
    `全程累计 ${summary?.total.events ?? 0} 起（高风险 ${summary?.total.high ?? 0} 起）。`

  return <div className="stage-console" data-tick={tick}>
    <header className="sc-topbar">
      <div className="sc-topbar-title"><AuditOutlined /><div>
        <strong>{summary?.session?.name || '未选择场次'}</strong>
        <span>{summary?.session ? `场次状态 ${summary.session.status}` : '请在阶段配置中选择场次'}</span>
      </div></div>
      <div className="sc-topbar-center">
        <div className="sc-now">
          <em>当前阶段</em>
          <strong>{live ? PHASE_TITLES[PHASE_IDS[currentIndex]] : '—'}</strong>
        </div>
        <div className="sc-clock"><ClockCircleOutlined /> 已持续 {durationText(live?.entered_at || null)}</div>
        <div className="sc-total">全程 {summary?.total.events ?? 0} 起 · 高风险 <b>{summary?.total.high ?? 0}</b></div>
      </div>
      <div className="sc-topbar-actions">
        <Button size="small" icon={<ArrowLeftOutlined />} disabled={busy || currentIndex === 0} onClick={() => void shift(-1)}>上一阶段</Button>
        <Button size="small" type="primary" icon={<ArrowRightOutlined />} loading={busy} disabled={currentIndex >= PHASE_IDS.length - 1} onClick={() => void shift(1)}>下一阶段</Button>
        <Button size="small" icon={<SettingOutlined />} onClick={() => navigate('/stages/console')}>阶段配置</Button>
        <Button size="small" icon={<ReloadOutlined />} onClick={() => void load()}>刷新</Button>
        <Button size="small" type={kiosk ? 'primary' : 'default'}
          icon={kiosk ? <FullscreenExitOutlined /> : <FullscreenOutlined />}
          onClick={() => setKiosk((value) => !value)}>{kiosk ? '退出投屏' : '投屏模式'}</Button>
      </div>
    </header>

    <div className="sc-projectbar">
      <span className="sc-project-name">{summary?.project?.name || summary?.session?.name || '未选择项目'}</span>
      <span className="sc-project-item"><em>项目编号</em>{summary?.project?.code || '—'}</span>
      <span className="sc-project-item"><em>招标人</em>{summary?.project?.owner || '—'}</span>
      <span className="sc-project-item"><em>评标室</em>{summary?.project?.room_name || '—'}</span>
      <span className="sc-project-item"><em>评标专家</em>{summary?.session?.expected_people ?? '—'} 人</span>
      <span className="sc-project-item"><em>已评时长</em>{durationText(summary?.session?.start_at || null)}</span>
      <span className="sc-project-item"><em>阶段进度</em>{currentIndex + 1}/{PHASE_IDS.length}</span>
    </div>
    <div className="sc-healthbar">
      <span className={`sc-health-dot ${ops?.ai_engine === 'online' ? 'is-ok' : ops?.ai_engine === 'offline' ? 'is-bad' : 'is-warn'}`} />
      <em>AI 引擎</em>
      <b>{ops ? ({online:'在线',idle:'空闲',offline:'离线',unknown:'待核验'}[ops.ai_engine]) : '—'}</b>
      <span className={`sc-health-dot ${ops && ops.streams_ready === ops.streams_total ? 'is-ok' : 'is-warn'}`} />
      <em>视频流</em>
      <b>{ops ? `${ops.streams_ready}/${ops.streams_total} 正常` : '—'}</b>
      <span className={`sc-health-dot ${(ops?.pending ?? 0) > 0 ? 'is-warn' : 'is-ok'}`} />
      <em>全局未闭环</em>
      <b>{ops?.pending ?? '—'}</b>
      <span className="sc-health-time">检查于 {ops?.checked_at || '—'}</span>
    </div>
    {loading && !summary ? <div className="sc-loading"><Spin tip="正在读取阶段数据…" /></div> : <div className="sc-body">
      <aside className="sc-rail">
        <div className="sc-rail-head">评标六阶段</div>
        <ol>
          {(summary?.phases?.length ? summary.phases : STAGE_SPEC.map((item) => ({
            phase_id: item.id, title: PHASE_TITLES[item.id], state: 'pending' as const,
            event_count: 0, high: 0, medium: 0, low: 0, entered_at: null, left_at: null,
            version: null, scenes: item.scenes, rule_versions: {}, reason: null, actor: null,
          }))).map((phase, index) => {
            const state = phase.state
            const isCurrent = state === 'current'
            return <li key={phase.phase_id}
              className={`sc-step ${state === 'done' ? 'is-done' : ''} ${isCurrent ? 'is-current' : ''}`}>
              <div className="sc-step-dot">{state === 'done' ? <CheckCircleFilled /> : <span>{index + 1}</span>}</div>
              <div className="sc-step-body">
                <div className="sc-step-title">
                  <strong>{phase.title}</strong>
                  {isCurrent && <Tag color="processing">进行中</Tag>}
                  {state === 'done' && <em>{durationText(phase.entered_at, phase.left_at)}</em>}
                </div>
                <div className="sc-step-meta">
                  <span>累计 {phase.event_count}</span>
                  {phase.high > 0 && <span className="is-high">高 {phase.high}</span>}
                  {phase.medium > 0 && <span className="is-medium">中 {phase.medium}</span>}
                  {phase.entered_at && <span>{formatTime(phase.entered_at)}{phase.left_at ? `–${formatTime(phase.left_at)}` : ' 起'}</span>}
                </div>
              </div>
            </li>
          })}
        </ol>
      </aside>

      <main className="sc-main">
        <section className="sc-hero">
          <div className="sc-hero-head">
            <div>
              <span className="sc-hero-no">阶段 {stage?.no || String(currentIndex + 1).padStart(2, '0')}</span>
              <h2>{live ? PHASE_TITLES[PHASE_IDS[currentIndex]] : '未开始'}</h2>
            </div>
            <div className="sc-hero-tags">
              {(stage?.scenes || []).map((scene) => <Tag key={scene} color="blue">{scene}</Tag>)}
              {config && <Tag color={config.sync_status === 'applied' ? 'green' : 'orange'}>v{config.phase_version} · {config.sync_status}</Tag>}
            </div>
          </div>
          <p className="sc-hero-action">{stage?.action || '选择阶段后展示该阶段的业务动作。'}</p>
          <div className="sc-funnel">
            <div className="sc-funnel-cell">
              <em>已抓拍</em>
              <b>{live?.event_count ?? 0}</b>
              <span>高风险 {live?.high ?? 0} · 中风险 {live?.medium ?? 0}</span>
            </div>
            <div className="sc-funnel-arrow" />
            <div className="sc-funnel-cell is-warn">
              <em>待人工确认</em>
              <b>{live?.pending ?? 0}</b>
              <span>处置队列</span>
            </div>
            <div className="sc-funnel-arrow" />
            <div className="sc-funnel-cell is-ok">
              <em>已确认处置</em>
              <b>{live?.confirmed ?? 0}</b>
              <span>误报 {live?.false_positive ?? 0}</span>
            </div>
            <div className="sc-funnel-arrow" />
            <div className="sc-funnel-cell is-done">
              <em>已闭环</em>
              <b>{live?.closed ?? 0}</b>
              <span>已持续 {durationText(live?.entered_at || null)}</span>
            </div>
          </div>

          <div className="sc-rules">
            <div className="sc-rules-col is-must">
              <div className="sc-rules-head">本阶段该有</div>
              <ul>{(stage?.must || []).map((item) => <li key={item}><CheckCircleFilled />{item}</li>)}</ul>
            </div>
            <div className="sc-rules-col is-must-not">
              <div className="sc-rules-head">本阶段不该有</div>
              <ul>{(stage?.mustNot || []).map((item) => <li key={item}><CloseCircleFilled />{item}</li>)}</ul>
            </div>
          </div>
        </section>

        <section className="sc-events">
          <div className="sc-events-head">
            <strong>本阶段事件（仅 "{stage?.scenes?.join('/') || '—'}" 场景）{phaseEvents.length ? <em style={{ fontStyle: 'normal', color: '#7d94ac', marginLeft: 8, fontSize: 12 }}>最近 200 条内 {phaseEvents.length} 起</em> : null}</strong>
            <Segmented size="small" value={risk} onChange={(value) => setRisk(value as 'all' | 'high' | 'medium')}
              options={[{ label: '全部', value: 'all' }, { label: '高风险', value: 'high' }, { label: '中风险', value: 'medium' }]} />
          </div>
          {!hasScenes ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span style={{ color: "#9fb6cb" }}>该阶段不设现场监测场景（业务记录阶段），事件不参与归集</span>} />
            : phaseEvents.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span style={{ color: "#9fb6cb" }}>本阶段暂无事件 —— 画面在跑、规则在等</span>} />
            : <div className="sc-event-list">
              {phaseEvents.slice(0, 40).map((item) => <button key={item.event_id} className="sc-event"
                onClick={() => navigate(`/event-detail?event=${item.event_id}`)}>
                {item.thumb
                  ? <img className="sc-event-thumb" src={item.thumb} alt="" loading="lazy" />
                  : <span className="sc-event-thumb is-empty" />}
                <span className="sc-event-time">{formatTime(item.occurred_at)}</span>
                <span className="sc-event-type">{eventTypeLabels[item.event_type] || item.event_type}</span>
                <span className="sc-event-risk" style={{ color: riskColor[item.risk_level] }}>{riskText[item.risk_level] || item.risk_level}</span>
                <span className="sc-event-cam">{item.camera_id || '—'}</span>
                <span className="sc-event-reason">{item.reason}</span>
              </button>)}
            </div>}
        </section>
      </main>

      <aside className="sc-side">
        <div className="sc-side-block">
          <div className="sc-side-head">本阶段机位</div>
          {cams.length ? cams.map((item) => <CameraTile key={item.cam} cam={item.cam} label={item.label} />)
            : <p className="sc-side-empty">该阶段未绑定机位（业务型阶段）</p>}
        </div>
        <div className="sc-side-block">
          <div className="sc-side-head">阶段结论</div>
          <p className="sc-conclusion">{conclusion}</p>
          <Button size="small" icon={<ExportOutlined />} onClick={() => {
            void navigator.clipboard?.writeText(conclusion).then(() => message.success('阶段小结已复制'))
          }}>复制阶段小结</Button>
        </div>
        <div className="sc-side-block">
          <div className="sc-side-head">最近事件时间</div>
          <ul className="sc-side-list">
            {phaseEvents.slice(0, 5).map((item) => <li key={item.event_id}>
              <em>{formatDateTime(item.occurred_at)}</em>
              <span>{eventTypeLabels[item.event_type] || item.event_type}</span>
            </li>)}
            {!phaseEvents.length && <li><em>—</em><span>暂无</span></li>}
          </ul>
        </div>
      </aside>
    </div>}
  </div>
}
