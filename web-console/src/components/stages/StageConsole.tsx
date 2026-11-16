import { useEffect, useRef, useState } from 'react'
import { AutoComplete, Alert, Button, Card, Checkbox, Empty, Input, Modal, Select, Table, Tag } from 'antd'
import { ArrowLeftOutlined, ArrowRightOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { createEvaluationSession } from '../../api/client'
import { fetchRegistry } from '../../api/registry'
import type { RegistrySnapshot } from '../../api/registry'
import { bindStageSession, changeStage, fetchStageSessions, PHASE_TITLES, saveStageConfig, StageApiError } from '../../api/stages'
import type { PhaseId, StageSession, StageWrite } from '../../api/stages'
import { usePhaseStore } from '../../store/usePhaseStore'
import PhaseBadge from './PhaseBadge'
import '../../stage-console.css'

const stages = Object.entries(PHASE_TITLES) as [PhaseId, string][]
const scenes = ['S01', 'S02', 'S03', 'S04', 'S05', 'S06', 'S07', 'S08', 'S09']
const labels: Record<string, string> = { S01: '入口设备', S02: '设备使用', S03: '离岗越界', S04: '身份授权', S05: '跨区接触', S06: '倾向性发言', S07: '保密信息', S08: '摄录与资料', S09: '现场秩序', S10: '见证完整性' }
const suggestions: Record<PhaseId, string[]> = { SIGNIN: ['S01', 'S04'], DISCIPLINE: ['S01', 'S02'], PRELIM: ['S02', 'S05'], DETAIL: ['S03', 'S05', 'S07', 'S08'], CLARIFY: ['S02', 'S06'], REPORT: ['S06', 'S07', 'S10'] }
const summaries: Record<PhaseId, string> = {
  SIGNIN: '核对到场签到、身份核验、通讯工具寄存，无关人员不得进入评标区。',
  DISCIPLINE: '全体在场听取纪律宣读，承诺书签署、组长推选与回避确认过程留痕。',
  PRELIM: '按形式、资格、响应性三项审查，记录否决投标情形与算术错误修正。',
  DETAIL: '技术与商务分项独立评审打分，评分依据留痕，不得协商评分。',
  CLARIFY: '澄清事项集中提出、统一记录，未定事项由组长拍板，不得改变投标文件实质。',
  REPORT: '汇总评审结果，代理机构出具评标报告并现场宣读，确认无违规行为。' }

const errorMessage = (error: unknown) => error instanceof Error ? error.message : '操作失败，请重试'
const timeText = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false })

interface Draft { phase: PhaseId; initial: boolean; version: number; reason: string; enabled: string[]; versions: Record<string, string>; project: string; room: string; source: string }

export default function StageConsole() {
  const state = usePhaseStore()
  const [sessions, setSessions] = useState<StageSession[]>([])
  const [registry, setRegistry] = useState<RegistrySnapshot | null>(null)
  const [listLoading, setListLoading] = useState(true)
  const [listError, setListError] = useState('')
  const [notice, setNotice] = useState('')
  const [draft, setDraft] = useState<Draft | null>(null)
  const [formError, setFormError] = useState('')
  const [busy, setBusy] = useState(false)
  const [bindingSaved, setBindingSaved] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [name, setName] = useState('')
  const mounted = useRef(false)
  const writeLock = useRef(false)
  const loadEpoch = useRef(0)
  const listRequest = useRef<AbortController | null>(null)
  const writeRequest = useRef<AbortController | null>(null)
  const sceneDrafts = useRef<Partial<Record<PhaseId, string[]>>>({})
  const selected = sessions.find(s => s.session_id === state.selectedSessionId)
  const config = state.config
  const index = config ? stages.findIndex(([id]) => id === config.phase_id) : -1
  const closed = selected?.status === 'closed'

  // 规则版本候选：已登记版本 + 已知规则包版本（可下拉选择，也可手动输入）
  const SOURCE_OPTIONS = [
    { value: 'camera-002', label: 'camera-002 · 人员聚集' },
    { value: 'camera-007', label: 'camera-007 · 离席判定' },
    { value: 'camera-010', label: 'camera-010 · S02 疑似通话' },
    { value: 'camera-011', label: 'camera-011 · S08 疑似拍摄' },
    { value: 'camera-012', label: 'camera-012 · S01 入口未寄存' },
    { value: 'audio-001', label: 'audio-001 · 评标室音频源' },
  ].map((item) => ({ value: item.value, label: item.label }))

  const KNOWN_VERSIONS: Record<string, string[]> = {
    S01: ['s01-v1'], S02: ['s02-v1'], S03: ['s03-v1', 's03-v3-v1'], S04: ['s04-v1', 's04-v04-v1'],
    S05: ['s05-v1', 's05-v05-v1'], S06: ['s06-v1', 's06-v3', 's06-v4', 's06-v5'],
    S07: ['s07-v1', 's07-v2', 's07-v3'], S08: ['s08-v1', 's08-v08-v1'], S09: ['s09-v1', 's09-v09-v1'],
    S10: ['s10-v1'],
  }
  function versionOptions(scene: string): { value: string }[] {
    const collected = new Set<string>()
    const registered = config?.rule_versions?.[scene]
    if (registered) collected.add(registered)
    for (const item of KNOWN_VERSIONS[scene] || []) collected.add(item)
    collected.add(`${scene.toLowerCase()}-v1`)
    return [...collected].filter(Boolean).map((value) => ({ value }))
  }

  async function loadLists() {
    const epoch = ++loadEpoch.current
    listRequest.current?.abort()
    const controller = new AbortController(); listRequest.current = controller
    setListLoading(true); setListError('')
    const [list, business] = await Promise.allSettled([fetchStageSessions(controller.signal), fetchRegistry()])
    if (!mounted.current || epoch !== loadEpoch.current) return
    if (list.status === 'fulfilled') setSessions(list.value)
    else if (!controller.signal.aborted) setListError(errorMessage(list.reason))
    if (business.status === 'fulfilled') setRegistry(business.value)
    else setListError(previous => previous || '项目与评标室配置加载失败，请刷新')
    setListLoading(false)
  }
  useEffect(() => {
    mounted.current = true
    void loadLists()
    return () => { mounted.current = false; loadEpoch.current++; listRequest.current?.abort(); writeRequest.current?.abort(); void usePhaseStore.getState().selectSession(null) }
  }, [])

  function begin(phase: PhaseId, initial = false) {
    if (closed || busy || state.loading || (!initial && !config) || !state.selectedSessionId) return
    setNotice(''); setFormError('')
    setBindingSaved(false)
    sceneDrafts.current = config ? { [config.phase_id]: config.enabled_scenes.filter(s => s !== 'S10') } : {}
    setDraft({ phase, initial, version: config?.phase_version || 0, reason: '',
      enabled: initial || config?.phase_id !== phase ? suggestions[phase] : config.enabled_scenes.filter(s => s !== 'S10'),
      versions: { ...config?.rule_versions }, project: config?.project_id || '', room: config?.room_id || '', source: config?.source_id || '' })
  }
  async function save() {
    if (!draft || writeLock.current || !state.selectedSessionId || closed) return
    if (!draft.reason.trim()) { setFormError('请填写切换或配置原因'); return }
    const required = [...draft.enabled, 'S10']
    if (required.some(scene => !draft.versions[scene]?.trim())) { setFormError('请填写每个启用场景的规则版本'); return }
    if (draft.initial && (!draft.project || !draft.room || !draft.source.trim())) { setFormError('请选择项目、评标室并填写源编号'); return }
    const sessionId = state.selectedSessionId
    const controller = new AbortController(); writeRequest.current = controller
    writeLock.current = true; setBusy(true); setFormError('')
    const body: StageWrite = { phase: draft.phase, expected_version: draft.version, enabled_scenes: draft.enabled,
      rule_versions: Object.fromEntries(required.map(scene => [scene, draft.versions[scene].trim()])), reason: draft.reason.trim(),
      assets: !draft.initial && config?.phase_id === draft.phase ? config.assets : [],
      exceptions: !draft.initial && config?.phase_id === draft.phase ? config.exceptions : [] }
    try {
      if (draft.initial && !bindingSaved) {
        await bindStageSession(sessionId, { project_id: draft.project, room_id: draft.room, source_id: draft.source.trim() }, controller.signal)
        if (!mounted.current || controller.signal.aborted) return
        setBindingSaved(true)
      }
      await (draft.initial ? saveStageConfig : changeStage)(sessionId, body, controller.signal)
      if (!mounted.current || usePhaseStore.getState().selectedSessionId !== sessionId) return
      setDraft(null); setNotice('配置已保存，应用状态请查看阶段状态标签')
      await usePhaseStore.getState().refresh()
      if (mounted.current) setSessions(rows => rows.map(row => row.session_id === sessionId ? { ...row, current_phase: body.phase } : row))
    } catch (error) {
      if (!mounted.current || controller.signal.aborted) return
      if (error instanceof StageApiError && error.code === 'SESSION_CLOSED') {
        setDraft(null); setNotice('场次已关闭，配置未保存')
        setSessions(rows => rows.map(row => row.session_id === sessionId ? { ...row, status: 'closed' } : row))
        await Promise.all([loadLists(), usePhaseStore.getState().refresh()])
      } else if (error instanceof StageApiError && error.code === 'VERSION_CONFLICT') {
        setDraft(null); setNotice('配置已被更新，请确认最新版本后重新操作'); await usePhaseStore.getState().refresh()
      } else setFormError(errorMessage(error))
    } finally { writeLock.current = false; if (mounted.current) setBusy(false) }
  }
  async function createSession() {
    if (writeLock.current) return
    if (!name.trim()) { setFormError('请填写场次名称'); return }
    writeLock.current = true; setBusy(true); setFormError('')
    try {
      const created = await createEvaluationSession({ name: name.trim(), current_phase: 'review' })
      if (!mounted.current) return
      setSessions(rows => [created, ...rows]); setCreateOpen(false); setName('')
      await usePhaseStore.getState().selectSession(created.session_id)
    } catch (error) { if (mounted.current) setFormError(errorMessage(error)) }
    finally { writeLock.current = false; if (mounted.current) setBusy(false) }
  }

  return <div className="stage-workbench">
    <Card className="stage-console-card">
      <div className="stage-console-heading"><div><span className="stage-console-kicker">场次与阶段配置</span><h2>评标流程控制</h2><p>明确选择场次，按版本保存配置，核对消费者应用状态。</p></div>
        <div className="stage-toolbar"><Select aria-label="选择评标场次" placeholder="请选择评标场次" value={state.selectedSessionId || undefined}
          loading={listLoading} disabled={busy} allowClear options={sessions.map(s => ({ value: s.session_id, label: s.name }))}
          onChange={id => { setDraft(null); setNotice(''); void state.selectSession(id || null) }} />
          <Button aria-label="新建场次" icon={<PlusOutlined />} disabled={busy} onClick={() => { setFormError(''); setCreateOpen(true) }}>新建场次</Button>
          <Button aria-label="刷新阶段" icon={<ReloadOutlined />} loading={listLoading || state.loading} disabled={busy} onClick={() => { void loadLists(); void state.refresh() }}>刷新阶段</Button></div></div>
      {listError && <Alert showIcon type="error" message={listError} />}
      {notice && <Alert showIcon type="info" message={notice} />}
      {closed && <Alert showIcon type="warning" message="场次已关闭，仅可查看配置与历史" />}
      <div className="stage-status-row"><PhaseBadge config={config} control={state.control} loading={state.loading}
        needsConfiguration={state.needsConfiguration} error={state.error || (config ? state.controlError : null)} />
        {config && <span className="stage-scope">场次：{selected?.name || config.session_id} · 源：{config.source_id || '场次级配置'}</span>}</div>
      {!state.selectedSessionId ? <Empty description="请先选择评标场次；打开页面不会创建或修改业务数据" />
        : state.needsConfiguration ? <div className="stage-initial"><h3>为当前场次配置初始阶段</h3><p>明确项目、评标室和视频或音频源，再选择阶段与规则版本。</p>
          <Button type="primary" disabled={closed || state.loading} onClick={() => begin('SIGNIN', true)}>配置初始阶段</Button></div>
        : !config && !state.loading ? <Alert type="error" showIcon message={state.error || '阶段配置暂不可用，请刷新'} /> : null}
      <div className="stage-track" aria-label="六阶段导航">{stages.map(([id, title], position) => <button type="button" key={id}
        className={`stage-node ${config?.phase_id === id ? 'is-current' : ''}`} aria-current={config?.phase_id === id ? 'step' : undefined}
        disabled={!config || closed || busy || state.loading} onClick={() => begin(id)}><span>{String(position + 1).padStart(2, '0')}</span><b>{title}</b></button>)}</div>
      {config && <div className="stage-console-detail"><div className="stage-current"><Tag color="blue">阶段 {index + 1} / 7</Tag><h3>{PHASE_TITLES[config.phase_id]}</h3><p>{summaries[config.phase_id]}</p>
        <div className="stage-scene-tags">{Array.from(new Set([...config.enabled_scenes, ...config.always_on_scenes])).map(scene => <Tag key={scene}>{scene} {labels[scene]} · {config.rule_versions[scene] || '版本缺失'}</Tag>)}</div>
        <div className="stage-console-actions"><Button aria-label="上一阶段" icon={<ArrowLeftOutlined />} disabled={index <= 0 || closed || busy || state.loading} onClick={() => begin(stages[index - 1][0])}>上一阶段</Button>
          <Button aria-label="进入下一阶段" type="primary" icon={<ArrowRightOutlined />} disabled={index < 0 || index === 6 || closed || busy || state.loading} onClick={() => begin(stages[index + 1][0])}>进入下一阶段</Button>
          <Button disabled={closed || busy || state.loading} onClick={() => begin(config.phase_id)}>编辑当前配置</Button></div></div>
        <aside className="stage-output"><span>素材与见证完整性</span><strong>{config.assets.length ? `已登记 ${config.assets.length} 项阶段素材` : '当前阶段未登记素材'}</strong>
          {config.assets.map(asset => <small key={`${asset.slot}:${asset.source_id}`}>{asset.slot} · {asset.asset_id}</small>)}
          <small>媒体播放状态未接入</small><small>S10 在采集任务全程启用，阶段切换不关闭见证缺口监测。</small>
          <small>配置生效时间：{timeText(config.effective_at)}</small></aside></div>}
    </Card>
    <Card title="阶段配置时间轴" className="stage-history-card" extra={<span>保留版本、操作人和变更原因</span>}>
      {state.timelineError && <Alert type="warning" showIcon message={state.timelineError} />}
      <Table rowKey="stage_snapshot_id" size="small" pagination={false} dataSource={state.timeline.slice().reverse()} locale={{ emptyText: state.selectedSessionId ? '暂无阶段配置历史' : '请选择场次查看历史' }}
        columns={[{ title: '版本', dataIndex: 'phase_version', width: 76, render: value => `v${value}` },
          { title: '阶段', dataIndex: 'phase_id', width: 120, render: (id: PhaseId) => PHASE_TITLES[id] || id },
          { title: '生效时间', dataIndex: 'effective_at', width: 190, render: timeText },
          { title: '操作人', dataIndex: 'actor_id', width: 130 }, { title: '变更原因', dataIndex: 'reason' }]} />
    </Card>
    <Modal open={!!draft} title={draft?.initial ? '配置初始阶段' : '确认阶段配置'} okText="确认保存配置" cancelText="取消" confirmLoading={busy}
      maskClosable={false} closable={!busy} cancelButtonProps={{ disabled: busy }} onCancel={() => setDraft(null)} onOk={() => void save()} width={720}>
      {draft && <div className="stage-config-form">
        <p>保存基于 v{draft.version}，生成新版本；消费者确认后才显示已应用。</p>
        {formError && <Alert type="error" showIcon message={formError} />}
        <label>目标阶段<Select aria-label="选择目标阶段" value={draft.phase} disabled={busy} options={stages.map(([value, label]) => ({ value, label }))}
          onChange={phase => {
            sceneDrafts.current[draft.phase] = [...draft.enabled]
            setDraft({ ...draft, phase, enabled: [...(sceneDrafts.current[phase] || suggestions[phase])] })
          }} /></label>
        {draft.initial && <div className="stage-binding-fields"><label>项目<Select aria-label="选择项目" value={draft.project || undefined} disabled={busy || bindingSaved}
          options={registry?.projects.filter(p => p.status !== 'closed').map(p => ({ value: p.project_id, label: p.name }))}
          onChange={project => setDraft({ ...draft, project, room: '' })} /></label>
          <label>评标室<Select aria-label="选择评标室" value={draft.room || undefined} disabled={busy || bindingSaved || !draft.project}
            options={registry?.rooms.filter(r => r.project_id === draft.project && r.enabled).map(r => ({ value: r.room_id, label: r.name }))}
            onChange={room => setDraft({ ...draft, room })} /></label>
          <label className="stage-full-field">采集源编号（该阶段生效的视频摄像头 / 音频源）<AutoComplete aria-label="视频或音频源编号" style={{ width: '100%' }} disabled={busy || bindingSaved}
          value={draft.source} placeholder="选择采集源，如 camera-002（人员聚集摄像头）"
          options={SOURCE_OPTIONS} filterOption={(input, option) => String(option?.value ?? '').toLowerCase().includes(input.toLowerCase())}
          onChange={(value: string) => setDraft({ ...draft, source: value })} />
          <small style={{ color: '#7891ac' }}>摄像头为 camera-001~012（详情见视频监测页），音频源为 audio-001；也可输入其他已登记源编号。</small></label></div>}
        <div><b>启用识别场景</b><Checkbox.Group disabled={busy} options={scenes.map(value => ({ value, label: `${value} ${labels[value]}` }))}
          value={draft.enabled} onChange={enabled => setDraft({ ...draft, enabled: enabled as string[] })} /></div>
        <div className="stage-s10-note">S10 见证完整性 · 全程开启</div>
        <div className="stage-rule-fields">{[...draft.enabled, 'S10'].map(scene => <label key={scene}>{scene} 规则版本<AutoComplete aria-label={`${scene}规则版本`} disabled={busy} style={{ width: '100%' }}
          options={versionOptions(scene)} value={draft.versions[scene] || ''} placeholder="选择或输入已登记规则版本"
          filterOption={(input, option) => String(option?.value ?? '').toLowerCase().includes(input.toLowerCase())}
          onChange={(value: string) => setDraft({ ...draft, versions: { ...draft.versions, [scene]: value } })} /></label>)}</div>
        <label>切换或配置原因<Input.TextArea aria-label="阶段切换原因" value={draft.reason} disabled={busy} maxLength={1024} rows={3} onChange={e => setDraft({ ...draft, reason: e.target.value })} /></label>
        <small>{draft.initial ? bindingSaved ? '业务绑定已保存并锁定，本次重试只保存阶段配置。' : '初始配置先保存业务绑定；若配置失败，绑定会保留，可使用同一绑定重试。' : '切换到其他阶段不沿用前一阶段素材或个人例外；素材适配和授权事实需另行登记。'}</small>
      </div>}
    </Modal>
    <Modal open={createOpen} title="新建评标场次" okText="创建场次" cancelText="取消" confirmLoading={busy} maskClosable={false} closable={!busy}
      cancelButtonProps={{ disabled: busy }} onCancel={() => setCreateOpen(false)} onOk={() => void createSession()}>
      {formError && <Alert type="error" message={formError} />}
      <p>仅创建草稿场次，随后明确业务绑定和初始阶段。</p><Input aria-label="新建场次名称" value={name} maxLength={128} disabled={busy} onChange={e => setName(e.target.value)} />
    </Modal>
  </div>
}
