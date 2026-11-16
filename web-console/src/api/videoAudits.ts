// 历史视频审计：素材清单、任务提交与查询、发现明细。
// 审计发现与实时事件分属两套数据，页面也刻意分开，避免讲解时混淆。
import { readErrorDetail } from './errorText'
import { authFetch } from './auth'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await authFetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...options?.headers } })
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    throw new Error(readErrorDetail(body, response.status))
  }
  return body as T
}

export interface AuditSource {
  file_name: string
  size_mb: number
}

export interface AuditRun {
  audit_id: string
  file_name: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  progress: number
  findings_count: number
  duration_sec: number | null
  error: string | null
  created_at: string | null
  finished_at: string | null
}

export interface AuditFinding {
  at_ms: number
  at_sec: number
  event_type: string
  risk_level: string | null
  confidence: number | null
  person_count: number | null
  phone_count: number | null
  snapshot_url: string | null
  reason: string | null
}

export function fetchAuditSources(): Promise<{ items: AuditSource[]; root: string }> {
  return request('/api/video-audits/sources')
}

export function createAudit(fileName: string): Promise<{ audit_id: string; status: string; file_name: string }> {
  return request('/api/video-audits', { method: 'POST', body: JSON.stringify({ file_name: fileName }) })
}

export function fetchAudits(limit = 20): Promise<{ items: AuditRun[] }> {
  return request(`/api/video-audits?limit=${limit}`)
}

export function fetchAudit(auditId: string): Promise<AuditRun & { findings: AuditFinding[] }> {
  return request(`/api/video-audits/${encodeURIComponent(auditId)}`)
}
