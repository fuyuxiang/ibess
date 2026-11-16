// S04业务事实登记面板（6.1）：签到/换人/离岗许可/澄清事实的登记与查询。
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Button, Card, Empty, Form, Input, Select, Table, Tag, message } from 'antd'
import { Link } from 'react-router-dom'
import { autoLinkFacts, createFact, deleteFact, factTypeLabels, linkFact, listFactLinks, listLinkCandidates, listFacts } from '../../api/facts'
import type { AttendanceFact, FactType, FactVerificationLink } from '../../api/facts'
import { useRegistry } from '../../store/useRegistryStore'
import { activeRooms, roomLabel } from '../../utils/registry'
import { formatDateTime } from '../../utils/ui'
import AuthzPanel from './AuthzPanel'
import './facts-panel.css'

const factColor: Record<FactType, string> = {
  check_in: 'green', check_out: 'default', change: 'orange', leave_permit: 'blue', clarification: 'purple',
}

export default function FactsPanel() {
  const registry = useRegistry()
  const rooms = useMemo(() => activeRooms(registry.data), [registry.data])
  const [roomId, setRoomId] = useState<string | undefined>(rooms[0]?.room_id)
  const room = rooms.find((item) => item.room_id === roomId)
  const participants = useMemo(() => {
    if (!registry.data || !room) return []
    return registry.data.persons.filter((person) => registry.data?.participants.some((item) => item.project_id === room.project_id && item.person_id === person.person_id && person.enabled))
  }, [registry.data, room])
  const seats = useMemo(() => (registry.data?.seats || []).filter((seat) => seat.room_id === roomId && seat.enabled), [registry.data, roomId])
  const [factType, setFactType] = useState<FactType>('check_in')
  const [personId, setPersonId] = useState<string | undefined>()
  const [replacedPersonId, setReplacedPersonId] = useState<string | undefined>()
  const [seatId, setSeatId] = useState<string | undefined>()
  const [phaseId, setPhaseId] = useState<string | undefined>()
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [facts, setFacts] = useState<AttendanceFact[]>([])
  const [filterType, setFilterType] = useState<string>('')
  const [filterPerson, setFilterPerson] = useState<string>('')
  const [links, setLinks] = useState<Record<string, FactVerificationLink>>({})
  const [loading, setLoading] = useState(false)
  const [linkBusy, setLinkBusy] = useState(false)
  const [messageApi, messageHolder] = message.useMessage()

  const reload = useCallback(async () => {
    if (!room) { setFacts([]); return }
    setLoading(true)
    try {
      const [data, linkData] = await Promise.all([
        listFacts(room.project_id, { room_id: room.room_id, fact_type: filterType || undefined, person_id: filterPerson || undefined }),
        listFactLinks(room.project_id).catch(() => ({ links: {} as Record<string, FactVerificationLink> })),
      ])
      setFacts(data.items)
      setLinks(linkData.links)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '加载业务事实失败')
    } finally { setLoading(false) }
  }, [room, filterType, filterPerson])

  useEffect(() => { void registry.refresh(true) }, [registry.refresh])
  useEffect(() => { if (!roomId && rooms.length) setRoomId(rooms[0].room_id) }, [roomId, rooms])
  useEffect(() => { setPersonId(undefined); setSeatId(undefined); setReplacedPersonId(undefined); setNote('') }, [roomId])
  // 房间切换时自动加载；筛选条件由「查询」按钮触发
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { void reload() }, [room])
  useEffect(() => { setReplacedPersonId(undefined) }, [factType])

  const submit = async () => {
    if (!room) return
    setBusy(true); setError('')
    try {
      await createFact({
        fact_type: factType, project_id: room.project_id, room_id: room.room_id,
        person_id: personId, seat_id: seatId, phase_id: phaseId,
        note: note.trim() || undefined,
        payload: { ...(factType === 'change' && replacedPersonId ? { replaced_person_id: replacedPersonId } : {}), ...(submitMode === 'supplement' ? { supplement: 'true' } : {}) },
      })
      setNote('')
      await reload()
      void message.success(submitMode === 'supplement' ? '业务事实已补录' : '业务事实已登记')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '登记失败，请重试')
    } finally { setBusy(false) }
  }

  const autoLink = async () => {
    if (!room) return
    setLinkBusy(true); setError('')
    try {
      const outcome = await autoLinkFacts(room.project_id)
      messageApi.success(`已关联 ${outcome.linked} 条事实；${outcome.skipped_no_result} 条无可用核验记录`)
      await reload()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '自动关联失败，请重试')
    } finally { setLinkBusy(false) }
  }

  const linkOne = async (factId: string) => {
    setError('')
    try {
      const candidates = await listLinkCandidates(factId)
      const best = candidates.items.find((item: { status: string }) => item.status === "matched") || candidates.items[0]
      if (!best) { messageApi.warning('该人员暂无人脸核验记录'); return }
      await linkFact(factId, best.result_id)
      await reload()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '关联失败，请重试')
    }
  }

  const remove = async (factId: string) => {
    try { await deleteFact(factId); await reload() } catch (cause) { setError(cause instanceof Error ? cause.message : '删除失败') }
  }

  const personOptions = participants.map((person) => ({ value: person.person_id, label: `${person.name}（${person.role}）` }))
  const [submitMode, setSubmitMode] = useState<'register' | 'supplement'>('register')

  return <div className="facts-panel">
    {messageHolder}
    <Card className="facts-card" title="业务事实登记（S04）" extra={<Link to="/registry">基础管理配置 →</Link>}>
      <Form layout="vertical" onFinish={() => void submit()}>
        <div className="facts-form-grid">
          <Form.Item label="评标室" required>
            <Select value={roomId} disabled={!rooms.length} showSearch optionFilterProp="label" onChange={setRoomId}
              options={rooms.map((item) => ({ value: item.room_id, label: roomLabel(item, registry.data) }))} placeholder="请先在基础管理配置评标室" />
          </Form.Item>
          <Form.Item label="事实类型" required>
            <Select value={factType} onChange={setFactType} options={Object.entries(factTypeLabels).map(([value, label]) => ({ value, label }))} />
          </Form.Item>
          <Form.Item label={factType === 'change' ? '新人员（递补）' : '人员'}>
            <Select value={personId} allowClear showSearch optionFilterProp="label" onChange={setPersonId} disabled={factType === 'leave_permit'}
              options={personOptions} placeholder={participants.length ? '选择项目成员' : '请先配置项目成员'} />
          </Form.Item>
          <Form.Item label="工位">
            <Select value={seatId} allowClear onChange={setSeatId} options={seats.map((seat) => ({ value: seat.seat_id, label: seat.code }))} placeholder="可选" />
          </Form.Item>
          <Form.Item label="阶段">
            <Select value={phaseId} allowClear onChange={setPhaseId} placeholder="可选"
              options={['OPEN_BID', 'ATTENDANCE', 'RECUSAL_CHANGE', 'PRE_REVIEW', 'DETAILED', 'CLARIFICATION', 'SCORE_CHECK', 'REPORT', 'OFFLINE'].map((item) => ({ value: item, label: item }))} />
          </Form.Item>
          {factType === 'change' && <Form.Item label="被替换人员" required>
            <Select value={replacedPersonId} allowClear showSearch optionFilterProp="label" onChange={setReplacedPersonId} options={personOptions} placeholder="选择离场人员" />
          </Form.Item>}
        </div>
        <Form.Item label="说明" required={factType === 'leave_permit' || factType === 'clarification'}>
          <Input.TextArea value={note} onChange={(event) => setNote(event.target.value)} rows={2} maxLength={500} showCount
            placeholder={factType === 'leave_permit' ? '例：临时离岗10分钟，已许可' : factType === 'clarification' ? '例：就交付周期提出澄清并已答复' : '可选备注'} />
        </Form.Item>
        {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <Button type="primary" htmlType="submit" loading={busy} disabled={!room} onClick={() => setSubmitMode('register')}>登记事实</Button>
          <Button htmlType="submit" loading={busy} disabled={!room} onClick={() => setSubmitMode('supplement')}>补录事实</Button>
          <span style={{ color: '#7891ac', fontSize: 11 }}>补录用于事后补登，记录会标记「补录」</span>
        </div>
        <p style={{ color: '#7891ac', fontSize: 11, margin: '10px 0 0' }}>
          事实登记为演示用途人工录入；6.3 授权快照将自动引用这些事实生成语义研判的业务上下文。
        </p>
      </Form>
    </Card>
    <Card className="facts-card" title={`事实查询${room ? ` · ${roomLabel(room, registry.data)}` : ''}`} extra={<div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}><Select size="small" allowClear placeholder="类型" style={{ width: 110 }} value={filterType || undefined} onChange={(value) => setFilterType(value || '')} options={Object.entries(factTypeLabels).map(([value, label]) => ({ value, label }))} /><Select size="small" allowClear placeholder="人员" style={{ width: 130 }} value={filterPerson || undefined} onChange={(value) => setFilterPerson(value || '')} options={personOptions} /><Button size="small" type="primary" onClick={() => void reload()}>查询</Button><Button size="small" type="link" onClick={() => { setFilterType(''); setFilterPerson('') }}>重置</Button><Button size="small" loading={linkBusy} disabled={!room} onClick={() => void autoLink()}>一键关联人脸核验</Button><Link to="/history">历史查询 →</Link></div>}>
      <Table rowKey="fact_id" size="small" loading={loading} dataSource={facts}
        columns={[
          { title: '时间', dataIndex: 'occurred_at', width: 150, render: (value: string) => formatDateTime(value) },
          { title: '类型', dataIndex: 'fact_type', width: 100, render: (value: FactType) => <Tag color={factColor[value]}>{factTypeLabels[value]}</Tag> },
          { title: '人员', width: 130, render: (_: unknown, record: AttendanceFact) => record.person_name || '—' },
          { title: '工位', dataIndex: 'seat_code', width: 80, render: (value: string | null) => value || '—' },
          { title: '阶段', dataIndex: 'phase_id', width: 120, render: (value: string | null) => value || '—' },
          { title: '说明', ellipsis: true, render: (_: unknown, record: AttendanceFact) => <>{record.payload?.supplement === 'true' ? <Tag color="orange" style={{ marginRight: 6 }}>补录</Tag> : null}{record.fact_type === 'change' && record.replaced_person_name ? `${record.person_name || '新人员'} 递补 ${record.replaced_person_name}` : (record.note || '—')}</> },
          { title: '人脸核验', width: 130, render: (_: unknown, record: AttendanceFact) => {
            const link = links[record.fact_id]
            if (link) return <Tag color={link.status === 'matched' ? 'green' : 'orange'}>{link.status === 'matched' ? `本人核验 ${((link.similarity ?? 0) * 100).toFixed(0)}%` : '核验未通过'}</Tag>
            if (!record.person_id) return <span style={{ color: '#a5b8ca' }}>无人员</span>
            return <Button size="small" type="link" onClick={() => void linkOne(record.fact_id)}>关联最近核验</Button>
          } },
          { title: '操作', width: 70, render: (_: unknown, record: AttendanceFact) => <Button size="small" type="link" onClick={() => void remove(record.fact_id)}>撤销</Button> },
        ]}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={registry.loading ? '正在加载配置…' : '该评标室暂无业务事实'} /> }}
        pagination={{ pageSize: 8, showSizeChanger: false }} />
    </Card>
    <AuthzPanel projectId={room?.project_id} roomId={roomId} />
  </div>
}
