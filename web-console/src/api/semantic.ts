import { readErrorDetail } from './errorText'
// 3.4 语义研判面板 API：语义任务查询/创建/重试 + 授权快照 latest（trusted context）。
import { authFetch } from './auth'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await authFetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...options?.headers } })
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = readErrorDetail(body, response.status)
    throw new Error(detail)
  }
  return body as T
}

export type SemanticScene = 'S06' | 'S07'
export type SemanticConclusion = 'normal' | 'suspected' | 'insufficient'

export interface SemanticEvidence { segment_id: string; quote: string; start_ms: number; end_ms: number }

export interface SemanticAnalysis {
  intake_error?: string | null
  window_start_ms: number
  window_end_ms: number
  event_id?: string | null
  analysis_id: string
  recording_id: string
  scene_id: SemanticScene
  status: 'queued' | 'processing' | 'ready' | 'failed'
  deduplicated?: boolean
  result: {
    scene_id: SemanticScene
    conclusion: SemanticConclusion
    risk_level: 'none' | 'low' | 'medium' | 'high'
    reason: string
    evidence: SemanticEvidence[]
    missing_facts: string[]
    recommendation: 'no_action' | 'human_review' | 'collect_evidence'
  } | null
  error: string | null
  created_at: string
  completed_at: string | null
}

export function listSemanticAnalyses(recordingId: string): Promise<{ items: SemanticAnalysis[] }> {
  return request(`/api/audio/recordings/${encodeURIComponent(recordingId)}/semantic-analyses?limit=30`)
}

export function createSemanticAnalysis(recordingId: string, input: Record<string, unknown>): Promise<SemanticAnalysis> {
  return request(`/api/audio/recordings/${encodeURIComponent(recordingId)}/semantic-analyses`, { method: 'POST', body: JSON.stringify({ input }) })
}

export function retrySemanticAnalysis(analysisId: string): Promise<SemanticAnalysis> {
  return request(`/api/audio/semantic-analyses/${encodeURIComponent(analysisId)}/retry`, { method: 'POST', body: JSON.stringify({}) })
}

export interface AuthzBinding { segment_speaker: string | null; person_id: string; display_name: string | null; role: string; identity_verified: boolean; basis_fact_id: string | null }

export interface AuthzSnapshotContext {
  snapshot_id: string
  project_id: string
  room_id: string
  phase_id: string | null
  binding_status: 'bound'
  information_visibility: 'public' | 'confidential'
  confidentiality_policy_id: string | null
  speaker_bindings: AuthzBinding[]
  recipient_authorized: boolean
  recipient_fact_id: string
  context_snapshot_id: string
  created_by: string
  created_at: string
}

export function latestAuthzSnapshot(roomId: string): Promise<AuthzSnapshotContext> {
  return request(`/api/authz/snapshots/latest?room_id=${encodeURIComponent(roomId)}`)
}

export interface MediaBinding { binding_id: string; recording_id: string; project_id: string; room_id: string; actor: string; created_at: string; deduplicated?: boolean; rebound?: boolean }

export function getRecordingBinding(recordingId: string): Promise<MediaBinding> {
  return request(`/api/media-bindings/by-recording/${encodeURIComponent(recordingId)}`)
}

export function bindRecording(recordingId: string, roomId: string): Promise<MediaBinding> {
  return request('/api/media-bindings', { method: 'POST', body: JSON.stringify({ recording_id: recordingId, room_id: roomId }) })
}
