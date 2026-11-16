import { authFetch } from './auth'

export type ZonePoint = [number, number]
export interface DrawnZone {
  zone_id: string
  name: string
  kind: 'seat' | 'restricted'
  seat_id: string | null
  polygon: ZonePoint[]
}
export interface EntryLine {
  line_id: string
  name: string
  start: ZonePoint
  end: ZonePoint
  direction: 'in' | 'out' | 'both'
}
export interface ZoneConfig {
  project_id: string
  room_id: string
  session_id: string
  source_id: string
  config_version: number
  zones: DrawnZone[]
  entry_lines: EntryLine[]
}
export interface ZoneConfigWrite {
  config_version: number
  zones: DrawnZone[]
  entry_lines: EntryLine[]
}

export class ZoneApiError extends Error {
  constructor(message: string, public readonly status: number, public readonly code: string) {
    super(message)
    this.name = 'ZoneApiError'
  }
}

function pathFor(sessionId: string, sourceId: string) {
  if (!sessionId.trim()) throw new ZoneApiError('请明确选择评标场次', 0, 'SESSION_REQUIRED')
  if (!sourceId.trim()) throw new ZoneApiError('请明确选择视频源', 0, 'SOURCE_REQUIRED')
  return `/api/sessions/${encodeURIComponent(sessionId)}/sources/${encodeURIComponent(sourceId)}/zones`
}

async function request<T>(path: string, init?: RequestInit, signal?: AbortSignal, timeoutMs = 15000): Promise<T> {
  const controller = new AbortController()
  const abort = () => controller.abort()
  if (signal?.aborted) controller.abort()
  signal?.addEventListener('abort', abort, { once: true })
  let timedOut = false
  const timer = globalThis.setTimeout(() => { timedOut = true; controller.abort() }, timeoutMs)
  try {
    if (controller.signal.aborted) throw new DOMException('请求已取消', 'AbortError')
    const response = await authFetch(path, { ...init, signal: controller.signal, cache: 'no-store' })
    if (controller.signal.aborted) throw new DOMException('请求已取消', 'AbortError')
    const body = await response.json().catch((error: unknown) => {
      if (controller.signal.aborted || (error instanceof Error && error.name === 'AbortError')) throw error
      return null
    })
    if (controller.signal.aborted) throw new DOMException('请求已取消', 'AbortError')
    if (!response.ok) {
      const detail = body?.detail
      const code = typeof detail?.code === 'string' ? detail.code : `HTTP_${response.status}`
      const message = typeof detail === 'string' ? detail : typeof detail?.message === 'string'
        ? detail.message : Array.isArray(detail) ? '区域配置参数无效' : `区域配置请求失败（${response.status}）`
      throw new ZoneApiError(message, response.status, code)
    }
    if (body === null) throw new ZoneApiError('区域接口返回格式无效', response.status, 'INVALID_RESPONSE')
    return body as T
  } catch (error) {
    if (timedOut && !signal?.aborted) throw new ZoneApiError('区域配置请求超时，请刷新确认后重试', 0, 'REQUEST_TIMEOUT')
    throw error
  } finally {
    globalThis.clearTimeout(timer)
    signal?.removeEventListener('abort', abort)
  }
}

export async function fetchZoneConfig(sessionId: string, sourceId: string, signal?: AbortSignal, timeoutMs = 15000) {
  return request<ZoneConfig>(pathFor(sessionId, sourceId), undefined, signal, timeoutMs)
}

export async function saveZoneConfig(sessionId: string, sourceId: string, body: ZoneConfigWrite, signal?: AbortSignal) {
  return request<ZoneConfig>(pathFor(sessionId, sourceId), {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }, signal)
}
