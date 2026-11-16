import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Card, Empty, Modal, Space, Spin, Tag } from 'antd'
import { CameraOutlined, CheckCircleOutlined, DeleteOutlined, ReloadOutlined, SafetyCertificateOutlined, UploadOutlined, UserOutlined } from '@ant-design/icons'
import { deleteFace, FaceApiError, fetchFacePhoto, fetchFaceProfile, fetchFaceStatus, registerFace, validateFaceFile, verifyFace } from '../../api/faces'
import type { FaceProfile, FaceStatus, FaceVerification } from '../../api/faces'
import type { RegistryPerson } from '../../api/registry'
import '../../face-match.css'

export interface FaceMatchModalProps { person: RegistryPerson | null; onClose: () => void }
type BusyAction = 'register' | 'delete' | 'verify' | null
type Confirmation = 'replace' | 'delete' | null

function errorText(error: unknown): string {
  if (error instanceof TypeError) return '无法连接后台，请检查网络后重试。'
  return error instanceof Error ? error.message : '操作失败，请刷新后重试。'
}
function dateText(value: string | null): string {
  if (!value) return '未记录'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间格式无效' : date.toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' })
}
function numberText(value: number): string { return Number.isFinite(value) ? value.toFixed(4) : '无有效数值' }

/** 每个 Blob 独占 URL；换文件、关闭或切换人员时自动释放，不沿用上一张预览。 */
function useBlobPreview(blob: Blob | null): string | null {
  const [preview, setPreview] = useState<{ blob: Blob; url: string } | null>(null)
  useEffect(() => {
    if (!blob) { setPreview(null); return }
    const url = URL.createObjectURL(blob)
    setPreview({ blob, url })
    return () => URL.revokeObjectURL(url)
  }, [blob])
  return preview?.blob === blob ? preview.url : null
}

function CameraPreview({ stream, onError, onReady }: { stream: MediaStream; onError: (message: string) => void; onReady: (video: HTMLVideoElement) => void }) {
  const ref = useRef<HTMLVideoElement>(null)
  useEffect(() => {
    const video = ref.current
    let active = true
    if (!video) return
    video.srcObject = stream
    void video.play().catch(() => { if (active) onError('摄像头预览无法播放，请停止摄像头并上传照片。') })
    return () => { active = false; video.pause(); video.srcObject = null }
  }, [stream, onError])
  return <video ref={ref} autoPlay muted playsInline aria-label="手动摄像头预览" onLoadedData={() => { if (ref.current) onReady(ref.current) }} onError={() => onError('摄像头预览异常，请停止摄像头并上传照片。')} />
}

function FaceMatchSession({ person }: { person: RegistryPerson }) {
  const [status, setStatus] = useState<FaceStatus | null>(null)
  const [profile, setProfile] = useState<FaceProfile | null>(null)
  const [photo, setPhoto] = useState<Blob | null>(null)
  const [referenceFile, setReferenceFile] = useState<File | null>(null)
  const [candidate, setCandidate] = useState<File | null>(null)
  const [candidateSource, setCandidateSource] = useState<'upload' | 'camera' | null>(null)
  const [result, setResult] = useState<FaceVerification | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<BusyAction>(null)
  const [confirmation, setConfirmation] = useState<Confirmation>(null)
  const [loadError, setLoadError] = useState('')
  const [photoError, setPhotoError] = useState('')
  const [operationError, setOperationError] = useState('')
  const [cameraError, setCameraError] = useState('')
  const [notice, setNotice] = useState('')
  const [stream, setStream] = useState<MediaStream | null>(null)
  const [cameraStarting, setCameraStarting] = useState(false)
  const [cameraReady, setCameraReady] = useState(false)
  const [capturing, setCapturing] = useState(false)
  const streamRef = useRef<MediaStream | null>(null)
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const cameraGeneration = useRef(0)
  const cameraPending = useRef(false)
  const capturePending = useRef(false)
  const generation = useRef(0)
  const alive = useRef(true)
  const lock = useRef(false)
  const controllers = useRef(new Set<AbortController>())
  const referenceUrl = useBlobPreview(referenceFile)
  const candidateUrl = useBlobPreview(candidate)
  const registeredUrl = useBlobPreview(photo)

  const stopCamera = useCallback(() => {
    cameraGeneration.current += 1
    cameraPending.current = false
    capturePending.current = false
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
    if (videoRef.current) { videoRef.current.pause(); videoRef.current.srcObject = null }
    videoRef.current = null
    if (alive.current) { setStream(null); setCameraStarting(false); setCameraReady(false); setCapturing(false) }
  }, [])

  const load = useCallback(async () => {
    if (lock.current) return
    lock.current = true
    const ticket = ++generation.current
    const controller = new AbortController()
    controllers.current.add(controller)
    const timer = window.setTimeout(() => controller.abort(), 30000)
    setLoading(true); setLoadError(''); setPhotoError(''); setResult(null); setPhoto(null)
    const current = () => alive.current && ticket === generation.current
    try {
      const [statusResponse, profileResponse] = await Promise.allSettled([
        fetchFaceStatus(controller.signal), fetchFaceProfile(person.person_id, controller.signal),
      ])
      if (!current()) return
      const errors: string[] = []
      if (statusResponse.status === 'fulfilled') setStatus(statusResponse.value)
      else { setStatus(null); errors.push(`模型状态：${errorText(statusResponse.reason)}`) }
      if (profileResponse.status === 'fulfilled') {
        const next = profileResponse.value
        if (next.person_id !== person.person_id) throw new Error('登记信息的人员编号不符，请刷新。')
        setProfile(next)
        if (next.registered) {
          try {
            const blob = await fetchFacePhoto(person.person_id, controller.signal)
            if (current()) setPhoto(blob)
          } catch (cause) { if (current()) setPhotoError(`登记照片加载失败：${errorText(cause)}`) }
        }
      } else { setProfile(null); errors.push(`登记信息：${errorText(profileResponse.reason)}`) }
      if (current()) setLoadError(controller.signal.aborted ? '加载超时，请刷新登记信息。' : errors.join('；'))
    } catch (cause) { if (current()) { setProfile(null); setLoadError(errorText(cause)) } }
    finally {
      window.clearTimeout(timer); controllers.current.delete(controller)
      if (current()) { lock.current = false; setLoading(false) }
    }
  }, [person.person_id])

  useEffect(() => {
    alive.current = true
    void load()
    return () => {
      alive.current = false
      generation.current += 1
      lock.current = false
      controllers.current.forEach((controller) => controller.abort())
      controllers.current.clear()
      stopCamera()
    }
  }, [load, stopCamera])

  const available = person.enabled && status?.ready && Boolean(profile) && !loading
  const locked = loading || Boolean(busy)
  const canVerify = available && profile?.registered && candidate && !locked && !cameraStarting

  function chooseFile(file: File | undefined, kind: 'reference' | 'candidate') {
    if (!file || lock.current) return
    setResult(null); setNotice(''); setOperationError('')
    if (kind === 'candidate') { stopCamera(); setCandidate(null); setCandidateSource(null) }
    else setReferenceFile(null)
    const validation = validateFaceFile(file)
    if (validation) { setOperationError(validation); return }
    if (kind === 'reference') setReferenceFile(file)
    else { setCandidate(file); setCandidateSource('upload') }
  }

  async function mutate(action: Exclude<BusyAction, null>) {
    if (lock.current) return
    if (action !== 'delete' && !available) return
    if (action === 'verify' && !canVerify) return
    const file = action === 'register' ? referenceFile : candidate
    if (action !== 'delete' && !file) return
    lock.current = true
    const ticket = ++generation.current
    const current = () => alive.current && ticket === generation.current
    const controller = new AbortController()
    controllers.current.add(controller)
    const timer = window.setTimeout(() => controller.abort(), 60000)
    setBusy(action); setOperationError(''); setNotice(''); setResult(null); setConfirmation(null)
    let refreshAfterConflict = false
    try {
      if (action === 'verify') {
        const verification = await verifyFace(person.person_id, file!, controller.signal)
        if (!current()) return
        if (verification.person_id !== person.person_id || verification.reference_version !== profile?.version) throw new FaceApiError('登记版本已变化，请刷新登记信息并重新核验。', 409)
        if (!Number.isFinite(verification.similarity) || !Number.isFinite(verification.threshold)) throw new Error('后台未返回有效相似度与门槛，请重新核验。')
        setResult(verification)
      } else if (action === 'register') {
        const next = await registerFace(person.person_id, file!, controller.signal)
        if (!current()) return
        if (next.person_id !== person.person_id || !next.registered) throw new Error('登记返回的信息不符，请刷新核对。')
        setProfile(next); setReferenceFile(null); setPhoto(null); setPhotoError('')
        setNotice('登记照片已保存，旧核验结果已清除。')
        try {
          const blob = await fetchFacePhoto(person.person_id, controller.signal)
          if (current()) setPhoto(blob)
        } catch (cause) { if (current()) setPhotoError(`登记已保存，但照片加载失败：${errorText(cause)}`) }
      } else {
        await deleteFace(person.person_id, controller.signal)
        if (!current()) return
        setProfile({ person_id: person.person_id, registered: false, registered_at: null, model: null, photo_url: null, version: null })
        setPhoto(null); setReferenceFile(null); setPhotoError(''); setNotice('登记照片与特征已删除，旧核验结果已清除。')
      }
    } catch (cause) {
      if (!current()) return
      setOperationError(controller.signal.aborted ? '请求超时。登记或删除可能已保存，请刷新核对；核验请重新提交。' : errorText(cause))
      refreshAfterConflict = cause instanceof FaceApiError && cause.status === 409
      if (cause instanceof FaceApiError && cause.status === 503) setStatus((previous) => previous ? { ...previous, ready: false, message: cause.message } : null)
    } finally {
      window.clearTimeout(timer); controllers.current.delete(controller)
      if (current()) {
        lock.current = false; setBusy(null)
        if (refreshAfterConflict) void load()
      }
    }
  }

  const setCameraFailure = useCallback((message: string) => { if (alive.current) setCameraError(message) }, [])
  const setVideoReady = useCallback((video: HTMLVideoElement) => {
    if (!alive.current || !streamRef.current || video.srcObject !== streamRef.current) return
    videoRef.current = video
    setCameraReady(Boolean(video.videoWidth && video.videoHeight))
  }, [])

  async function startCamera() {
    if (lock.current || cameraPending.current || streamRef.current || !person.enabled) return
    stopCamera()
    setResult(null); setCandidate(null); setCandidateSource(null); setCameraError(''); setNotice('')
    if (!navigator.mediaDevices?.getUserMedia) { setCameraError('当前环境不支持摄像头。请使用 HTTPS 或本机地址，或直接上传候选照片。'); return }
    const ticket = ++cameraGeneration.current
    cameraPending.current = true
    setCameraStarting(true)
    let acquired: MediaStream | null = null
    try {
      acquired = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false })
      if (!alive.current || ticket !== cameraGeneration.current) { acquired.getTracks().forEach((track) => track.stop()); return }
      streamRef.current = acquired
      setStream(acquired)
      acquired.getVideoTracks().forEach((track) => track.addEventListener('ended', () => {
        if (alive.current && streamRef.current === acquired) { stopCamera(); setCameraError('摄像头连接已结束，可重新启用或上传照片。') }
      }, { once: true }))
    } catch (cause) {
      if (alive.current && ticket === cameraGeneration.current) {
        const name = cause instanceof DOMException ? cause.name : ''
        const messages: Record<string, string> = { NotAllowedError: '未获得摄像头权限，请允许后重试，或上传照片。', NotFoundError: '没有可用摄像头，请连接 USB 摄像头或上传照片。', NotReadableError: '摄像头无法读取，可能被其他程序占用，请关闭占用程序或上传照片。' }
        setCameraError(messages[name] || `启用摄像头失败：${errorText(cause)} 可改用上传照片。`)
      }
    } finally { if (alive.current && ticket === cameraGeneration.current) { cameraPending.current = false; setCameraStarting(false) } }
  }

  async function captureCandidate() {
    const video = videoRef.current
    if (!video || !cameraReady || lock.current || capturePending.current) return
    const ticket = cameraGeneration.current
    capturePending.current = true
    setCapturing(true)
    setOperationError(''); setResult(null)
    try {
      if (!video.videoWidth || !video.videoHeight) throw new Error('摄像头画面未就绪，请稍后拍照或上传照片。')
      const canvas = document.createElement('canvas')
      const scale = Math.min(1, 1920 / Math.max(video.videoWidth, video.videoHeight))
      canvas.width = Math.round(video.videoWidth * scale); canvas.height = Math.round(video.videoHeight * scale)
      const context = canvas.getContext('2d')
      if (!context) throw new Error('无法生成摄像头快照，请改用上传照片。')
      context.drawImage(video, 0, 0, canvas.width, canvas.height)
      const blob = await new Promise<Blob>((resolve, reject) => canvas.toBlob((value) => value ? resolve(value) : reject(new Error('拍照失败，请改用上传照片。')), 'image/jpeg', 0.9))
      if (!alive.current || ticket !== cameraGeneration.current) return
      const file = new File([blob], 'camera-candidate.jpg', { type: 'image/jpeg' })
      const validation = validateFaceFile(file)
      if (validation) throw new Error(validation)
      stopCamera(); setCandidate(file); setCandidateSource('camera'); setCameraError('')
    } catch (cause) { if (alive.current && ticket === cameraGeneration.current) setOperationError(errorText(cause)) }
    finally { if (alive.current && ticket === cameraGeneration.current) { capturePending.current = false; setCapturing(false) } }
  }

  return <div className="face-match-session">
    <div className="face-person-bar"><span className="face-person-icon"><UserOutlined /></span><div><strong>{person.name}</strong><span>{person.code} · {person.unit || '未填写单位'}</span></div><Tag color={person.enabled ? 'blue' : 'default'}>{person.enabled ? '人员已启用' : '人员已停用'}</Tag><Button icon={<ReloadOutlined />} disabled={locked} onClick={() => void load()}>刷新登记信息</Button></div>
    <Alert className="face-notice" type="info" showIcon message="仅核对所选人员与单张候选照片" description="相似度不是准确率；未做活体检测。摄像头仅用于您手动拍照，不进行连续检测或全名单搜索。" />
    {!person.enabled && <Alert className="face-notice" type="warning" showIcon message="该人员已停用，不能登记或核验。" />}
    {status && !status.ready && <Alert className="face-notice" type="warning" showIcon message="模型尚未就绪" description={status.message || '请稍后刷新状态。'} />}
    {loadError && <Alert className="face-notice" type="error" showIcon message="登记信息或模型状态加载失败" description={loadError} />}
    {operationError && <Alert className="face-notice" type="error" showIcon message="操作未完成" description={operationError} />}
    {notice && <Alert className="face-notice" type="success" showIcon message={notice} />}
    <Spin spinning={loading} tip="正在读取登记信息…"><div className="face-comparison-grid">
      <Card className="face-photo-card" title={<span><span className="face-step">01</span> 登记照片</span>} extra={<Tag color={profile?.registered ? 'green' : 'default'}>{profile?.registered ? '已登记' : profile ? '未登记' : '待加载'}</Tag>}>
        <div className="face-photo-stage">{referenceUrl || registeredUrl ? <img src={referenceUrl || registeredUrl!} alt={referenceUrl ? '待保存的登记照片预览' : `${person.name}的鉴权登记照片`} onError={() => setPhotoError('照片预览无法显示，请重新选择照片或刷新登记信息。')} /> : <Empty image={<UserOutlined />} description={profile?.registered ? '登记照片未加载' : '尚未登记照片'} />}</div>
        {referenceFile && <p className="face-file-note">待保存：{referenceFile.name} <Tag color="orange">尚未登记</Tag></p>}
        {photoError && <Alert className="face-notice" type="error" showIcon message={photoError} />}
        <p className="face-photo-help">上传单人清晰 JPG / PNG，不超过 5 MiB。</p>
        <Space wrap className="face-card-actions"><label className={`face-file-button ${!available || locked ? 'is-disabled' : ''}`}><UploadOutlined />选择登记照片<input aria-label="选择登记照片" type="file" accept="image/jpeg,image/png,.jpg,.jpeg,.png" disabled={!available || locked} onChange={(event) => { chooseFile(event.target.files?.[0], 'reference'); event.target.value = '' }} /></label><Button type="primary" loading={busy === 'register'} disabled={!available || locked || !referenceFile} onClick={() => profile?.registered ? setConfirmation('replace') : void mutate('register')}>{profile?.registered ? '替换登记照片' : '保存登记照片'}</Button></Space>
        {profile?.registered && <div className="face-registration-meta"><span>登记时间：{dateText(profile.registered_at)}</span><span>登记版本：{profile.version || '未记录'}</span><Button danger size="small" icon={<DeleteOutlined />} disabled={locked} onClick={() => setConfirmation('delete')}>删除登记</Button></div>}
      </Card>
      <Card className="face-photo-card" title={<span><span className="face-step">02</span> 待核验照片</span>} extra={<Tag>{candidateSource === 'camera' ? '手动拍照' : candidateSource === 'upload' ? '本地上传' : '未选择'}</Tag>}>
        <div className={`face-photo-stage ${stream ? 'face-camera-stage' : ''}`}>{stream ? <CameraPreview stream={stream} onError={setCameraFailure} onReady={setVideoReady} /> : candidateUrl ? <img src={candidateUrl} alt="待核验照片预览" onError={() => { setOperationError('候选照片无法预览，请重新选择清晰 JPG 或 PNG。'); setResult(null) }} /> : <Empty image={<CameraOutlined />} description="上传照片或手动启用摄像头" />}</div>
        {candidate && <p className="face-file-note">{candidate.name}</p>}
        <p className="face-photo-help">候选照片须只有一张清晰人脸，与登记人员逐一核对。</p>
        <Space wrap className="face-card-actions"><label className={`face-file-button ${!person.enabled || locked ? 'is-disabled' : ''}`}><UploadOutlined />上传候选照片<input aria-label="上传候选照片" type="file" accept="image/jpeg,image/png,.jpg,.jpeg,.png" disabled={!person.enabled || locked} onChange={(event) => { chooseFile(event.target.files?.[0], 'candidate'); event.target.value = '' }} /></label>{!stream && !cameraStarting ? <Button icon={<CameraOutlined />} disabled={!person.enabled || locked} onClick={() => void startCamera()}>手动启用摄像头</Button> : <><Button type="primary" icon={<CameraOutlined />} loading={capturing} disabled={!cameraReady || locked || capturing} onClick={() => void captureCandidate()}>拍照作为候选</Button><Button onClick={stopCamera}>停止摄像头</Button></>}<Button disabled={locked || (!candidate && !stream && !cameraStarting)} onClick={() => { stopCamera(); setCandidate(null); setCandidateSource(null); setResult(null); setCameraError(''); setOperationError(''); setNotice('') }}>清除候选</Button></Space>
        {cameraStarting && <p className="face-photo-help" role="status">等待摄像头授权，可点击停止摄像头取消。</p>}
        {cameraError && <Alert className="face-notice" type="warning" showIcon message="摄像头不可用" description={cameraError} />}
      </Card>
    </div></Spin>
    <section className={`face-result-panel ${result ? result.matched ? 'is-matched' : 'is-unmatched' : ''}`} aria-label="人脸匹配结果" aria-live="polite"><div className="face-result-heading"><div><h3><SafetyCertificateOutlined /> 一对一核验结果</h3><p>{!profile?.registered ? '请先登记照片，再选择候选进行核验。' : !status?.ready ? '请等待模型就绪后再核验。' : '核验结果由后台模型返回，请结合人工核对使用。'}</p></div><Button type="primary" size="large" icon={<CheckCircleOutlined />} loading={busy === 'verify'} disabled={!canVerify} onClick={() => void mutate('verify')}>开始一对一核验</Button></div>
      {result ? <><div className="face-result-values"><div><span>模型比对结论</span><strong>{result.matched ? '相似度达到门槛' : '相似度未达到门槛'}</strong></div><div><span>相似度（非准确率）</span><strong>{numberText(result.similarity)}</strong></div><div><span>比对门槛</span><strong>{numberText(result.threshold)}</strong></div></div><dl className="face-result-meta"><div><dt>核验对象</dt><dd>{result.person_name}</dd></div><div><dt>核验时间</dt><dd>{dateText(result.checked_at)}</dd></div><div><dt>模型</dt><dd>{result.model}</dd></div><div><dt>登记版本</dt><dd>{result.reference_version}</dd></div></dl><p className="face-result-limit">未做活体检测，照片可能来自翻拍；此结果不能单独确认人员身份。</p></> : <div className="face-result-empty">{busy === 'verify' ? '正在核验，请勿重复提交…' : '暂无核验结果'}</div>}
    </section>
    <div className="face-model-footer"><span>模型：{status?.model || '尚未读取'}</span><span>参考门槛：{status ? numberText(status.threshold) : '—'}</span><span>门槛需由现场样本校准</span></div>
    <Modal title={confirmation === 'delete' ? '确认删除人脸登记？' : '确认替换登记照片？'} open={confirmation !== null} onCancel={() => setConfirmation(null)} onOk={() => void mutate(confirmation === 'delete' ? 'delete' : 'register')} okText={confirmation === 'delete' ? '确认删除' : '确认替换'} cancelText="取消" okButtonProps={{ danger: confirmation === 'delete' }}>
      <p>人员：{person.name}（{person.code}）</p><p>{confirmation === 'delete' ? '将删除该人员已登记的照片与人脸特征。需要重新登记后才能核验。' : '新照片将替换现有登记照片与人脸特征。' } 旧核验结果会清除。</p>
    </Modal>
  </div>
}

/** person=null 立即卸载会话内容，人员切换以 key 隔离所有异步请求和预览资源。 */
export default function FaceMatchModal({ person, onClose }: FaceMatchModalProps) {
  // 不让 Modal 的关闭动画缓存继续持有摄像头会话。
  if (!person) return null
  return <Modal className="face-match-modal" title="人脸登记 · 照片管理与核验" open onCancel={onClose} footer={null} width={1040}>
    <FaceMatchSession key={`${person.person_id}:${person.enabled}`} person={person} />
  </Modal>
}

export { FaceMatchModal }
