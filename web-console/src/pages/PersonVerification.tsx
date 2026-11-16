import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Card, Empty, Space, Spin, Tag } from 'antd'
import { CameraOutlined, CheckCircleOutlined, ReloadOutlined, SafetyCertificateOutlined, UploadOutlined, UserOutlined } from '@ant-design/icons'
import { clearVerificationToken, consumeVerificationToken, fetchVerificationContext, submitVerification, validateVerificationFile, VerificationApiError } from '../api/participantVerification'
import type { VerificationContext, VerificationResult } from '../api/participantVerification'
import '../person-verification.css'

function errorText(cause: unknown): string { return cause instanceof TypeError ? '无法连接核验服务，请检查网络后刷新。' : cause instanceof Error ? cause.message : '请求失败，请刷新重试。' }
function dateText(value: string): string { return new Date(value).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' }) }
function scoreText(value: number | null): string { return value !== null && Number.isFinite(value) ? value.toFixed(4) : '—' }

function CandidateVideo({ stream, onReady, onError }: { stream: MediaStream; onReady: (video: HTMLVideoElement) => void; onError: () => void }) {
  const ref = useRef<HTMLVideoElement>(null)
  useEffect(() => {
    const video = ref.current
    let active = true
    if (!video) return
    video.srcObject = stream
    void video.play().catch(() => { if (active) onError() })
    return () => { active = false; video.pause(); video.srcObject = null }
  }, [stream, onError])
  return <video ref={ref} autoPlay muted playsInline aria-label="核验照片预览" onLoadedData={() => { if (ref.current) onReady(ref.current) }} onError={onError} />
}

function VerificationSession({ token }: { token: string }) {
  const [context, setContext] = useState<VerificationContext | null>(null)
  const [result, setResult] = useState<VerificationResult | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<{ file: File; url: string } | null>(null)
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [cameraError, setCameraError] = useState('')
  const [cameraPending, setCameraPending] = useState(false)
  const [cameraReady, setCameraReady] = useState(false)
  const [capturing, setCapturing] = useState(false)
  const [stream, setStream] = useState<MediaStream | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const cameraTicket = useRef(0)
  const cameraLock = useRef(false)
  const captureLock = useRef(false)
  const alive = useRef(true)
  const generation = useRef(0)
  const requestLock = useRef(false)
  const controllerRef = useRef<AbortController | null>(null)

  useEffect(() => {
    if (!file) { setPreview(null); return }
    const url = URL.createObjectURL(file)
    setPreview({ file, url })
    const disposePreview = () => URL.revokeObjectURL(url)
    window.addEventListener('pagehide', disposePreview)
    return () => { window.removeEventListener('pagehide', disposePreview); disposePreview() }
  }, [file])

  const stopCamera = useCallback(() => {
    cameraTicket.current += 1; cameraLock.current = false; captureLock.current = false
    streamRef.current?.getTracks().forEach((track) => track.stop()); streamRef.current = null
    if (videoRef.current) { videoRef.current.pause(); videoRef.current.srcObject = null }; videoRef.current = null
    if (alive.current) { setStream(null); setCameraPending(false); setCameraReady(false); setCapturing(false) }
  }, [])

  const refresh = useCallback(async () => {
    if (requestLock.current) return
    requestLock.current = true
    const ticket = ++generation.current
    const controller = new AbortController(); controllerRef.current = controller
    const timer = window.setTimeout(() => controller.abort(), 30000)
    const current = () => alive.current && ticket === generation.current
    setLoading(true); setError(''); setResult(null)
    try {
      const next = await fetchVerificationContext(token, controller.signal)
      if (current()) { setContext(next); setResult(next.last_result) }
    } catch (cause) { if (current()) { setContext(null); setError(controller.signal.aborted ? '核验信息加载超时，请重试。' : errorText(cause)) } }
    finally { window.clearTimeout(timer); if (current()) { requestLock.current = false; setLoading(false) }; if (controllerRef.current === controller) controllerRef.current = null }
  }, [token])

  useEffect(() => {
    alive.current = true
    void refresh()
    const dispose = () => {
      alive.current = false; generation.current += 1; requestLock.current = false
      controllerRef.current?.abort(); stopCamera()
    }
    window.addEventListener('pagehide', dispose)
    return () => { window.removeEventListener('pagehide', dispose); dispose() }
  }, [refresh, stopCamera])

  const canCapture = Boolean(context?.can_verify && context.status === 'active' && context.model_ready && context.remaining_attempts > 0)
  const busy = loading || submitting
  const canSubmit = canCapture && Boolean(file) && !busy && !cameraPending && !capturing

  function chooseFile(next: File | undefined) {
    if (!next || requestLock.current) return
    stopCamera(); setFile(null); setResult(null); setError(''); setCameraError('')
    const validation = validateVerificationFile(next)
    if (validation) { setError(validation); return }
    setFile(next)
  }

  async function submit() {
    if (requestLock.current || !canSubmit || !file) return
    requestLock.current = true
    const ticket = ++generation.current
    const current = () => alive.current && ticket === generation.current
    const controller = new AbortController(); controllerRef.current = controller
    const timer = window.setTimeout(() => controller.abort(), 60000)
    setSubmitting(true); setResult(null); setError(''); stopCamera()
    try {
      const next = await submitVerification(token, file, controller.signal)
      if (!current()) return
      if (next.status !== 'invalid' && (next.similarity === null || next.threshold === null || !Number.isFinite(next.similarity) || !Number.isFinite(next.threshold))) throw new Error("核验结果不完整，请刷新核对。")
      setResult(next)
      if (next.status === 'matched') setFile(null)
      // 成功和未通过都刷新剩余次数，刷新失败不允许再次提交。
      try {
        const updated = await fetchVerificationContext(token, controller.signal)
        if (current()) setContext(updated)
      } catch (cause) { if (current()) { setContext(null); setError(`提交已返回结果，但入口状态刷新失败：${errorText(cause)} 请刷新后再继续。`) } }
    } catch (cause) {
      if (!current()) return
      setResult(null)
      setError(controller.signal.aborted ? "提交超时，可能已计入次数。请刷新状态后再试。" : errorText(cause))
      // 422 也消耗提交次数。刷新最近记录，仅显示新 invalid，不恢复旧相似度。
      try {
        const updated = await fetchVerificationContext(token, controller.signal)
        if (current()) { setContext(updated); if (cause instanceof VerificationApiError && cause.status === 422 && updated.last_result?.status === 'invalid') setResult(updated.last_result) }
      } catch { if (current()) setContext(null) }
    } finally {
      window.clearTimeout(timer)
      if (current()) { requestLock.current = false; setSubmitting(false) }
      if (controllerRef.current === controller) controllerRef.current = null
    }
  }

  const cameraFailure = useCallback(() => { if (alive.current) setCameraError('摄像头预览无法播放，请停止摄像头并上传照片。') }, [])
  const videoReady = useCallback((video: HTMLVideoElement) => {
    if (!alive.current || video.srcObject !== streamRef.current || !streamRef.current) return
    videoRef.current = video; setCameraReady(Boolean(video.videoWidth && video.videoHeight))
  }, [])

  async function enableCamera() {
    if (!canCapture || requestLock.current || cameraLock.current || streamRef.current) return
    stopCamera(); setFile(null); setResult(null); setError(''); setCameraError('')
    if (!navigator.mediaDevices?.getUserMedia) { setCameraError("摄像头需在 HTTPS 或本机地址使用，也可上传照片。"); return }
    const ticket = ++cameraTicket.current; cameraLock.current = true; setCameraPending(true)
    try {
      const acquired = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false })
      if (!alive.current || ticket !== cameraTicket.current) { acquired.getTracks().forEach((track) => track.stop()); return }
      streamRef.current = acquired; setStream(acquired)
      acquired.getVideoTracks().forEach((track) => track.addEventListener('ended', () => { if (alive.current && streamRef.current === acquired) { stopCamera(); setCameraError('摄像头连接已结束，请重新开启或上传照片。') } }, { once: true }))
    } catch (cause) {
      if (alive.current && ticket === cameraTicket.current) {
        const labels: Record<string, string> = { NotAllowedError: '未获得摄像头权限，可允许权限后重试或上传照片。', NotFoundError: '未找到摄像头，请连接摄像头或上传照片。', NotReadableError: '摄像头不可读取，可能被其他应用占用，可改用上传照片。' }
        setCameraError(labels[cause instanceof DOMException ? cause.name : ''] || '开启摄像头失败，请改用上传照片。')
      }
    } finally { if (alive.current && ticket === cameraTicket.current) { cameraLock.current = false; setCameraPending(false) } }
  }
  async function capture() {
    const video = videoRef.current
    if (!video || !cameraReady || captureLock.current || requestLock.current) return
    const ticket = cameraTicket.current; captureLock.current = true; setCapturing(true); setResult(null)
    try {
      const canvas = document.createElement('canvas')
      const scale = Math.min(1, 1920 / Math.max(video.videoWidth, video.videoHeight))
      canvas.width = Math.round(video.videoWidth * scale); canvas.height = Math.round(video.videoHeight * scale)
      const drawing = canvas.getContext('2d')
      if (!drawing || !canvas.width || !canvas.height) throw new Error('摄像头画面未就绪，请稍后拍照或上传。')
      drawing.drawImage(video, 0, 0, canvas.width, canvas.height)
      const blob = await new Promise<Blob>((resolve, reject) => canvas.toBlob((value) => value ? resolve(value) : reject(new Error('拍照失败，请上传照片。')), 'image/jpeg', 0.9))
      if (!alive.current || ticket !== cameraTicket.current) return
      const next = new File([blob], 'verification-camera.jpg', { type: 'image/jpeg' })
      const validation = validateVerificationFile(next); if (validation) throw new Error(validation)
      stopCamera(); setFile(next); setCameraError('')
    } catch (cause) { if (alive.current && ticket === cameraTicket.current) setError(errorText(cause)) }
    finally { if (alive.current && ticket === cameraTicket.current) { captureLock.current = false; setCapturing(false) } }
  }

  return <>
    <Card className="pv-context-card"><div className="pv-context-heading"><div><span className="pv-eyebrow">01 · 确认核验信息</span><h1>{context?.person.name || '人员核验'}</h1><p>{context ? `${context.person.code} · ${context.person.unit || '未填写单位'}` : "正在读取核验信息…"}</p></div><Button icon={<ReloadOutlined />} disabled={busy} onClick={() => void refresh()}>刷新核验状态</Button></div>
      {context && <><div className="pv-bound-context"><span>项目：<b>{context.project_name || '未绑定项目'}</b></span><span>评标室：<b>{context.room_name || '未绑定评标室'}</b></span></div><div className="pv-context-bottom"><Tag color={context.status === 'completed' ? 'green' : context.status === 'active' ? 'blue' : 'orange'}>{context.status === 'completed' ? '核验已完成' : context.status === 'exhausted' ? '次数已用尽' : '限时核验'}</Tag><span>剩余 {context.remaining_attempts} 次提交</span><span>有效期至 {dateText(context.expires_at)}</span></div><p className="pv-helper">项目与评标室由管理员指定，不用于确认您的实际位置。</p></>}
    </Card>
    {error && <Alert className="pv-alert" type="error" showIcon message="核验未完成" description={error} />}
    {context?.message && <Alert className="pv-alert" type={context.status === 'completed' ? 'success' : context.can_verify ? 'info' : 'warning'} showIcon message={context.message} />}
    <Spin spinning={loading} tip="正在读取核验信息…"><div className="pv-work-grid"><Card className="pv-candidate-card" title={<span><span className="pv-eyebrow">02</span> 拍照或上传单人照片</span>}><div className="pv-photo-stage">{stream ? <CandidateVideo stream={stream} onReady={videoReady} onError={cameraFailure} /> : file && preview?.file === file ? <img src={preview.url} alt="本次待核验照片预览" onError={() => { setFile(null); setResult(null); setError('照片无法显示，请重新上传完整 JPG 或 PNG。') }} /> : <Empty image={<CameraOutlined />} description="选择用于本次核验的照片" />}</div>
      {file && <p className="pv-file-name">{file.name}</p>}<p className="pv-helper">JPG / PNG，最大 5 MiB。请正对镜头，确保照片包含一张清晰人脸。</p>
      <Space wrap><label className={`pv-upload ${!canCapture || busy ? 'is-disabled' : ''}`}><UploadOutlined />上传核验照片<input type="file" aria-label="上传核验照片" accept="image/jpeg,image/png,.jpg,.jpeg,.png" disabled={!canCapture || busy} onChange={(event) => { chooseFile(event.target.files?.[0]); event.target.value = '' }} /></label>{!stream && !cameraPending ? <Button icon={<CameraOutlined />} disabled={!canCapture || busy} onClick={() => void enableCamera()}>开启摄像头</Button> : <><Button disabled={!cameraReady || busy || capturing} loading={capturing} onClick={() => void capture()}>拍照</Button><Button onClick={stopCamera}>停止摄像头</Button></>}<Button disabled={busy || (!file && !stream && !cameraPending)} onClick={() => { stopCamera(); setFile(null); setResult(null); setError(''); setCameraError('') }}>清除照片</Button></Space>
      {cameraPending && <p className="pv-helper" role="status">等待摄像头授权，点击停止摄像头可取消。</p>}{cameraError && <Alert className="pv-alert" type="warning" showIcon message="可改用上传照片" description={cameraError} />}
      <Button className="pv-submit" type="primary" size="large" block loading={submitting} disabled={!canSubmit} icon={<CheckCircleOutlined />} onClick={() => void submit()}>提交核验</Button><p className="pv-helper">最多提交 5 次，图片不合格也计入次数；通过后链接不可再次使用。</p>
    </Card><Card className={`pv-result-card ${result?.status === 'matched' ? 'is-matched' : ''}`} title={<span><span className="pv-eyebrow">03</span> 核验结果</span>}>
      {result ? <><Tag color={result.status === 'matched' ? 'green' : result.status === 'unmatched' ? 'orange' : 'red'}>{result.status === 'matched' ? "匹配通过" : result.status === 'unmatched' ? "未匹配通过" : '图片不合格'}</Tag>{result.status !== 'invalid' && <div className="pv-score-row"><div><span>相似度（非准确率）</span><strong>{scoreText(result.similarity)}</strong></div><div><span>匹配阈值</span><strong>{scoreText(result.threshold)}</strong></div></div>}{result.error && <Alert className="pv-alert" type="error" showIcon message={result.error} />}<dl className="pv-result-facts"><div><dt>核验对象</dt><dd>{result.person_name}</dd></div><div><dt>核验时间</dt><dd>{dateText(result.checked_at)}</dd></div><div><dt>绑定项目</dt><dd>{result.project_name || '未绑定'}</dd></div><div><dt>绑定评标室</dt><dd>{result.room_name || '未绑定'}</dd></div><div><dt>模型</dt><dd>{result.model || '未返回'}</dd></div></dl></> : <div className="pv-result-empty"><SafetyCertificateOutlined /><strong>{submitting ? '正在核验，请勿重复提交' : '尚无核验结果'}</strong><span>选择照片后，点击“提交核验”。</span></div>}
      <div className="pv-limit-note"><b>未做活体检测</b><p>照片可能来自翻拍，相似度仅辅助人工核对，不能单独作为身份依据。</p></div>
    </Card></div></Spin>
  </>
}

export default function PersonVerification() {
  const [entry, setEntry] = useState(() => ({ token: consumeVerificationToken(), revision: 0 }))
  useEffect(() => {
    const change = () => setEntry((previous) => ({ token: consumeVerificationToken(), revision: previous.revision + 1 }))
    const restore = (event: PageTransitionEvent) => { if (event.persisted) change() }
    window.addEventListener('hashchange', change); window.addEventListener('popstate', change); window.addEventListener('pageshow', restore)
    return () => { window.removeEventListener('hashchange', change); window.removeEventListener('popstate', change); window.removeEventListener('pageshow', restore) }
  }, [])
  return <main className="person-verification-page"><div className="pv-page-shell"><header className="pv-page-header"><div><span className="pv-brand-icon"><SafetyCertificateOutlined /></span><div><strong>人员身份核验</strong><span>一对一身份核验</span></div></div>{entry.token && <Button onClick={() => { clearVerificationToken(); setEntry((previous) => ({ token: null, revision: previous.revision + 1 })) }}>清除核验入口</Button>}</header>
    {entry.token ? <VerificationSession key={entry.revision} token={entry.token} /> : <Card className="pv-no-entry"><UserOutlined /><h1>请打开管理员提供的核验链接</h1><p>核验链接无效或已失效，请联系管理员重新提供。</p><p>无需注册或登录人员账号。</p></Card>}
    <footer className="pv-page-footer">照片仅用于当前人员的一对一身份核验。</footer>
  </div></main>
}
