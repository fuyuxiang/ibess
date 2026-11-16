import { authFetch } from './auth'
import { readErrorDetail } from './errorText'

export interface FaceVerifyConfig {
  threshold_same: number
  threshold_suspect: number
  auto_enabled: boolean
  interval_minutes: number
  updated_by?: string | null
  updated_at?: string | null
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await authFetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...options?.headers } })
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    throw new Error(readErrorDetail(body, response.status))
  }
  return body as T
}

export function fetchFaceVerifyConfig(): Promise<FaceVerifyConfig> {
  return request<FaceVerifyConfig>('/api/face-verify/config')
}

export function updateFaceVerifyConfig(config: FaceVerifyConfig): Promise<FaceVerifyConfig> {
  return request<FaceVerifyConfig>('/api/face-verify/config', { method: 'PUT', body: JSON.stringify(config) })
}

export interface CaptureCamera {
  key: string
  label: string
}

export interface CaptureCandidate {
  person_id: string
  person_name: string
  similarity: number
}

export interface CaptureResult {
  face_found: boolean
  image: string
  camera: string
  status?: 'matched' | 'suspect' | 'unmatched'
  conclusion?: string
  similarity?: number | null
  threshold?: number
  threshold_suspect?: number
  person_id?: string
  person_name?: string
  candidates?: CaptureCandidate[]
  recorded?: boolean
  checked_at?: string
  message: string
}

export function fetchCaptureCameras(): Promise<{ items: CaptureCamera[]; default: string }> {
  return request('/api/face-verify/cameras')
}

export function captureFace(camera: string): Promise<CaptureResult> {
  return request('/api/face-verify/capture', { method: 'POST', body: JSON.stringify({ camera }) })
}
