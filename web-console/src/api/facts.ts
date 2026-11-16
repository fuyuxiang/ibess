import { readErrorDetail } from './errorText'
import { authFetch } from './auth'

export type FactType = 'check_in' | 'check_out' | 'change' | 'leave_permit' | 'clarification'

export interface AttendanceFact {
  fact_id: string
  project_id: string
  room_id: string
  fact_type: FactType
  person_id: string | null
  seat_id: string | null
  phase_id: string | null
  occurred_at: string
  note: string | null
  payload: Record<string, string> | null
  actor: string
  created_at: string
  person_name: string | null
  seat_code: string | null
  room_name: string | null
  replaced_person_name?: string | null
}

export const factTypeLabels: Record<FactType, string> = {
  check_in: '到场签到', check_out: '离场', change: '换人', leave_permit: '离岗许可', clarification: '澄清事实',
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  return requestApi(`/api/facts${path}`, options)
}

async function requestApi<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await authFetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...options?.headers } })
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = readErrorDetail(body, response.status) + '，请重试'
    throw new Error(detail)
  }
  return body as T
}

export function listFacts(projectId: string, filters: { room_id?: string; person_id?: string; fact_type?: string; limit?: number } = {}): Promise<{ items: AttendanceFact[]; total: number }> {
  const query = new URLSearchParams({ project_id: projectId, limit: String(filters.limit ?? 50) })
  if (filters.room_id) query.set('room_id', filters.room_id)
  if (filters.person_id) query.set('person_id', filters.person_id)
  if (filters.fact_type) query.set('fact_type', filters.fact_type)
  return request(`?${query}`)
}

export function createFact(body: {
  session_id?: string
  fact_type: FactType
  project_id: string
  room_id: string
  person_id?: string
  seat_id?: string
  phase_id?: string
  occurred_at?: string
  note?: string
  payload?: Record<string, string>
}): Promise<AttendanceFact> {
  return request('', { method: 'POST', body: JSON.stringify(body) })
}

export function deleteFact(factId: string, sessionId?: string): Promise<{ ok: boolean }> {
  return request(`/${encodeURIComponent(factId)}${sessionId?'?session_id='+encodeURIComponent(sessionId):''}`, { method: 'DELETE' })
}

export interface FactVerificationLink {
  link_id: string
  fact_id: string
  result_id: string
  actor: string
  created_at: string
  status: 'matched' | 'unmatched' | 'invalid'
  matched: boolean | null
  similarity: number | null
  checked_at: string | null
  person_name: string | null
  model: string | null
}

export function listFactLinks(projectId: string): Promise<{ links: Record<string, FactVerificationLink> }> {
  return requestApi(`/api/fact-links?project_id=${encodeURIComponent(projectId)}`)
}

export function autoLinkFacts(projectId: string): Promise<{ linked: number; skipped_no_result: number }> {
  return requestApi(`/api/fact-links/auto-link?project_id=${encodeURIComponent(projectId)}`, { method: 'POST', body: JSON.stringify({}) })
}

export function linkFact(factId: string, resultId: string): Promise<FactVerificationLink> {
  return requestApi(`/api/fact-links/${encodeURIComponent(factId)}`, { method: 'POST', body: JSON.stringify({ result_id: resultId }) })
}

export function listLinkCandidates(factId: string): Promise<{ items: Array<{ result_id: string; status: string; matched: boolean | null; similarity: number | null; checked_at: string; person_name: string; model: string | null }> }> {
  return requestApi(`/api/fact-links/${encodeURIComponent(factId)}/candidates`)
}

export interface AuthzEntry {
  entry_id: string
  person_id: string
  seat_id: string | null
  role: string
  identity_verified: boolean
  verification_result_id: string | null
  basis_fact_id: string | null
  recipient_authorized: boolean
  person_name: string | null
}

export interface AuthzSnapshot {
  snapshot_id: string
  project_id: string
  room_id: string
  phase_id: string | null
  information_visibility: 'public' | 'confidential'
  confidentiality_policy_id: string | null
  status: 'active' | 'superseded'
  created_by: string
  created_at: string
  entries: AuthzEntry[]
}

export function generateAuthzSnapshot(body: {
  project_id: string
  room_id: string
  phase_id?: string
  information_visibility: 'public' | 'confidential'
  confidentiality_policy_id?: string
  grants?: Array<{ person_id: string; recipient_authorized: boolean }>
}): Promise<AuthzSnapshot> {
  return requestApi('/api/authz/snapshots', { method: 'POST', body: JSON.stringify(body) })
}

export function listAuthzSnapshots(roomId: string): Promise<{ items: AuthzSnapshot[] }> {
  return requestApi(`/api/authz/snapshots?room_id=${encodeURIComponent(roomId)}`)
}
