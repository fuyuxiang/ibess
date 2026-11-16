export interface AdminUser { username: string; display_name: string; role: 'admin'|'supervisor'|'operator'|'viewer' }
export interface AuthStatus { initialized: boolean; user: AdminUser | null; csrf_token: string | null }
export interface AuthSession { user: AdminUser; csrf_token: string }
export interface LoginInput { username: string; password: string }
export interface SetupInput extends LoginInput { display_name: string }
export interface ChangePasswordInput { current_password: string; new_password: string }

export class AuthError extends Error {
  constructor(message: string, public readonly status: number) { super(message); this.name = 'AuthError' }
}

interface AuthFetchHooks {
  getCsrfToken: () => string | null
  onUnauthorized: () => void
}
let hooks: AuthFetchHooks = { getCsrfToken: () => null, onUnauthorized: () => undefined }
let sessionEpoch = 0
export function configureAuthFetch(next: AuthFetchHooks) { hooks = next }
/** Invalidate responses started under an older session before they can refill business caches. */
export function invalidateAuthRequests() { sessionEpoch += 1 }

function sameOriginUrl(input: RequestInfo | URL): URL {
  const raw = input instanceof Request ? input.url : String(input)
  const url = new URL(raw, globalThis.location.origin)
  if (url.origin !== globalThis.location.origin) throw new AuthError('不允许向其他站点发送认证请求', 0)
  return url
}

/** Drop-in fetch for same-origin business APIs. HTTP errors remain available to the caller. */
export async function authFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  sameOriginUrl(input)
  const epoch = sessionEpoch
  const method = (init?.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase()
  const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined))
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    headers.set('X-Requested-With', 'XMLHttpRequest')
    const token = hooks.getCsrfToken()
    if (token) headers.set('X-CSRF-Token', token)
    else headers.delete('X-CSRF-Token')
  }
  const response = await fetch(input, { ...init, headers, credentials: 'same-origin', cache: 'no-store', redirect: 'error' })
  if (epoch !== sessionEpoch) throw new DOMException('会话已变化，请重新加载', 'AbortError')
  if (response.status === 401) hooks.onUnauthorized()
  // A body can finish after logout/login even when its headers arrived before logout.
  // Preserve the 401 body so existing clients can display the backend's specific error.
  if (response.status !== 401) guardBody(response, epoch)
  return response
}

function guardBody(response: Response, epoch: number) {
  for (const method of ['json', 'text', 'blob', 'arrayBuffer', 'formData'] as const) {
    const read = response[method].bind(response)
    Object.defineProperty(response, method, { configurable: true, value: async () => {
      const result: unknown = await read()
      if (epoch !== sessionEpoch) throw new DOMException('会话已变化，请重新加载', 'AbortError')
      return result
    } })
  }
  const clone = response.clone.bind(response)
  Object.defineProperty(response, 'clone', { configurable: true, value: () => {
    const copy = clone()
    guardBody(copy, epoch)
    return copy
  } })
}

async function readError(response: Response): Promise<string> {
  const body: unknown = await response.json().catch(() => null)
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = body.detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) return detail.map((item: unknown) => item && typeof item === 'object' && 'msg' in item ? String(item.msg) : '输入参数无效').join('；')
  }
  const labels: Record<number, string> = { 401: '账号或密码错误，或当前登录已失效', 403: '请求校验失败，请刷新页面后重试', 409: '账号状态已变化，请重新检查登录状态', 429: '尝试次数过多，请稍后重试' }
  return labels[response.status] ?? (response.status >= 500 ? '后台服务暂时不可用，请稍后重试' : `请求失败（${response.status}），请重试`)
}

async function authRequest<T>(path: string, body?: unknown, publicRequest = false): Promise<T> {
  const controller = new AbortController()
  const timer = globalThis.setTimeout(() => controller.abort(), 15000)
  try {
    const options: RequestInit = { method: body === undefined ? 'GET' : 'POST', signal: controller.signal, credentials: 'same-origin', cache: 'no-store', redirect: 'error' }
    if (body !== undefined) {
      options.headers = { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' }
      options.body = JSON.stringify(body)
    }
    // A wrong password on a public login must not expire another tab's valid session.
    const response = await (publicRequest ? fetch(`/api/auth/${path}`, options) : authFetch(`/api/auth/${path}`, options))
    if (!response.ok) throw new AuthError(await readError(response), response.status)
    return await response.json() as T
  } catch (error) {
    if (controller.signal.aborted) throw new AuthError('请求超时，请重试', 0)
    if (error instanceof TypeError) throw new AuthError('无法连接后台，请检查网络后重试', 0)
    throw error
  } finally { globalThis.clearTimeout(timer) }
}

export function fetchAuthStatus(): Promise<AuthStatus> { return authRequest('status', undefined, true) }
export function loginAdmin(input: LoginInput): Promise<AuthSession> { return authRequest('login', input, true) }
export function setupAdmin(input: SetupInput): Promise<AuthSession> { return authRequest('setup', input, true) }
export function logoutAdmin(): Promise<{ ok: boolean }> { return authRequest('logout', {}) }
export function changeAdminPassword(input: ChangePasswordInput): Promise<{ ok: boolean }> { return authRequest('password', input) }

/** Only root-relative same-origin routes may be used after login. */
export function safeReturnTo(value: unknown, fallback = '/overview'): string {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//') || /[\\\u0000-\u0020\u007f]/.test(value)) return fallback
  try {
    const decoded = decodeURIComponent(value)
    if (decoded.startsWith('//') || /[\\\u0000-\u001f\u007f]/.test(decoded)) return fallback
    const url = new URL(value, 'https://auth.invalid')
    if (url.origin !== 'https://auth.invalid' || url.pathname === '/login' || url.pathname.startsWith('/login/')) return fallback
    return `${url.pathname}${url.search}${url.hash}`
  } catch { return fallback }
}
