import { authFetch } from './auth'

export const PHASE_TITLES = {
  SIGNIN: '入场签到', DISCIPLINE: '宣读纪律', PRELIM: '初步评审',
  DETAIL: '详细评审', CLARIFY: '澄清讨论', REPORT: '收尾宣读',
} as const
export type PhaseId = keyof typeof PHASE_TITLES
export type SyncStatus = 'pending' | 'partially_applied' | 'applied' | 'missing_asset' | 'failed'
export interface StageSession {
  session_id: string; name: string; status: 'draft' | 'running' | 'closed'; current_phase: string
  expected_people: number; start_at: string | null; end_at: string | null
}
export interface StageConfig {
  contract_version: '2.0.0'; project_id: string; room_id: string; session_id: string; source_id: string | null
  stage_snapshot_id: string; phase_id: PhaseId; phase_version: number; effective_at: string
  enabled_scenes: string[]; always_on_scenes: string[]; rule_versions: Record<string, string>
  assets: { slot: 'video' | 'audio' | 'scoring'; source_id: string; asset_id: string }[]
  exceptions: { fact_id: string; fact_version: number }[]; sync_status: SyncStatus
}
export interface StageControl {
  session_id: string; stage_snapshot_id: string; phase_id: PhaseId; phase_version: number
  required_consumers: string[]; acknowledged_consumers: string[]; sync_status: SyncStatus
  delivery_status: 'pending' | 'published'; attempts: number; last_error_code: string | null
}
export interface StageTimeline { session_id: string; items: (StageConfig & { actor_id: string; reason: string; created_at: string })[] }
export interface StageWrite {
  phase: PhaseId; expected_version: number; enabled_scenes: string[]; rule_versions: Record<string, string>; reason: string
  effective_at?: string; assets?: StageConfig['assets']; exceptions?: StageConfig['exceptions']
}
export class StageApiError extends Error {
  constructor(message: string, public readonly status: number, public readonly code: string) {
    super(message); this.name = 'StageApiError'
  }
}
function pathFor(id: string) {
  if (!id.trim()) throw new StageApiError('请明确选择场次', 0, 'SESSION_REQUIRED')
  return `/api/sessions/${encodeURIComponent(id)}`
}
async function request<T>(path: string, options: RequestInit = {}, signal?: AbortSignal, timeoutMs = 15000): Promise<T> {
  const controller = new AbortController()
  const abort = () => controller.abort()
  if (signal?.aborted) controller.abort()
  signal?.addEventListener('abort', abort, { once: true })
  let timedOut = false
  const timer = globalThis.setTimeout(() => { timedOut = true; controller.abort() }, timeoutMs)
  try {
    if (controller.signal.aborted) throw new DOMException('请求已取消', 'AbortError')
    const response = await authFetch(path, { ...options, signal: controller.signal, cache: 'no-store' })
    if (controller.signal.aborted) throw new DOMException('请求已取消', 'AbortError')
    const body = await response.json().catch((error: unknown) => {
      if (controller.signal.aborted || (error instanceof Error && error.name === 'AbortError')) throw error
      return null
    })
    if (controller.signal.aborted) throw new DOMException('请求已取消', 'AbortError')
    if (!response.ok) {
      const detail = body?.detail
      const code = typeof detail?.code === 'string' ? detail.code : `HTTP_${response.status}`
      const message = typeof detail === 'string' ? detail : typeof detail?.message === 'string' ? detail.message
        : Array.isArray(detail) ? '输入参数无效，请检查配置' : `阶段请求失败（${response.status}）`
      throw new StageApiError(message, response.status, code)
    }
    if (body === null) throw new StageApiError('阶段接口返回格式无效', response.status, 'INVALID_RESPONSE')
    return body as T
  } catch (error) {
    if (timedOut && !signal?.aborted) throw new StageApiError('阶段请求超时，请重试', 0, 'REQUEST_TIMEOUT')
    throw error
  } finally {
    globalThis.clearTimeout(timer); signal?.removeEventListener('abort', abort)
  }
}
const json = (method: string, body: unknown): RequestInit => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
export async function fetchStageSessions(signal?: AbortSignal) { return request<StageSession[]>('/api/sessions', {}, signal) }
export async function fetchStageConfig(id: string, signal?: AbortSignal, timeoutMs = 15000) { return request<StageConfig>(`${pathFor(id)}/phase-config`, {}, signal, timeoutMs) }
export async function fetchStageControl(id: string, signal?: AbortSignal) { return request<StageControl>(`${pathFor(id)}/control-status`, {}, signal) }
export async function fetchStageTimeline(id: string, signal?: AbortSignal) { return request<StageTimeline>(`${pathFor(id)}/timeline`, {}, signal) }
export async function bindStageSession(id: string, body: { project_id: string; room_id: string; source_id: string }, signal?: AbortSignal) { return request(`${pathFor(id)}/binding`, json('PUT', body), signal) }
export async function saveStageConfig(id: string, body: StageWrite, signal?: AbortSignal) { return request<StageConfig>(`${pathFor(id)}/phase-config`, json('PUT', body), signal) }
export async function changeStage(id: string, body: StageWrite, signal?: AbortSignal) { return request<StageConfig>(`${pathFor(id)}/phase`, json('PATCH', body), signal) }
export function phaseTitle(id: string): string { return PHASE_TITLES[id as PhaseId] || '阶段待配置' }
export function describePhaseStatus(config: StageConfig | null, control: StageControl | null): { label: string; tone: 'neutral' | 'info' | 'success' | 'warning' | 'danger' } {
  if (!config) return { label: '请选择场次', tone: 'neutral' }
  if (!control) return { label: '应用状态待确认', tone: 'neutral' }
  if (control.session_id !== config.session_id || control.stage_snapshot_id !== config.stage_snapshot_id
      || control.phase_version !== config.phase_version || control.phase_id !== config.phase_id) {
    return { label: '版本待同步', tone: 'warning' }
  }
  if (control.sync_status === 'failed') return { label: '应用失败', tone: 'danger' }
  if (control.sync_status === 'missing_asset') return { label: '素材缺失', tone: 'warning' }
  if (control.sync_status === 'applied') {
    const required = control.required_consumers
    if (Array.isArray(required) && required.length > 0 && new Set(required).size === required.length
        && required.every(c => control.acknowledged_consumers?.includes(c))) return { label: '已应用', tone: 'success' }
    return { label: '应用状态待确认', tone: 'warning' }
  }
  if (control.sync_status === 'partially_applied') return { label: '部分应用', tone: 'info' }
  if (control.sync_status === 'pending' && control.delivery_status === 'pending') return { label: '待发送', tone: 'neutral' }
  if (control.sync_status === 'pending' && control.delivery_status === 'published') return { label: '已发送，待应用', tone: 'info' }
  return { label: '应用状态待确认', tone: 'warning' }
}
