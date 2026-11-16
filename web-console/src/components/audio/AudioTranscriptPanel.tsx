import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Card, Empty, Space, Spin, Tag } from 'antd'
import { AudioOutlined, ClockCircleOutlined, PlayCircleOutlined, ReloadOutlined, StopOutlined, UploadOutlined } from '@ant-design/icons'
import { audioStatusLabels, fetchAudioDetail, fetchAudioPlayback, fetchAudioRecordings, formatAudioTime, retryAudioRecording, speakerLabel, uploadAudioRecording, validAudioSegments, validateAudioFile } from '../../api/audioRecordings'
import type { AudioDetail, AudioRecording, AudioSegment } from '../../api/audioRecordings'
import '../../audio-transcript.css'
import { useAuthStore } from '../../store/useAuthStore'

const active = (recording: AudioRecording) => recording.status === 'queued' || recording.status === 'processing'
const statusColors = { queued: 'gold', processing: 'blue', ready: 'green', failed: 'red' }
function errorText(cause: unknown): string { return cause instanceof Error ? cause.message : '音频操作失败，请刷新后重试。' }
function dateText(value: string | null): string { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' }) : '未记录' }

function RecordingTranscript({ detail, disabled }: { detail: AudioDetail; disabled: boolean }) {
  const [player, setPlayer] = useState<{ url: string; label: string; segmentId: string | null } | null>(null)
  const [playbackBusy, setPlaybackBusy] = useState(false)
  const [playbackError, setPlaybackError] = useState('')
  const audioRef = useRef<HTMLAudioElement>(null)
  const urlRef = useRef<string | null>(null)
  const requestRef = useRef<AbortController | null>(null)
  const pendingPlayback = useRef<string | null>(null)
  const generation = useRef(0)
  const alive = useRef(true)
  const segments = detail.status === 'ready' ? validAudioSegments(detail.segments, detail.duration_ms) : []

  const release = useCallback(() => {
    generation.current += 1
    pendingPlayback.current = null
    requestRef.current?.abort(); requestRef.current = null
    if (audioRef.current) { audioRef.current.pause(); audioRef.current.removeAttribute('src'); audioRef.current.load() }
    if (urlRef.current) { URL.revokeObjectURL(urlRef.current); urlRef.current = null }
  }, [])
  useEffect(() => {
    alive.current = true
    const leave = () => { alive.current = false; release() }
    window.addEventListener('pagehide', leave)
    return () => { window.removeEventListener('pagehide', leave); leave() }
  }, [release])

  async function playback(segment?: AudioSegment) {
    if (disabled || detail.status !== 'ready') return
    const target = segment?.segment_id || 'whole-recording'
    if (pendingPlayback.current === target) return
    // 新播放请求先立即停止当前声音，再加载下一段。迟到 blob 不生成URL。
    release(); setPlayer(null); setPlaybackError(''); setPlaybackBusy(true)
    const ticket = generation.current
    pendingPlayback.current = target
    const controller = new AbortController(); requestRef.current = controller
    try {
      const blob = await fetchAudioPlayback(detail.recording_id, segment?.segment_id, controller.signal)
      if (!alive.current || ticket !== generation.current) return
      const url = URL.createObjectURL(blob); urlRef.current = url
      setPlayer({ url, segmentId: segment?.segment_id || null, label: segment ? `${speakerLabel(segment.speaker)} · ${formatAudioTime(segment.start_ms)} — ${formatAudioTime(segment.end_ms)}` : '整条录音 · 规范化原音回放' })
    } catch (cause) {
      if (alive.current && ticket === generation.current) setPlaybackError(`原音加载失败：${errorText(cause)}`)
    } finally {
      if (alive.current && ticket === generation.current) { setPlaybackBusy(false); pendingPlayback.current = null }
      if (requestRef.current === controller) requestRef.current = null
    }
  }
  useEffect(() => {
    if (!player || !audioRef.current) return
    let current = true
    // 点击加载后尝试播放，浏览器策略阻止时保留原生controls供手动开始。
    void audioRef.current.play().catch((cause: unknown) => {
      if (current && alive.current) setPlaybackError(cause instanceof DOMException && cause.name === 'NotAllowedError' ? '浏览器阻止自动开始，请点击下方播放器的播放按钮。' : `原音无法播放：${errorText(cause)}`)
    })
    return () => { current = false }
  }, [player])

  function stop() { release(); setPlayer(null); setPlaybackBusy(false); setPlaybackError('') }
  const filteredCount = detail.status === 'ready' ? detail.segments.length - segments.length : 0
  return <div className="at-recording-detail">
    <div className="at-detail-heading"><div><h3>{detail.title}</h3><span>{detail.source_type === 'upload' ? '上传录音' : '文件来源'} · {dateText(detail.created_at)}</span></div><Tag color={statusColors[detail.status]}>{audioStatusLabels[detail.status]}</Tag></div>
    <div className="at-recording-metrics"><div><span>录音时长</span><b>{formatAudioTime(detail.duration_ms)}</b></div><div><span>模型返回片段数</span><b>{detail.segment_count}</b></div><div><span>本录音说话人数</span><b>{detail.speaker_count ?? '未确定'}</b></div></div>
    <div className="at-player-card"><div className="at-player-title"><AudioOutlined /><strong>{player?.label || '原音回放'}</strong><Space wrap><Button size="small" icon={<PlayCircleOutlined />} disabled={disabled || detail.status !== 'ready'} onClick={() => void playback()}>播放整条原音</Button><Button size="small" icon={<StopOutlined />} disabled={!player && !playbackBusy} onClick={stop}>停止回放</Button></Space></div>
      {playbackBusy && <p className="at-note" role="status">正在鉴权加载音频…</p>}
      {player && <audio ref={audioRef} src={player.url} controls preload="metadata" aria-label="录音原音播放器" onError={() => { if (alive.current) setPlaybackError('音频无法解码或播放，请停止后重新加载原音。') }} />}
      {!player && !playbackBusy && <p className="at-note">点击整条录音或下方片段回放，播放器使用受管理员保护的原音。</p>}
      {playbackError && <Alert type="error" showIcon message={errorText(playbackError)} />}
    </div>
    {active(detail) && <Alert className="at-spaced" type="info" showIcon message={detail.status === 'queued' ? '录音已入库，等待识别' : '正在转写并进行说话人分段'} description="页面约每 3 秒更新状态。上传成功不代表识别已完成。" />}
    {detail.status === 'failed' && <Alert className="at-spaced" type="error" showIcon message="录音处理失败" description={detail.error || '后台未返回具体原因，请刷新后核对。'} />}
    {detail.status === 'ready' && <>
      <div className="at-segment-title"><h4>转写片段 <small>{segments.length} 条有效片段</small></h4><span>起止时间为录音相对时间</span></div>
      {filteredCount > 0 && <Alert className="at-spaced" type="warning" showIcon message={`${filteredCount} 条片段时间边界无效，已跳过；请检查后台识别结果。`} />}
      {segments.length ? <div className="at-segment-list">{segments.map((segment) => <article className={`at-segment-row ${player?.segmentId === segment.segment_id ? 'is-selected' : ''}`} key={segment.segment_id}><div className="at-segment-meta"><Tag color={segment.speaker ? 'blue' : 'default'}>{speakerLabel(segment.speaker)}</Tag><span><ClockCircleOutlined />{formatAudioTime(segment.start_ms)} — {formatAudioTime(segment.end_ms)}</span></div><p>{segment.text || '本片段未返回识别文字'}</p><Button size="small" icon={<PlayCircleOutlined />} disabled={disabled} onClick={() => void playback(segment)}>回放此片段</Button></article>)}</div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无有效转写片段。可能没有识别人声，页面不补造说话人或文字。" />}
    </>}
    <dl className="at-model-meta"><div><dt>转写模型</dt><dd>{detail.model || '未返回'}</dd></div><div><dt>说话人模型</dt><dd>{detail.speaker_model || '未返回'}</dd></div><div><dt>处理时间</dt><dd>{dateText(detail.processed_at)}</dd></div><div><dt>原件 SHA-256</dt><dd>{detail.sha256 || '未返回'}</dd></div></dl>
  </div>
}

export default function AudioTranscriptPanel() {
  const readonly=useAuthStore(s=>s.user?.role)==='viewer'
  const [recordings, setRecordings] = useState<AudioRecording[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [detail, setDetail] = useState<AudioDetail | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [initialLoading, setInitialLoading] = useState(true)
  const [reading, setReading] = useState(false)
  const [busy, setBusy] = useState<'upload' | 'retry' | null>(null)
  const [listError, setListError] = useState('')
  const [detailError, setDetailError] = useState('')
  const [operationError, setOperationError] = useState('')
  const [notice, setNotice] = useState('')
  // 现场拾音每 20 秒一段，会很快占满列表；默认只看上传/文件录音，按需展开
  // 默认展示现场拾音：这是"音频分析"页的主要内容，上传录音是少数场景
  const [showLive, setShowLive] = useState(true)
  const [revision, setRevision] = useState(0)
  const alive = useRef(true)
  const actionGeneration = useRef(0)
  const actionLock = useRef(false)
  const actionController = useRef<AbortController | null>(null)
  const readController = useRef<AbortController | null>(null)
  const wakeRead = useRef<() => void>(() => {})

  useEffect(() => {
    alive.current = true
    const leave = () => { alive.current = false; actionGeneration.current += 1; actionController.current?.abort(); readController.current?.abort() }
    window.addEventListener('pagehide', leave)
    return () => { window.removeEventListener('pagehide', leave); leave() }
  }, [])

  useEffect(() => {
    let current = true
    let timer: number | undefined
    let inFlight = false
    let recheck = false
    let shouldPoll = false
    const controller = new AbortController(); readController.current = controller
    setDetailError(''); setListError('')
    async function read() {
      if (!current || !alive.current) return
      if (inFlight) { recheck = true; return }
      if (timer) window.clearTimeout(timer)
      inFlight = true; setReading(true)
      let list: AudioRecording[] | null = null
      let nextDetail: AudioDetail | null = null
      let failed = false
      try {
        try {
          const response = await fetchAudioRecordings(controller.signal)
          if (!current) return
          list = response.items; setRecordings(list); setListError('')
          if (!selectedId && list.length) {
            const preferred = list.find((item) => !/^camera-/.test(item.title)) ?? list[0]
            setSelectedId(preferred.recording_id); return
          }
        } catch (cause) { if (!current) return; failed = true; setListError(`录音列表加载失败：${errorText(cause)}`) }
        if (selectedId) {
          try {
            nextDetail = await fetchAudioDetail(selectedId, controller.signal)
            if (!current) return
            if (nextDetail.recording_id !== selectedId) throw new Error('录音编号不符，请重新选择。')
            setDetail(nextDetail); setDetailError('')
          } catch (cause) { if (!current) return; failed = true; setDetailError(`录音详情加载失败：${errorText(cause)}`) }
        }
        shouldPoll = Boolean(list?.some(active) || nextDetail && active(nextDetail))
      } finally {
        inFlight = false
        if (current) {
          setInitialLoading(false); setReading(false)
          if (recheck) { recheck = false; void read() }
          else if (!failed && shouldPoll) timer = window.setTimeout(() => void read(), 3000)
        }
      }
    }
    wakeRead.current = () => { void read() }
    void read()
    return () => { current = false; controller.abort(); if (timer) window.clearTimeout(timer); if (readController.current === controller) readController.current = null }
  }, [selectedId, revision])

  function refresh() { wakeRead.current() }
  function select(id: string) {
    if (id === selectedId) return
    actionGeneration.current += 1; actionController.current?.abort(); actionLock.current = false
    setBusy(null); setOperationError(''); setNotice(''); setDetail(null); setSelectedId(id)
  }
  async function mutate(action: 'upload' | 'retry') {
    if (actionLock.current || action === 'upload' && !file || action === 'retry' && detail?.status !== 'failed') return
    if (action === 'upload') { const validation = validateAudioFile(file!); if (validation) { setOperationError(validation); return } }
    actionLock.current = true
    const ticket = ++actionGeneration.current
    const controller = new AbortController(); actionController.current = controller
    setBusy(action); setOperationError(''); setNotice('')
    try {
      const saved = action === 'upload' ? await uploadAudioRecording(file!, controller.signal) : await retryAudioRecording(selectedId, controller.signal)
      if (!alive.current || ticket !== actionGeneration.current) return
      setNotice(action === 'upload' ? 'PCM WAV 已上传并入库，识别尚未完成；请查看后台处理状态。' : '重试请求已提交，请查看后台处理状态。')
      if (action === 'upload') setFile(null)
      setDetail(null); setSelectedId(saved.recording_id); setRevision((previous) => previous + 1)
    } catch (cause) { if (alive.current && ticket === actionGeneration.current) setOperationError(errorText(cause)) }
    finally { if (alive.current && ticket === actionGeneration.current) { actionLock.current = false; setBusy(null) }; if (actionController.current === controller) actionController.current = null }
  }
  const visibleRecordings = showLive ? recordings : recordings.filter((item) => !/^camera-/.test(item.title))
  const selectedDetail = detail?.recording_id === selectedId ? detail : null
  const hasActive = recordings.some(active) || selectedDetail && active(selectedDetail)

  return <section className="audio-transcript-panel" aria-label="文件音频转写与说话人分段"><div className="at-panel-heading"><p className="at-heading-note"><AudioOutlined /> 说话人标签仅在本录音内有效，未绑定真实姓名；支持片段时间与原音回放。</p><Button size="small" icon={<ReloadOutlined />} disabled={reading} onClick={refresh}>刷新录音与状态</Button></div>

    <div className="at-work-grid"><Card className="at-list-card" title="录音采集与最近录音" extra={<Space size={6}><Button size="small" type="link" onClick={() => setShowLive((previous) => !previous)}>{showLive ? '只看上传录音' : '显示现场拾音'}</Button><span>{visibleRecordings.length} 条</span></Space>}>
      {!readonly && <div className="at-upload-inline"><div className="at-upload-content"><div><strong>上传 PCM WAV 录音</strong><p>最大 20 MiB、最长 10 分钟。上传走离线识别；cam7 的现场拾音由服务端自动切片，不经过这里。</p></div><Space wrap><label className={`at-file-button ${busy ? 'is-disabled' : ''}`}><UploadOutlined />选择 WAV 文件<input type="file" aria-label="选择 PCM WAV 文件" accept=".wav,audio/wav,audio/x-wav" disabled={Boolean(busy)} onChange={(event) => { const next = event.target.files?.[0]; event.target.value = ''; setNotice(''); setOperationError(''); setFile(null); if (next) { const validation = validateAudioFile(next); if (validation) setOperationError(validation); else setFile(next) } }} /></label><Button type="primary" loading={busy === 'upload'} disabled={!file || Boolean(busy)} onClick={() => void mutate('upload')}>上传并开始识别</Button></Space></div>{file && <p className="at-file-name">待上传：{file.name} · {(file.size / 1024 / 1024).toFixed(2)} MiB</p>}
      {operationError && <Alert className="at-spaced" type="error" showIcon message="操作未完成" description={errorText(operationError)} />}{notice && <Alert className="at-spaced" type="success" showIcon message={selectedDetail?.status === 'ready' ? '录音识别已完成，可查看转写片段和回放原音。' : notice} />}
    </div>}
      {listError && <Alert type="error" showIcon message={errorText(listError)} action={<Button size="small" onClick={refresh} disabled={reading}>重试查询</Button>} />}
      {initialLoading ? <div className="at-list-loading"><Spin /></div> : visibleRecordings.length ? <div className="at-recording-list">{visibleRecordings.map((recording) => <button key={recording.recording_id} className={`at-recording-choice ${recording.recording_id === selectedId ? 'is-active' : ''}`} aria-pressed={recording.recording_id === selectedId} onClick={() => select(recording.recording_id)}><div><strong>{recording.title}</strong><Tag color={statusColors[recording.status]}>{audioStatusLabels[recording.status]}</Tag></div><span>{dateText(recording.created_at)}</span><small>{formatAudioTime(recording.duration_ms)} · {recording.segment_count} 个模型片段</small></button>)}</div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={listError
        ? '列表暂不可用，请重试查询'
        : !showLive && recordings.length
          ? `现场拾音 ${recordings.length} 条已隐藏，点右上角「显示现场拾音」查看`
          : '暂无录音，可上传 PCM WAV 开始识别'} />}
      <p className="at-note">{hasActive ? '存在待处理录音，约每 3 秒更新一次；请求完成后再轮询。' : '识别结果来自后台录音记录。可手动刷新查看新文件。'}</p>
    </Card><Card className="at-main-card" title="录音转写与原音" extra={!readonly && selectedDetail?.status === 'failed' ? <Button loading={busy === 'retry'} disabled={Boolean(busy) || Boolean(detailError)} onClick={() => void mutate('retry')}>重试识别</Button> : null}>
      {detailError && <Alert type="error" showIcon message={errorText(detailError)} action={<Button size="small" disabled={reading} onClick={refresh}>重新加载详情</Button>} />}
      {selectedDetail ? <RecordingTranscript key={selectedId} detail={selectedDetail} disabled={Boolean(detailError) || Boolean(busy)} /> : reading && selectedId ? <div className="at-detail-loading"><Spin tip="正在读取录音…"><div /></Spin></div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={detailError ? '录音详情暂不可用，请重新加载' : '请选择一条录音查看识别片段'} />}
    </Card></div>
  </section>
}
