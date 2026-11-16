import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { FileSearchOutlined, PlayCircleOutlined, ReloadOutlined } from '@ant-design/icons'
import { Button, Card, Drawer, Empty, Image, Progress, Select, Space, Table, Tag, Timeline, message } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { createAudit, fetchAudit, fetchAuditSources, fetchAudits } from '../api/videoAudits'
import type { AuditFinding, AuditRun, AuditSource } from '../api/videoAudits'
import '../stage-console-page.css'

const STATUS_LABEL: Record<AuditRun['status'], string> = {
  queued: '排队中', running: '分析中', completed: '已完成', failed: '失败',
}
const STATUS_COLOR: Record<AuditRun['status'], string> = {
  queued: 'default', running: 'processing', completed: 'success', failed: 'error',
}
const RISK_COLOR: Record<string, string> = { high: 'red', medium: 'orange', low: 'blue' }
const RISK_LABEL: Record<string, string> = { high: '高', medium: '中', low: '低' }
const TYPE_LABEL: Record<string, string> = {
  crowd_gathering: '人员聚集',
  person_detected: '人员出现',
  phone_detected: '手机出现',
  camera_blacked: '画面遮挡',
  seat_absence: '异常离席',
}

function fmtTime(value: string | null) {
  if (!value) return '—'
  const d = new Date(value)
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

export default function VideoAudit() {
  const [sources, setSources] = useState<AuditSource[]>([])
  const [selected, setSelected] = useState<string>()
  const [runs, setRuns] = useState<AuditRun[]>([])
  const [detail, setDetail] = useState<(AuditRun & { findings: AuditFinding[] }) | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [loading, setLoading] = useState(false)
  const [messageApi, holder] = message.useMessage()
  const timer = useRef<number | null>(null)

  const loadRuns = useCallback(async () => {
    setLoading(true)
    try {
      const data = await fetchAudits()
      setRuns(data.items)
    } catch (error) {
      messageApi.error(error instanceof Error ? error.message : '任务列表加载失败')
    } finally {
      setLoading(false)
    }
  }, [messageApi])

  const loadDetail = useCallback(async (auditId: string) => {
    try {
      setDetail(await fetchAudit(auditId))
    } catch (error) {
      messageApi.error(error instanceof Error ? error.message : '详情加载失败')
    }
  }, [messageApi])

  useEffect(() => {
    fetchAuditSources()
      .then((data) => setSources(data.items))
      .catch(() => messageApi.error('素材清单加载失败'))
    void loadRuns()
  }, [loadRuns, messageApi])

  // 有任务在跑时才轮询，任务都停下就停，避免无意义请求
  useEffect(() => {
    const active = runs.some((r) => r.status === 'queued' || r.status === 'running')
    if (!active) {
      if (timer.current) { window.clearInterval(timer.current); timer.current = null }
      return
    }
    if (timer.current) return
    timer.current = window.setInterval(() => {
      void loadRuns()
      if (detail && (detail.status === 'queued' || detail.status === 'running')) {
        void loadDetail(detail.audit_id)
      }
    }, 3000)
    return () => {
      if (timer.current) { window.clearInterval(timer.current); timer.current = null }
    }
  }, [runs, detail, loadRuns, loadDetail])

  const submit = async () => {
    if (!selected) {
      messageApi.warning('先选一段素材')
      return
    }
    setSubmitting(true)
    try {
      const created = await createAudit(selected)
      messageApi.success('已提交审计，分析期间可继续操作')
      setSelected(undefined)
      await loadRuns()
      void loadDetail(created.audit_id)
    } catch (error) {
      messageApi.error(error instanceof Error ? error.message : '提交失败')
    } finally {
      setSubmitting(false)
    }
  }

  const columns: ColumnsType<AuditRun> = [
    { title: '素材', dataIndex: 'file_name', ellipsis: true },
    {
      title: '状态', dataIndex: 'status', width: 92,
      render: (status: AuditRun['status']) => <Tag color={STATUS_COLOR[status]}>{STATUS_LABEL[status]}</Tag>,
    },
    {
      title: '进度', dataIndex: 'progress', width: 130,
      render: (progress: number, row) => row.status === 'running'
        ? <Progress percent={progress} size="small" />
        : row.status === 'completed' ? <span style={{ color: '#389e0d' }}>100%</span> : '—',
    },
    {
      title: '发现', dataIndex: 'findings_count', width: 72,
      render: (count: number) => count ? <Tag color="orange">{count} 处</Tag> : <span style={{ color: '#999' }}>—</span>,
    },
    { title: '提交时间', dataIndex: 'created_at', width: 130, render: fmtTime },
    {
      title: '操作', width: 80,
      render: (_: unknown, row) => <Button type="link" size="small" onClick={() => void loadDetail(row.audit_id)}>查看</Button>,
    },
  ]

  const timelineItems = useMemo(() => (detail?.findings || []).map((f) => ({
    color: f.risk_level === 'high' ? 'red' : f.risk_level === 'medium' ? 'orange' : 'blue',
    children: (
      <div style={{ display: 'flex', gap: 12, alignItems: 'flex-start' }}>
        {f.snapshot_url
          ? <Image src={f.snapshot_url} width={96} height={54} style={{ objectFit: 'cover', borderRadius: 4 }} />
          : <div style={{ width: 96, height: 54, background: '#f5f5f5', borderRadius: 4 }} />}
        <div>
          <div style={{ marginBottom: 2 }}>
            <b style={{ marginRight: 8 }}>{f.at_sec.toFixed(1)}s</b>
            <Tag color="blue">{TYPE_LABEL[f.event_type] || f.event_type}</Tag>
            {f.risk_level && <Tag color={RISK_COLOR[f.risk_level]}>{RISK_LABEL[f.risk_level] || f.risk_level}风险</Tag>}
          </div>
          <div style={{ fontSize: 12, color: '#666' }}>{f.reason}</div>
        </div>
      </div>
    ),
  })), [detail])

  return (
    <div className="stage-page">
      {holder}
      <Card className="stage-page-heading">
        <div className="stage-page-title">
          <FileSearchOutlined />
          <div><h1>历史视频审计</h1><p>把一段录像完整过一遍，产出问题清单与关键截图。</p></div>
        </div>
        <Tag color="blue">离线分析</Tag>
      </Card>

      <Card size="small" title="发起审计" extra={<Button icon={<ReloadOutlined />} size="small" onClick={() => void loadRuns()} loading={loading}>刷新</Button>}>
        <Space wrap>
          <Select
            style={{ width: 460 }}
            placeholder="选择要审计的素材"
            value={selected}
            onChange={setSelected}
            showSearch
            optionFilterProp="label"
            options={sources.map((s) => ({
              value: s.file_name,
              label: `${s.file_name}（${s.size_mb} MB）`,
            }))}
          />
          <Button type="primary" icon={<PlayCircleOutlined />} loading={submitting} onClick={() => void submit()}>开始审计</Button>
        </Space>
        <p style={{ margin: '10px 0 0', fontSize: 12, color: '#888' }}>
          审计是离线任务，15 秒素材约 1~2 分钟完成；分析结果只进审计清单，不会混入实时告警。
        </p>
      </Card>

      <Card size="small" title="审计任务">
        <Table
          rowKey="audit_id"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={runs}
          pagination={false}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有审计任务" /> }}
        />
      </Card>

      {detail && (
        <Drawer
          open
          width={760}
          title={`审计发现 · ${detail.file_name}`}
          onClose={() => setDetail(null)}
          destroyOnHidden
          extra={<Tag color={STATUS_COLOR[detail.status]}>{STATUS_LABEL[detail.status]}</Tag>}
        >
          {detail.findings.length
            ? <Timeline items={timelineItems} />
            : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={detail.status === 'completed' ? '这段录像未发现异常' : '分析进行中，稍后刷新'} />}
        </Drawer>
      )}

      <div className="stage-page-note">
        判定口径：人员聚集 ≥ 4 人、画面遮挡持续 1 秒以上、出现手机。同类发现间隔 3 秒去重，避免同一动作刷屏。
      </div>
    </div>
  )
}
