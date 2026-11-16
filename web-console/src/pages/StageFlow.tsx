import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { ArrowLeftOutlined, ArrowRightOutlined, AuditOutlined, SettingOutlined } from '@ant-design/icons'
import { Button, Tag } from 'antd'
import { STAGES } from '../components/stageflow/stages'
import { fetchStageSummary } from '../api/stageSummary'
import type { StageSummary } from '../api/stageSummary'
import '../stage-flow.css'

export default function StageFlow() {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const [summary, setSummary] = useState<StageSummary | null>(null)

  useEffect(() => {
    let alive = true
    fetchStageSummary()
      .then((data) => { if (alive) setSummary(data) })
      .catch(() => { /* 未登录或接口不可用时只展示流程说明 */ })
    return () => { alive = false }
  }, [])
  const raw = Number(params.get('step'))
  const step = Number.isInteger(raw) && raw >= 1 && raw <= STAGES.length ? raw : 1
  const current = STAGES[step - 1]
  const live = summary?.phases[step - 1]
  const stateText = live?.state === 'current' ? '进行中' : live?.state === 'done' ? '已完成' : '未开始'
  const go = (next: number) => {
    const target = Math.min(STAGES.length, Math.max(1, next))
    setParams({ step: String(target) })
  }

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'ArrowRight') go(step + 1)
      if (event.key === 'ArrowLeft') go(step - 1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  return <div className="stage-flow">
    <header className="stage-flow-bar">
      <div className="stage-flow-bar-title"><AuditOutlined /><span>评标流程</span></div>
      <div className="stage-flow-dots">
        {STAGES.map((stage, index) => <button key={stage.no} type="button"
          className={`stage-dot ${index + 1 === step ? 'is-current' : ''} ${index + 1 < step ? 'is-done' : ''}`}
          onClick={() => go(index + 1)} title={`第 ${index + 1} 阶段 · ${stage.title}`}>
          <span>{index + 1}</span>
        </button>)}
      </div>
      <div className="stage-flow-bar-actions">
        <Button size="small" type="primary" ghost onClick={() => navigate('/show/1')}>进入展示模式</Button>
        <Button size="small" icon={<SettingOutlined />} onClick={() => navigate('/stages/console')}>阶段配置</Button>
      </div>
    </header>

    {summary?.session ? <div className="stage-flow-session">
      <span className="stage-flow-session-name">{summary.session.name}</span>
      <span className="stage-flow-session-item">当前 · {summary.session.current_title}</span>
      <span className="stage-flow-session-item">累计 {summary.total.events} 起事件</span>
      {summary.total.high > 0 ? <span className="stage-flow-session-risk">高风险 {summary.total.high}</span> : null}
    </div> : null}

    <article className="stage-panel" key={current.no}>
      <div className="stage-panel-meta">
        <span className="stage-panel-count">第 {step} 阶段 / 共 {STAGES.length} 个</span>
        {live ? <span className={`stage-panel-state is-${live.state}`}>{stateText}</span> : null}
        {live && live.event_count > 0
          ? <span className="stage-panel-events">事件 {live.event_count} 起
            {live.high > 0 ? <em>高风险 {live.high}</em> : null}</span>
          : null}
        <div className="stage-panel-scenes">
          {current.scenes.map(scene => <Tag key={scene} bordered={false} className="stage-scene-tag">{scene}</Tag>)}
        </div>
      </div>

      <h1 className="stage-panel-title"><span className="stage-panel-no">{current.no}</span>{current.title}</h1>

      <section className="stage-panel-block">
        <h2>业务动作</h2>
        <p>{current.action}</p>
      </section>

      <section className="stage-panel-block stage-panel-focus">
        <h2>这一步看什么</h2>
        <p>{current.focus}</p>
      </section>

      <section className="stage-panel-body">{current.block}</section>

      <section className="stage-panel-links">
        <h2>跳到对应页面</h2>
        <div className="stage-panel-link-list">
          {current.links.map((link, index) => <Button key={link.to} type={index === 0 ? 'primary' : 'default'}
            onClick={() => navigate(link.to)}>
            {link.label}<ArrowRightOutlined />
          </Button>)}
        </div>
      </section>
    </article>

    <footer className="stage-flow-nav">
      <Button icon={<ArrowLeftOutlined />} disabled={step <= 1} onClick={() => go(step - 1)}>上一阶段</Button>
      <span className="stage-flow-hint">← → 方向键也可切换</span>
      <Button type="primary" disabled={step >= STAGES.length} onClick={() => go(step + 1)}>
        下一阶段<ArrowRightOutlined />
      </Button>
    </footer>
  </div>
}
