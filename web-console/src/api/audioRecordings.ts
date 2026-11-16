import { authFetch } from './auth'

export type AudioRecordingStatus = 'queued' | 'processing' | 'ready' | 'failed'
export interface AudioRecording {
  recording_id: string; title: string; source_id: string; source_type: 'file' | 'upload'
  status: AudioRecordingStatus; duration_ms: number; created_at: string; processed_at: string | null
  model: string | null; speaker_model: string | null; speaker_count: number | null
  segment_count: number; sha256: string; error: string | null; playback_url: string | null
}
export interface AudioSegment {
  segment_id: string; speaker: string | null; start_ms: number; end_ms: number; text: string; playback_url: string
}
export interface AudioDetail extends AudioRecording { segments: AudioSegment[]; capture_started_at?: string | null }
export class AudioRecordingError extends Error {
  constructor(message: string, public readonly status: number) { super(message); this.name = 'AudioRecordingError' }
}
export const audioStatusLabels: Record<AudioRecordingStatus, string> = { queued: '等待处理', processing: '正在识别', ready: '识别完成', failed: '处理失败' }
export function validateAudioFile(file: File): string {
  if (!file.size) return '文件为空，请重新选择 PCM WAV 文件。'
  if (!/\.wav$/i.test(file.name)) return '仅支持 PCM WAV 文件，请将录音转换为 WAV 后上传。'
  if (file.size > 20 * 1024 * 1024) return '文件不能超过 20 MiB，请裁剪或压缩后重新上传。'
  return ''
}
export function formatAudioTime(ms: number): string { return Number.isFinite(ms) && ms >= 0 ? `${(ms / 1000).toFixed(3)} 秒` : '—' }
export function speakerLabel(speaker: string | null): string { return speaker?.trim() ? `说话人 ${speaker}` : '说话人未确定' }
export function validAudioSegments(segments: AudioSegment[], duration: number): AudioSegment[] {
  if (!Number.isFinite(duration) || duration < 0) return []
  return segments.filter((segment) => Number.isFinite(segment.start_ms) && Number.isFinite(segment.end_ms) && segment.start_ms >= 0 && segment.end_ms > segment.start_ms && segment.end_ms <= duration)
}
async function checkResponse(response: Response): Promise<void> {
  if (response.ok) return
  const body: unknown = await response.json().catch(() => null)
  let detail = ''
  if (body && typeof body === 'object' && 'detail' in body) {
    if (typeof body.detail === 'string') detail = body.detail
    else if (Array.isArray(body.detail)) detail = body.detail.map((item: unknown) => item && typeof item === 'object' && 'msg' in item ? String(item.msg) : '音频参数无效').join('；')
  }
  const labels: Record<number, string> = { 401: '登录已失效，请重新登录。', 403: '请求校验失败，请刷新后重试。', 404: '该录音或片段不存在，请刷新列表。', 409: '录音状态已变化，请刷新后再操作。', 413: '音频超过大小限制，请使用不超过 20 MiB 的 PCM WAV。', 422: '音频无效，请使用不超过 10 分钟的 PCM WAV。', 503: '音频服务暂时不可用，请稍后重试。' }
  throw new AudioRecordingError(detail || labels[response.status] || `音频请求失败（HTTP ${response.status}），请重试。`, response.status)
}
async function request<T>(path: string, init: RequestInit = {}, blob = false): Promise<T> {
  const controller = new AbortController()
  const external = init.signal
  const abort = () => controller.abort()
  if (external?.aborted) controller.abort()
  else external?.addEventListener('abort', abort, { once: true })
  let timedOut = false
  const timer = globalThis.setTimeout(() => { timedOut = true; controller.abort() }, 30000)
  try {
    const response = await authFetch(`/api/audio/recordings${path}`, { ...init, signal: controller.signal })
    await checkResponse(response)
    if (blob) {
      const audio = await response.blob()
      if (!audio.size || !/^(audio\/(wav|x-wav|wave|vnd\.wave)|application\/octet-stream)(;|$)/i.test(audio.type)) throw new AudioRecordingError('回放内容无效，请刷新后重新加载原音。', 502)
      return audio as T
    }
    return await response.json() as T
  } catch (cause) {
    if (timedOut) throw new AudioRecordingError('请求超时。上传或重试可能已保存，请刷新录音列表核对。', 0)
    if (cause instanceof TypeError) throw new AudioRecordingError('无法连接音频服务，请检查网络后重试。', 0)
    throw cause
  } finally { globalThis.clearTimeout(timer); external?.removeEventListener('abort', abort) }
}
const recordingPath = (id: string) => `/${encodeURIComponent(id)}`
export function fetchAudioRecordings(signal?: AbortSignal): Promise<{ items: AudioRecording[] }> { return request('?limit=30', { signal }) }
export function fetchAudioDetail(id: string, signal?: AbortSignal): Promise<AudioDetail> { return request(recordingPath(id), { signal }) }
export function saveCaptureTime(id:string,startedAt:string|null):Promise<{capture_started_at:string|null}> {
  return request(`${recordingPath(id)}/capture-time`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({started_at:startedAt})})
}
export function uploadAudioRecording(file: File, signal?: AbortSignal): Promise<AudioRecording> {
  const error = validateAudioFile(file)
  if (error) return Promise.reject(new AudioRecordingError(error, 422))
  const body = new FormData(); body.append('file', file)
  return request('', { method: 'POST', body, signal })
}
export function retryAudioRecording(id: string, signal?: AbortSignal): Promise<AudioRecording> { return request(`${recordingPath(id)}/retry`, { method: 'POST', signal }) }
/** 用稳定ID构造同源鉴权路径，不直接加载响应中的私有URL；调用方释放objectURL。 */
export function fetchAudioPlayback(id: string, segmentId?: string, signal?: AbortSignal): Promise<Blob> {
  const path = segmentId ? `${recordingPath(id)}/segments/${encodeURIComponent(segmentId)}/playback` : `${recordingPath(id)}/playback`
  return request(path, { signal }, true)
}
