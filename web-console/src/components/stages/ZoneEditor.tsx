import { useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Empty, Input, Select, Segmented, Spin, Tag } from 'antd'
import { DeleteOutlined, ReloadOutlined, UndoOutlined } from '@ant-design/icons'
import { fetchRegistry } from '../../api/registry'
import type { RegistrySeat } from '../../api/registry'
import { fetchZoneConfig, saveZoneConfig, ZoneApiError } from '../../api/zones'
import type { DrawnZone, EntryLine, ZoneConfig, ZoneConfigWrite, ZonePoint } from '../../api/zones'
import '../../zone-editor.css'

interface ZoneEditorProps {
  snapshotUrl: string | null
  config: ZoneConfig | null
  onSaved?: (config: ZoneConfig) => void
  seats?: RegistrySeat[]
}

type DrawingMode = 'seat' | 'restricted' | 'entry'
const initialConfig = (config: ZoneConfig | null): ZoneConfigWrite => ({
  config_version: config?.config_version ?? 0,
  zones: config?.zones ?? [],
  entry_lines: config?.entry_lines ?? [],
})
const errorText = (error: unknown) => error instanceof Error ? error.message : '区域配置操作失败，请重试'
const identifier = () => globalThis.crypto?.randomUUID?.() || `zone-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`

export default function ZoneEditor({ snapshotUrl, config, onSaved, seats: suppliedSeats }: ZoneEditorProps) {
  const [draft, setDraft] = useState<ZoneConfigWrite>(() => initialConfig(config))
  const [points, setPoints] = useState<ZonePoint[]>([])
  const [mode, setMode] = useState<DrawingMode>('seat')
  const [direction, setDirection] = useState<EntryLine['direction']>('both')
  const [name, setName] = useState('')
  const [seatId, setSeatId] = useState<string | undefined>()
  const [seats, setSeats] = useState<RegistrySeat[]>(suppliedSeats || [])
  const [seatError, setSeatError] = useState('')
  const [snapshotReady, setSnapshotReady] = useState(false)
  const [snapshotError, setSnapshotError] = useState(false)
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [dirty, setDirty] = useState(false)
  const request = useRef<AbortController | null>(null)
  const overlay = useRef<SVGSVGElement>(null)

  useEffect(() => {
    setDraft(initialConfig(config))
    setPoints([])
    setDirty(false)
    setError('')
  }, [config])

  useEffect(() => {
    setSnapshotReady(false)
    setSnapshotError(false)
  }, [snapshotUrl])

  useEffect(() => {
    if (suppliedSeats) {
      setSeats(suppliedSeats)
      return
    }
    if (!config) return
    let active = true
    void fetchRegistry().then(data => { if (active) { setSeats(data.seats); setSeatError('') } })
      .catch(error => { if (active) setSeatError(errorText(error)) })
    return () => { active = false }
  }, [config, suppliedSeats])

  useEffect(() => () => request.current?.abort(), [])

  const roomSeats = useMemo(() => seats.filter(seat => seat.room_id === config?.room_id && seat.enabled), [seats, config?.room_id])
  const canComplete = mode === 'entry' ? points.length === 2 : points.length >= 3 && (mode === 'restricted' || roomSeats.some(seat => seat.seat_id === seatId))

  function imagePoint(event: React.MouseEvent<SVGSVGElement>) {
    const rect = event.currentTarget.getBoundingClientRect()
    if (!rect.width || !rect.height) return null
    const x = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width))
    const y = Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height))
    return [Number(x.toFixed(6)), Number(y.toFixed(6))] as ZonePoint
  }

  function addPoint(event: React.MouseEvent<SVGSVGElement>) {
    if (!snapshotReady || saving || !config) return
    const point = imagePoint(event)
    if (!point) return
    setPoints(current => mode === 'entry' && current.length >= 2 ? [point] : [...current, point])
    setNotice('')
  }

  function completeShape() {
    if (!canComplete || !snapshotReady) return
    if (mode === 'entry') {
      const line: EntryLine = { line_id: identifier(), name: name.trim() || `出入口线${draft.entry_lines.length + 1}`,
        start: points[0], end: points[1], direction }
      setDraft(current => ({ ...current, entry_lines: [...current.entry_lines, line] }))
    } else {
      const zone: DrawnZone = { zone_id: identifier(), name: name.trim() || `${mode === 'seat' ? '工位' : '限制'}区域${draft.zones.length + 1}`,
        kind: mode, seat_id: mode === 'seat' ? seatId || null : null, polygon: points }
      setDraft(current => ({ ...current, zones: [...current.zones, zone] }))
      setName('')
    }
    setPoints([])
    setDirty(true)
    setError('')
  }

  function removeZone(id: string) {
    setDraft(current => ({ ...current, zones: current.zones.filter(zone => zone.zone_id !== id) }))
    setDirty(true)
  }
  function removeLine(id: string) {
    setDraft(current => ({ ...current, entry_lines: current.entry_lines.filter(line => line.line_id !== id) }))
    setDirty(true)
  }

  async function refresh() {
    if (!config || loading || saving) return
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    setLoading(true)
    setError('')
    try {
      const latest = await fetchZoneConfig(config.session_id, config.source_id, controller.signal)
      if (controller.signal.aborted) return
      setDraft(initialConfig(latest))
      setPoints([])
      setDirty(false)
      setNotice(`已刷新到区域配置 v${latest.config_version}`)
    } catch (cause) {
      if (!controller.signal.aborted) setError(errorText(cause))
    } finally {
      if (request.current === controller) request.current = null
      setLoading(false)
    }
  }

  async function save() {
    if (!config || !snapshotReady || !dirty || points.length || saving || !draft.zones.every(zone => zone.kind !== 'seat' || roomSeats.some(seat => seat.seat_id === zone.seat_id))) return
    setSaving(true)
    setError('')
    setNotice('')
    try {
      const result = await saveZoneConfig(config.session_id, config.source_id, draft)
      setDraft(initialConfig(result))
      setPoints([])
      setDirty(false)
      setNotice(`区域配置已保存，版本 v${result.config_version}`)
      onSaved?.(result)
    } catch (cause) {
      if (cause instanceof ZoneApiError && cause.code === 'VERSION_CONFLICT') setError('配置已被其他操作更新，请刷新后核对再保存')
      else setError(errorText(cause))
    } finally { setSaving(false) }
  }

  return <section className="zone-editor" aria-label="画面区域标注">
    <header className="zone-editor-heading">
      <div><span>区域与出入口配置</span><h2>画面区域标注</h2><p>区域按当前视频源保存，坐标使用画面归一化比例。</p></div>
      <div className="zone-editor-actions">
        <Tag>配置 v{draft.config_version}</Tag>
        <Button icon={<ReloadOutlined />} loading={loading} disabled={!config || saving} onClick={() => void refresh()}>刷新</Button>
        <Button type="primary" disabled={!snapshotReady || !config || !dirty || !!points.length || saving || draft.zones.some(zone => zone.kind === 'seat' && !roomSeats.some(seat => seat.seat_id === zone.seat_id))}
          loading={saving} onClick={() => void save()}>保存区域配置</Button>
      </div>
    </header>
    {error && <Alert type="error" showIcon message={error} />}
    {notice && <Alert type="success" showIcon message={notice} />}
    {!config ? <Empty description="请先选择已绑定评标室和视频源的场次" /> : <div className="zone-editor-grid">
      <div className="zone-canvas-column">
        <div className="zone-image-frame" data-snapshot-state={snapshotReady ? 'ready' : snapshotUrl ? 'loading' : 'missing'}>
          {snapshotUrl && !snapshotError ? <>
            {!snapshotReady && <div className="zone-image-loading"><Spin size="small" />正在加载画面快照</div>}
            <img className="zone-snapshot" src={snapshotUrl} alt="视频源静态画面快照"
              onLoad={() => { setSnapshotReady(true); setSnapshotError(false) }}
              onError={() => { setSnapshotReady(false); setSnapshotError(true) }} />
            <svg ref={overlay} className={`zone-overlay ${snapshotReady && !saving ? 'is-drawable' : ''}`} viewBox="0 0 1 1" preserveAspectRatio="none"
              role="img" aria-label="区域标注画布" onClick={addPoint}>
              {draft.zones.map(zone => <g key={zone.zone_id} className={`zone-shape zone-shape-${zone.kind}`}>
                <polygon points={zone.polygon.map(point => point.join(',')).join(' ')} />
                <text x={zone.polygon.reduce((sum, point) => sum + point[0], 0) / zone.polygon.length}
                  y={zone.polygon.reduce((sum, point) => sum + point[1], 0) / zone.polygon.length}>{zone.name}</text>
              </g>)}
              {draft.entry_lines.map(line => <g key={line.line_id} className="zone-entry-line"><line x1={line.start[0]} y1={line.start[1]} x2={line.end[0]} y2={line.end[1]} /><text x={(line.start[0] + line.end[0]) / 2} y={(line.start[1] + line.end[1]) / 2}>{line.name}</text></g>)}
              {points.length > 1 && <polyline className="zone-draft-shape" points={points.map(point => point.join(',')).join(' ')} />}
              {points.map((point, index) => <circle className="zone-draft-point" key={`${point[0]}-${point[1]}-${index}`} cx={point[0]} cy={point[1]} r="0.008" />)}
            </svg>
          </> : <div className="zone-image-empty"><span>{snapshotError ? '快照加载失败' : '未提供画面快照'}</span><small>请先获取此视频源的静态快照，未加载画面时不能保存。</small></div>}
        </div>
        <div className="zone-canvas-caption"><span>{snapshotReady ? '静态快照已加载' : '区域保存需先加载静态快照'}</span><span>{config.source_id}</span></div>
        {seatError && <Alert type="warning" showIcon message="工位列表加载失败" description={seatError} />}
      </div>
      <aside className="zone-tool-panel">
        <div className="zone-tool-heading"><b>绘制工具</b><Tag color={snapshotReady ? 'green' : 'default'}>{snapshotReady ? '快照就绪' : '等待快照'}</Tag></div>
        <label className="zone-control-label">标注类型</label>
        <Segmented aria-label="标注类型" block value={mode} disabled={!snapshotReady || saving}
          options={[{ label: '工位区', value: 'seat' }, { label: '限制区', value: 'restricted' }, { label: '出入口线', value: 'entry' }]}
          onChange={value => { setMode(value as DrawingMode); setPoints([]) }} />
        {mode === 'seat' && <label className="zone-control-label">关联工位<Select aria-label="关联工位" value={seatId} placeholder="选择本评标室工位" disabled={!snapshotReady || saving}
          options={roomSeats.map(seat => ({ value: seat.seat_id, label: seat.code }))} onChange={setSeatId} /></label>}
        {mode === 'entry' && <label className="zone-control-label">通行方向<Select aria-label="通行方向" value={direction} disabled={!snapshotReady || saving}
          options={[{ value: 'in', label: '进入评标区' }, { value: 'out', label: '离开评标区' }, { value: 'both', label: '双向通行' }]}
          onChange={value => setDirection(value as EntryLine['direction'])} /></label>}
        <label className="zone-control-label">名称<Input aria-label="区域或门线名称" maxLength={128} value={name} disabled={!snapshotReady || saving}
          placeholder={mode === 'entry' ? '例如：评标室入口' : '例如：专家工位区域'} onChange={event => setName(event.target.value)} /></label>
        <p className="zone-tool-hint">{mode === 'entry' ? '在画面点两下确定门线起点和终点。' : '在画面逐点围出区域，至少三个点。'}</p>
        <div className="zone-draw-actions">
          <Button icon={<UndoOutlined />} disabled={!points.length || saving} onClick={() => setPoints(current => current.slice(0, -1))}>撤销一点</Button>
          <Button disabled={!points.length || saving} onClick={() => setPoints([])}>清除当前点</Button>
          <Button type="primary" disabled={!canComplete || saving || !snapshotReady} onClick={completeShape}>{mode === 'entry' ? '完成门线' : '完成区域'}</Button>
        </div>
        <div className="zone-list-heading"><b>已标注</b><span>{draft.zones.length}个区域 · {draft.entry_lines.length}条门线</span></div>
        <div className="zone-item-list">
          {!draft.zones.length && !draft.entry_lines.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚无区域标注" /> : <>
            {draft.zones.map(zone => <div className="zone-item" key={zone.zone_id}><span className={`zone-item-swatch ${zone.kind}`} /><div><b>{zone.name}</b><small>{zone.kind === 'seat' ? roomSeats.find(seat => seat.seat_id === zone.seat_id)?.code || '工位不可用' : '限制区域'}</small></div><Button aria-label={`删除${zone.name}`} type="text" danger icon={<DeleteOutlined />} disabled={saving} onClick={() => removeZone(zone.zone_id)} /></div>)}
            {draft.entry_lines.map(line => <div className="zone-item" key={line.line_id}><span className="zone-item-swatch entry" /><div><b>{line.name}</b><small>出入口线 · {line.direction === 'both' ? '双向' : line.direction === 'in' ? '进入' : '离开'}</small></div><Button aria-label={`删除${line.name}`} type="text" danger icon={<DeleteOutlined />} disabled={saving} onClick={() => removeLine(line.line_id)} /></div>)}
          </>}
        </div>
        <small className="zone-version-note">保存将生成新配置版本；区域配置仅供检测服务读取，不代表已开始人员越界识别。</small>
      </aside>
    </div>}
  </section>
}
