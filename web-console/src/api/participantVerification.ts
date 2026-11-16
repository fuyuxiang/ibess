import { authFetch } from './auth'

export type VerificationLinkStatus = 'active' | 'completed' | 'expired' | 'revoked' | 'exhausted' | 'invalidated'
export interface LinkInfo {
  link_id: string; person_id: string; created_at: string; expires_at: string
  status: VerificationLinkStatus; attempts: number; max_attempts: number
  project_id: string | null; project_name: string | null; room_id: string | null; room_name: string | null
}
export interface VerificationResult {
  result_id: string; link_id: string | null; status: 'matched' | 'suspect' | 'unmatched' | 'invalid'
  matched: boolean | null; similarity: number | null; threshold: number | null; checked_at: string
  person_name: string; project_name: string | null; room_name: string | null; model: string | null
  reference_version: string; error: string | null; liveness_checked: false
}
export interface VerificationContext {
  person: { name: string; code: string; unit: string }; project_name: string | null; room_name: string | null
  expires_at: string; status: 'active' | 'completed' | 'exhausted'; remaining_attempts: number
  model_ready: boolean; message: string; can_verify: boolean; last_result: VerificationResult | null
}
export interface CreateVerificationLinkInput { expires_in_minutes: 15 | 60 | 240 | 1440; project_id?: string | null; room_id?: string | null }
export interface VerificationFaceProfile { person_id: string; registered: boolean; version: string | null }
export type CreatedVerificationLink = LinkInfo & { verification_url: string }
export class VerificationApiError extends Error {
  constructor(message: string, public readonly status: number) { super(message); this.name = 'VerificationApiError' }
}
const tokenKey = 'person-verification-token'
const validToken = (value: string | null): value is string => Boolean(value && /^[A-Za-z0-9_-]{43}$/.test(value))

/** 仅消费 fragment；显式无效入口不会复用旧凭证。不读取 query/localStorage。 */
export function consumeVerificationToken(): string | null {
  if (globalThis.location.hash) {
    const raw = globalThis.location.hash.slice(1)
    globalThis.history.replaceState(globalThis.history.state, '', globalThis.location.pathname + globalThis.location.search)
    try {
      if (validToken(raw)) sessionStorage.setItem(tokenKey, raw)
      else sessionStorage.removeItem(tokenKey)
    } catch { /* 禁用存储时仅保留本次页面内存凭证。 */ }
    return validToken(raw) ? raw : null
  }
  try {
    const value = sessionStorage.getItem(tokenKey)
    if (validToken(value)) return value
    sessionStorage.removeItem(tokenKey)
  } catch { /* 无存储时须重新打开原始入口。 */ }
  return null
}
export function clearVerificationToken(): void {
  try { sessionStorage.removeItem(tokenKey) } catch { /* 页面内存凭证由调用方同时清空。 */ }
  if (globalThis.location.hash) globalThis.history.replaceState(globalThis.history.state, '', globalThis.location.pathname + globalThis.location.search)
}
export function isVerificationUrl(url: string): boolean { return /^\/verify#[A-Za-z0-9_-]{43}$/.test(url) }
export function validateVerificationFile(file: File): string {
  if (!file.size) return '照片为空，请重新选择。'
  if (file.size > 5 * 1024 * 1024) return '照片不能超过 5 MiB。'
  if (!/\.(png|jpe?g)$/i.test(file.name) || !['image/jpeg', 'image/png'].includes(file.type)) return '仅支持 JPG / PNG 单人清晰照片。'
  return ''
}
async function checked<T>(response: Response): Promise<T> {
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    let detail = ''
    if (body && typeof body === 'object' && 'detail' in body) {
      if (typeof body.detail === 'string') detail = body.detail
      else if (Array.isArray(body.detail)) detail = body.detail.map((value: unknown) => value && typeof value === 'object' && 'msg' in value ? String(value.msg) : '输入参数无效').join('；')
    }
    const labels: Record<number, string> = { 404: '核验入口无效，请联系管理员重新生成。', 409: '核验已完成或登记版本变化，请刷新入口状态。', 410: '核验入口已失效，请联系管理员重新生成。', 422: '照片不符合要求，请使用单人清晰照片。', 429: '提交次数已用尽，请联系管理员重新生成。', 503: '模型尚未就绪，请稍后刷新。' }
    throw new VerificationApiError(detail || labels[response.status] || `请求失败（HTTP ${response.status}），请重试。`, response.status)
  }
  if (body === null) throw new VerificationApiError('后台响应无效，请刷新重试。', 502)
  return body as T
}
function publicRequest<T>(path: '/context' | '/submit', token: string, init: RequestInit = {}): Promise<T> {
  if (!validToken(token)) return Promise.reject(new VerificationApiError('核验入口无效，请重新打开管理员提供的链接。', 404))
  const headers = new Headers(init.headers)
  headers.set('Authorization', `Bearer ${token}`)
  if (init.method === 'POST') headers.set('X-Requested-With', 'XMLHttpRequest')
  return fetch(`/api/participant-verification${path}`, { ...init, headers, credentials: 'omit', cache: 'no-store', redirect: 'error' }).then(checked<T>)
}
export function fetchVerificationContext(token: string, signal?: AbortSignal): Promise<VerificationContext> { return publicRequest('/context', token, { signal }) }
export function submitVerification(token: string, file: File, signal?: AbortSignal): Promise<VerificationResult> {
  const error = validateVerificationFile(file)
  if (error) return Promise.reject(new VerificationApiError(error, 422))
  const body = new FormData(); body.append('file', file)
  return publicRequest('/submit', token, { method: 'POST', body, signal })
}
function adminRequest<T>(path: string, init?: RequestInit): Promise<T> { return authFetch(`/api/faces${path}`, init).then(checked<T>) }
const personPath = (id: string) => `/persons/${encodeURIComponent(id)}`
export function fetchVerificationProfile(id: string, signal?: AbortSignal): Promise<VerificationFaceProfile> { return adminRequest(personPath(id), { signal }) }
export function fetchVerificationLinks(id: string, signal?: AbortSignal): Promise<{ items: LinkInfo[] }> { return adminRequest(`${personPath(id)}/verification-links`, { signal }) }
export function fetchVerificationResults(id: string, signal?: AbortSignal): Promise<{ items: VerificationResult[] }> { return adminRequest(`${personPath(id)}/verification-results`, { signal }) }
export async function createVerificationLink(id: string, input: CreateVerificationLinkInput, signal?: AbortSignal): Promise<CreatedVerificationLink> {
  const result = await adminRequest<CreatedVerificationLink>(`${personPath(id)}/verification-links`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input), signal })
  if (!isVerificationUrl(result.verification_url)) throw new VerificationApiError('后台返回的核验链接无效，请刷新列表核对；请勿重复生成。', 502)
  return result
}
export function revokeVerificationLink(id: string, signal?: AbortSignal): Promise<{ ok: true }> { return adminRequest(`/verification-links/${encodeURIComponent(id)}`, { method: 'DELETE', signal }) }
