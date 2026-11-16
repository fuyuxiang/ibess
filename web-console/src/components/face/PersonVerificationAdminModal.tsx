import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Card, Input, Modal, Popconfirm, Select, Space, Table, Tag } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { CopyOutlined, LinkOutlined, ReloadOutlined, UserOutlined } from '@ant-design/icons'
import { createVerificationLink, fetchVerificationLinks, fetchVerificationProfile, fetchVerificationResults, revokeVerificationLink } from '../../api/participantVerification'
import type { CreateVerificationLinkInput, LinkInfo, VerificationFaceProfile, VerificationResult } from '../../api/participantVerification'
import type { RegistryParticipant, RegistryPerson, RegistryProject, RegistryRoom } from '../../api/registry'
import '../../person-verification.css'

export interface PersonVerificationAdminModalProps {
  person: RegistryPerson | null; projects: RegistryProject[]; rooms: RegistryRoom[]
  participants: RegistryParticipant[]; onClose: () => void
}
const linkLabels: Record<LinkInfo['status'], string> = { active: '有效', completed: '已完成', expired: '已过期', revoked: '已撤销', exhausted: '次数用尽', invalidated: '绑定已失效' }
const resultLabels: Record<VerificationResult['status'], string> = { matched: '匹配通过', suspect: '疑似（需复核）', unmatched: '未匹配', invalid: '图片不合格' }
function dateText(value: string): string { return new Date(value).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' }) }
function errorText(cause: unknown): string { return cause instanceof TypeError ? '无法连接后台，请检查网络后刷新。' : cause instanceof Error ? cause.message : '操作失败，请刷新重试。' }
function scoreText(value: number | null): string { return value !== null && Number.isFinite(value) ? value.toFixed(4) : '—' }

function AdminVerificationSession({ person, projects, rooms, participants }: Omit<PersonVerificationAdminModalProps, 'person' | 'onClose'> & { person: RegistryPerson }) {
  const [profile, setProfile] = useState<VerificationFaceProfile | null>(null)
  const [links, setLinks] = useState<LinkInfo[]>([])
  const [results, setResults] = useState<VerificationResult[]>([])
  const [projectId, setProjectId] = useState<string | null>(null)
  const [roomId, setRoomId] = useState<string | null>(null)
  const [minutes, setMinutes] = useState<CreateVerificationLinkInput['expires_in_minutes']>(60)
  const [freshLink, setFreshLink] = useState<{ id: string; url: string } | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [copyNotice, setCopyNotice] = useState('')
  const alive = useRef(true)
  const generation = useRef(0)
  const lock = useRef(false)
  const controllerRef = useRef<AbortController | null>(null)
  const memberProjectIds = useMemo(() => new Set(participants.filter((member) => member.person_id === person.person_id).map((member) => member.project_id)), [participants, person.person_id])
  const eligibleProjects = projects.filter((project) => memberProjectIds.has(project.project_id))
  const eligibleRooms = rooms.filter((room) => room.project_id === projectId && room.enabled)
  const validProject = projectId === null || eligibleProjects.some((project) => project.project_id === projectId)
  const validRoom = roomId === null || eligibleRooms.some((room) => room.room_id === roomId)

  const load = useCallback(async (clearLink = true) => {
    if (lock.current) return
    lock.current = true
    const ticket = ++generation.current
    const controller = new AbortController(); controllerRef.current = controller
    const timer = window.setTimeout(() => controller.abort(), 30000)
    const current = () => alive.current && ticket === generation.current
    if (clearLink) { setFreshLink(null); setCopyNotice('') }
    setLoading(true); setError('')
    try {
      const reads = await Promise.allSettled([
        fetchVerificationProfile(person.person_id, controller.signal),
        fetchVerificationLinks(person.person_id, controller.signal),
        fetchVerificationResults(person.person_id, controller.signal),
      ])
      if (!current()) return
      const errors: string[] = []
      const [faceRead, linkRead, resultRead] = reads
      if (faceRead.status === 'fulfilled' && faceRead.value.person_id === person.person_id) setProfile(faceRead.value)
      else { setProfile(null); errors.push(faceRead.status === 'rejected' ? `登记状态：${errorText(faceRead.reason)}` : '登记人员编号不符') }
      if (linkRead.status === 'fulfilled') setLinks(linkRead.value.items)
      else { setLinks([]); errors.push(`链接列表：${errorText(linkRead.reason)}`) }
      if (resultRead.status === 'fulfilled') setResults(resultRead.value.items)
      else { setResults([]); errors.push(`结果列表：${errorText(resultRead.reason)}`) }
      setError(controller.signal.aborted ? '读取超时，请刷新核对。' : errors.join('；'))
    } catch (cause) { if (current()) { setProfile(null); setError(errorText(cause)) } }
    finally { window.clearTimeout(timer); if (current()) { lock.current = false; setLoading(false) }; if (controllerRef.current === controller) controllerRef.current = null }
  }, [person.person_id])
  useEffect(() => {
    alive.current = true; void load()
    return () => { alive.current = false; generation.current += 1; lock.current = false; controllerRef.current?.abort() }
  }, [load])
  useEffect(() => {
    if (!validProject) { setProjectId(null); setRoomId(null) }
    else if (!validRoom) setRoomId(null)
  }, [validProject, validRoom])

  async function mutate(action: 'create' | 'revoke', id?: string) {
    if (lock.current || action === 'create' && (!person.enabled || !profile?.registered || !validProject || !validRoom || error)) return
    lock.current = true
    const ticket = ++generation.current
    const current = () => alive.current && ticket === generation.current
    const controller = new AbortController(); controllerRef.current = controller
    const timer = window.setTimeout(() => controller.abort(), 30000)
    setBusy(action === 'create' ? 'create' : id!); setError(''); setCopyNotice(''); setFreshLink(null)
    try {
      if (action === 'create') {
        const created = await createVerificationLink(person.person_id, { expires_in_minutes: minutes, project_id: projectId, room_id: roomId }, controller.signal)
        if (!current()) return
        if (created.person_id !== person.person_id) throw new Error('返回链接对应的人员不符，请刷新列表核对。')
        setFreshLink({ id: created.link_id, url: created.verification_url })
      } else await revokeVerificationLink(id!, controller.signal)
    } catch (cause) { if (current()) setError(controller.signal.aborted ? '请求超时，可能已保存；请刷新列表核对后再操作。' : errorText(cause)) }
    finally {
      window.clearTimeout(timer)
      if (current()) {
        lock.current = false; setBusy(null)
        // 保存新链接只留内存；重新取列表，不重新获取或重建原始令牌。
        void refreshLists(ticket)
      }
      if (controllerRef.current === controller) controllerRef.current = null
    }
  }

  async function refreshLists(ticket: number) {
    if (!alive.current || ticket !== generation.current || lock.current) return
    lock.current = true; setLoading(true)
    const controller = new AbortController(); controllerRef.current = controller
    const timer = window.setTimeout(() => controller.abort(), 30000)
    try {
      const [nextLinks, nextResults] = await Promise.all([fetchVerificationLinks(person.person_id, controller.signal), fetchVerificationResults(person.person_id, controller.signal)])
      if (alive.current && ticket === generation.current) { setLinks(nextLinks.items); setResults(nextResults.items) }
    } catch (cause) { if (alive.current && ticket === generation.current) setError((previous) => [previous, `操作后列表刷新失败：${errorText(cause)}`].filter(Boolean).join('；')) }
    finally { window.clearTimeout(timer); if (alive.current && ticket === generation.current) { lock.current = false; setLoading(false) }; if (controllerRef.current === controller) controllerRef.current = null }
  }
  async function copyLink() {
    if (!freshLink) return
    const ticket = generation.current
    try {
      if (!navigator.clipboard?.writeText) throw new Error('当前浏览器不支持自动复制，请选择下方链接手动复制。')
      await navigator.clipboard.writeText(new URL(freshLink.url, window.location.origin).href)
      if (alive.current && ticket === generation.current) setCopyNotice('链接已复制，请仅发送给该人员。')
    } catch (cause) { if (alive.current && ticket === generation.current) setCopyNotice(errorText(cause)) }
  }

  const locked = loading || Boolean(busy)
  const linkColumns: ColumnsType<LinkInfo> = [
    { title: '创建 / 过期时间', width: 190, render: (_: unknown, row: LinkInfo) => <div className="pv-admin-times"><span>{dateText(row.created_at)}</span><small>至 {dateText(row.expires_at)}</small></div> },
    { title: '绑定项目 / 评标室', width: 220, render: (_: unknown, row: LinkInfo) => <span>{row.project_name || '仅绑定人员'}<br /><small>{row.room_name || '未绑定评标室'}</small></span> },
    { title: '状态', dataIndex: 'status', width: 105, render: (value: LinkInfo['status']) => <Tag color={value === 'active' ? 'blue' : value === 'completed' ? 'green' : 'default'}>{linkLabels[value] || value}</Tag> },
    { title: '提交次数', width: 100, render: (_: unknown, row: LinkInfo) => `${row.attempts} / ${row.max_attempts}` },
    { title: '操作', width: 95, render: (_: unknown, row: LinkInfo) => row.status === 'active' ? <Popconfirm title="确认撤销该核验链接？" description="人员将不能再使用该入口提交。" okText="确认撤销" cancelText="取消" onConfirm={() => void mutate('revoke', row.link_id)} disabled={locked}><Button size="small" danger disabled={locked} loading={busy === row.link_id}>撤销</Button></Popconfirm> : '—' },
  ]
  const resultColumns: ColumnsType<VerificationResult> = [
    { title: '核验时间', dataIndex: 'checked_at', width: 175, render: dateText },
    { title: '绑定项目 / 室', width: 180, render: (_: unknown, row: VerificationResult) => <span>{row.project_name || '未绑定项目'}<br /><small>{row.room_name || '未绑定评标室'}</small></span> },
    { title: '结果', dataIndex: 'status', width: 105, render: (value: VerificationResult['status']) => <Tag color={value === 'matched' ? 'green' : value === 'invalid' ? 'red' : 'orange'}>{resultLabels[value] || value}</Tag> },
    { title: '相似度 / 门槛', width: 120, render: (_: unknown, row: VerificationResult) => row.status === 'invalid' ? '—' : `${scoreText(row.similarity)} / ${scoreText(row.threshold)}` },
    { title: '错误说明', dataIndex: 'error', width: 210, render: (value: string | null) => value || '—' },
  ]
  return <div className="pv-admin-session"><div className="pv-admin-person"><UserOutlined /><div><strong>{person.name}</strong><span>{person.code} · {person.unit}</span></div><Button icon={<ReloadOutlined />} disabled={locked} onClick={() => void load()}>刷新链接与结果</Button></div>
    {error && <Alert className="pv-alert" type="error" showIcon message="操作或读取失败" description={error} />}
    {!person.enabled && <Alert className="pv-alert" type="warning" showIcon message="人员已停用，不能生成核验链接。" />}
    {profile && !profile.registered && <Alert className="pv-alert" type="warning" showIcon message="尚未登记底照" description="请先在该人员的“人脸匹配”中登记单人清晰底照，再生成限时核验链接。" />}
    <Card className="pv-admin-card" title="生成限时人员核验链接"><div className="pv-admin-form"><div><label>绑定项目（可选）</label><Select aria-label="绑定项目" value={projectId || ''} disabled={locked} onChange={(value) => { setProjectId(value || null); setRoomId(null) }} options={[{ value: '', label: '仅绑定人员' }, ...eligibleProjects.map((project) => ({ value: project.project_id, label: `${project.name}（${project.code}）` }))]} /></div><div><label>绑定评标室（可选）</label><Select aria-label="绑定评标室" value={roomId || ''} disabled={locked || !projectId} onChange={(value) => setRoomId(value || null)} options={[{ value: '', label: '不绑定评标室' }, ...eligibleRooms.map((room) => ({ value: room.room_id, label: `${room.name}（${room.code}）` }))]} /></div><div><label>有效期</label><Select aria-label="链接有效期" value={minutes} disabled={locked} onChange={setMinutes} options={[{ value: 15, label: '15 分钟' }, { value: 60, label: '60 分钟' }, { value: 240, label: '4 小时' }, { value: 1440, label: '24 小时' }]} /></div></div><p className="pv-helper">只列出该人员已加入的项目及其启用评标室。每个链接最多 5 次提交；重新生成会撤销该人员未完成的旧链接。</p><Button type="primary" icon={<LinkOutlined />} loading={busy === 'create'} disabled={locked || !person.enabled || !profile?.registered || !validProject || !validRoom || Boolean(error)} onClick={() => void mutate('create')}>生成核验链接</Button>
      {freshLink && <div className="pv-fresh-link"><Alert type="success" showIcon message="链接已生成，仅本次临时显示" description="刷新、关闭或切换人员后不再显示原始链接。链接赋予该人员核验权限，请勿公开传播。" /><Input aria-label="刚生成的核验链接" readOnly value={new URL(freshLink.url, window.location.origin).href} onFocus={(event) => event.target.select()} /><Space><Button icon={<CopyOutlined />} onClick={() => void copyLink()}>复制链接</Button><a href={freshLink.url} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">打开人员核验页</a><Button onClick={() => { setFreshLink(null); setCopyNotice('') }}>隐藏链接</Button></Space>{copyNotice && <p className="pv-helper" role="status">{copyNotice}</p>}</div>}
    </Card>
    <Card className="pv-admin-card" title="最近核验链接" extra="最多 20 条"><Table rowKey="link_id" loading={loading} size="small" columns={linkColumns} dataSource={links} scroll={{ x: 730 }} pagination={{ pageSize: 5, hideOnSinglePage: true, showSizeChanger: false }} locale={{ emptyText: '暂无核验链接' }} /></Card>
    <Card className="pv-admin-card" title="人员核验结果" extra="最多 30 条"><Table rowKey="result_id" loading={loading} size="small" columns={resultColumns} dataSource={results} scroll={{ x: 790 }} pagination={{ pageSize: 5, hideOnSinglePage: true, showSizeChanger: false }} locale={{ emptyText: '暂无人员核验结果' }} /><p className="pv-helper">相似度不是准确率，未做活体检测。业务上下文由后台绑定，不代表系统已识别实际位置。</p></Card>
  </div>
}

export default function PersonVerificationAdminModal({ person, onClose, ...data }: PersonVerificationAdminModalProps) {
  if (!person) return null
  return <Modal className="person-verification-admin-modal" title="人员核验 · 限时链接与结果" open footer={null} width={1040} onCancel={onClose}><AdminVerificationSession key={`${person.person_id}:${person.enabled}`} person={person} {...data} /></Modal>
}
