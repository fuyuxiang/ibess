import { authFetch } from './auth'

export interface FaceStatus { ready: boolean; model: string; threshold: number; message: string }
export interface FaceProfile {
  person_id: string
  registered: boolean
  registered_at: string | null
  model: string | null
  photo_url: string | null
  version: string | null
}
export interface FaceVerification {
  matched: boolean
  similarity: number
  threshold: number
  person_id: string
  person_name: string
  model: string
  checked_at: string
  reference_version: string
  liveness_checked: false
}
export class FaceApiError extends Error {
  constructor(message: string, public readonly status: number) { super(message); this.name = 'FaceApiError' }
}
export const MAX_FACE_FILE_BYTES = 5 * 1024 * 1024

export function validateFaceFile(file: File): string {
  if (!file.size) return '照片内容为空，请重新选择 JPG 或 PNG 照片。'
  if (file.size > MAX_FACE_FILE_BYTES) return '照片不能超过 5 MiB，请压缩后重新上传。'
  if (!/\.(jpe?g|png)$/i.test(file.name) || !['image/jpeg', 'image/png'].includes(file.type)) return '仅支持 JPG / PNG 照片，请选择单人清晰照片。'
  return ''
}

async function checkResponse(response: Response): Promise<void> {
  if (response.ok) return
  const body: unknown = await response.json().catch(() => null)
  let detail = ''
  if (body && typeof body === 'object' && 'detail' in body) {
    if (typeof body.detail === 'string') detail = body.detail
    else if (Array.isArray(body.detail)) detail = body.detail.map((item: unknown) => item && typeof item === 'object' && 'msg' in item ? String(item.msg) : '照片参数无效').join('；')
  }
  const labels: Record<number, string> = {
    401: '登录已失效，请重新登录。', 403: '请求校验失败，请刷新页面后重试。',
    409: '登记状态或版本已变化，请刷新登记信息并重新核验。', 413: '照片超过大小限制，请使用不超过 5 MiB 的照片。',
    422: '照片或人员状态不符合要求，请使用单人清晰照片并确认人员已启用。', 503: '人脸匹配模型尚未就绪，请稍后刷新。',
  }
  throw new FaceApiError(detail || labels[response.status] || `人脸请求失败（HTTP ${response.status}），请重试。`, response.status)
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await authFetch(`/api/faces${path}`, init)
  await checkResponse(response)
  return response.json() as Promise<T>
}

function personPath(id: string): string { return `/persons/${encodeURIComponent(id)}` }
function multipart(file: File): FormData {
  const error = validateFaceFile(file)
  if (error) throw new FaceApiError(error, 422)
  const body = new FormData()
  body.append('file', file)
  return body
}

export function fetchFaceStatus(signal?: AbortSignal): Promise<FaceStatus> { return request('/status', { signal }) }
export function fetchFaceProfile(id: string, signal?: AbortSignal): Promise<FaceProfile> { return request(personPath(id), { signal }) }
/** 登记照片只经过同源鉴权请求，调用方负责创建并释放本地 objectURL。 */
export async function fetchFacePhoto(id: string, signal?: AbortSignal): Promise<Blob> {
  const response = await authFetch(`/api/faces${personPath(id)}/photo`, { signal })
  await checkResponse(response)
  const blob = await response.blob()
  if (!blob.size || !blob.type.startsWith('image/')) throw new FaceApiError('登记照片响应无效，请刷新后重试。', 502)
  return blob
}
export function registerFace(id: string, file: File, signal?: AbortSignal): Promise<FaceProfile> {
  return request(personPath(id), { method: 'PUT', body: multipart(file), signal })
}
export function deleteFace(id: string, signal?: AbortSignal): Promise<{ ok: boolean }> {
  return request(personPath(id), { method: 'DELETE', signal })
}
export function verifyFace(id: string, file: File, signal?: AbortSignal): Promise<FaceVerification> {
  return request(`${personPath(id)}/verify?record=true`, { method: 'POST', body: multipart(file), signal })
}
