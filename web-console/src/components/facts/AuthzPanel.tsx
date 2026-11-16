// S04授权快照面板（6.3）：从6.1事实+6.2核验关联生成房间授权快照，供语义研判消费。
import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Card, Empty, Input, Select, Table, Tag } from 'antd'
import { generateAuthzSnapshot, listAuthzSnapshots } from '../../api/facts'
import type { AuthzSnapshot } from '../../api/facts'
import { formatDateTime } from '../../utils/ui'

export default function AuthzPanel({ projectId, roomId }: { projectId: string | undefined; roomId: string | undefined }) {
  const [snapshots, setSnapshots] = useState<AuthzSnapshot[]>([])
  const [visibility, setVisibility] = useState<'public' | 'confidential'>('public')
  const [policyId, setPolicyId] = useState('')
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  const reload = useCallback(async () => {
    if (!roomId) { setSnapshots([]); return }
    setLoading(true)
    try {
      const data = await listAuthzSnapshots(roomId)
      setSnapshots(data.items)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '加载授权快照失败')
    } finally { setLoading(false) }
  }, [roomId])

  useEffect(() => { void reload() }, [reload])

  const generate = async () => {
    if (!projectId || !roomId) return
    setBusy(true); setError(''); setMessage('')
    try {
      const snapshot = await generateAuthzSnapshot({
        project_id: projectId, room_id: roomId, phase_id: 'ATTENDANCE',
        information_visibility: visibility,
        confidentiality_policy_id: visibility === 'confidential' ? (policyId.trim() || undefined) : undefined,
      })
      setMessage(`快照已生成：${snapshot.entries.length} 人在册（${snapshot.entries.filter((entry) => entry.identity_verified).length} 人身份已核验），旧快照已自动置为已取代`)
      await reload()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '生成快照失败，请重试')
    } finally { setBusy(false) }
  }

  return <Card className="facts-card" title="授权快照（S04 → 语义研判上下文）">
    <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'center', marginBottom: 12 }}>
      <Select value={visibility} onChange={setVisibility} style={{ width: 200 }}
        options={[{ value: 'public', label: '信息公开级别：公开' }, { value: 'confidential', label: '信息公开级别：保密' }]} />
      {visibility === 'confidential' && <Input value={policyId} onChange={(event) => setPolicyId(event.target.value)}
        placeholder="保密依据编号（必填，如 policy-demo-001）" style={{ width: 300 }} />}
      <Button type="primary" loading={busy} disabled={!projectId || !roomId} onClick={() => void generate()}>生成授权快照</Button>
      <span style={{ color: '#7891ac', fontSize: 11 }}>在场人员由签到/换人事实推导，身份核验状态由人脸核验关联推导</span>
    </div>
    {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
    {message && <Alert type="success" showIcon message={message} style={{ marginBottom: 12 }} />}
    {snapshots.length ? <Table rowKey="snapshot_id" size="small" loading={loading} dataSource={snapshots}
      columns={[
        { title: '生成时间', dataIndex: 'created_at', width: 150, render: (value: string) => formatDateTime(value) },
        { title: '状态', dataIndex: 'status', width: 90, render: (value: string) => <Tag color={value === 'active' ? 'green' : 'default'}>{value === 'active' ? '当前有效' : '已取代'}</Tag> },
        { title: '信息级别', dataIndex: 'information_visibility', width: 90, render: (value: string) => <Tag color={value === 'confidential' ? 'red' : 'blue'}>{value === 'confidential' ? '保密' : '公开'}</Tag> },
        { title: '在册人员', render: (_: unknown, record: AuthzSnapshot) => record.entries.map((entry) => (
          <Tag key={entry.entry_id} color={entry.identity_verified ? 'green' : 'default'}>
            {entry.person_name}（{entry.role}）{entry.identity_verified ? ' · 已核验' : ' · 未核验'}{entry.recipient_authorized ? ' · 可接收保密' : ''}
          </Tag>)) },
        { title: '登记人', dataIndex: 'created_by', width: 80 },
      ]}
      pagination={{ pageSize: 5, showSizeChanger: false }} />
      : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={loading ? '正在加载…' : '该评标室尚无授权快照，点击上方按钮生成'} />}
  </Card>
}
