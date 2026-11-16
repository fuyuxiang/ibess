import { authFetch } from './auth'
import type { EvidenceContext } from './workspace'

export interface EventItem {
  event_id: string
  event_type: string
  camera_id: string
  session_id: string
  risk_level: string
  confidence: number | null
  reason: string
  status: string
  occurred_at: string | null
  created_at?: string | null
  is_demo?: boolean
  room?: string
  project_id?: string | null
  project_name?: string | null
  room_id?: string | null
  seat_id?: string | null
  seat_code?: string | null
  person_id?: string | null
  person_name?: string | null
  source_type?: string
  model_name?: string
  model_version?: string
  skill_code?: string
  skill_version?: string
  phase_id?: string
  machine_conclusion?: string | null
  workflow_status?: string
  workflow_version?: number
  category?: 'behavior' | 'technical' | 'observation'
  resolution?: string | null
  assignee?: string | null
  due_at?: string | null
  feedback?: string | null
  archived_at?: string | null
  origin?: 'manual'|'machine'
  behavior?: {started_at:string;last_seen_at:string;ended_at:string|null;active:boolean;trigger_count:number;end_basis:string|null}|null
  task_receipt?: {assignee:string;received_by:string|null;received_at:string|null;note:string|null}|null
}

export interface DecisionItem {
  id?: number
  action: string
  reviewer_id: string
  note: string
  decided_at: string | null
}

export interface EventDetail extends EventItem {
  context_evidence?: EvidenceContext;
  evidence_status?: string;
  media_hold?: {status:string; files:number; error:string|null; updated_at:string|null} | null;
  archives?: {version:number; resolution:string|null; actor:string; note:string; evidence_note:string|null; at:string; snapshot:Record<string,unknown>}[];
  decisions: DecisionItem[]
  evidences: EvidenceItem[]
  reminders: ReminderItem[]
  audit: AuditItem[]
  demo_input?: { note: string; room: string } | null
  clues?: { clue_id: string; scene_id: string; rule_id: string; rule_version: string; conclusion: string; reason: string; missing_facts: string[]; quality_flags: string[]; evidence_refs: unknown[]; start_ms: number; end_ms: number }[]
  semantic?: { analysis_id: string; recording_id: string; scene_id: string; window_start_ms: number; window_end_ms: number; context: Record<string, unknown>; result: { conclusion: string; reason: string; missing_facts: string[]; evidence: { segment_id: string; quote: string; start_ms: number; end_ms: number }[] }; segments: { segment_id: string; speaker: string | null; start_ms: number; end_ms: number; text: string }[] } | null
}

export interface EvidenceItem {
  id: number; evidence_type: string; object_key: string; url: string; full_url?: string; captured_at: string | null; sha256?: string | null
}
export interface AuditItem { action: string; actor: string; note: string; at: string | null }
export interface ReminderItem {
  execution_id: string
  attempt_id: string | null
  attempts?: { attempt_id: string; status: string; error: string | null; asset_version: string | null; created_at: string | null }[]
  status: 'NOT_CONFIGURED' | 'QUEUED' | 'PLAYING' | 'COMPLETED' | 'FAILED'
  asset_url?: string | null; asset_version?: string | null; error?: string | null
  started_at: string | null; completed_at: string | null; created_at: string | null
}
export interface EvaluationSession {
  session_id: string; name: string; status: 'draft' | 'running' | 'closed'
  current_phase: string; expected_people: number; start_at: string | null; end_at: string | null
  phase_config?: PhaseConfig
}
export interface PhaseConfig {
  phase: string; title: string; enabled_scenes: string[]; media_asset: string
  source_mode: 'pre_recorded' | 'simulated_data' | 'offline_replay'; review_policy: string
}

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const controller = new AbortController()
  const external = options?.signal
  const abort = () => controller.abort()
  if (external?.aborted) controller.abort()
  else external?.addEventListener('abort',abort,{once:true})
  const timer = window.setTimeout(() => controller.abort(), 15000)
  try {
    const response = await authFetch(url, { ...options, signal: controller.signal })
    if (!response.ok) {
      const body = await response.json().catch(() => ({}))
      const detail = typeof body.detail === 'string' ? body.detail : Array.isArray(body.detail) ? body.detail.map((item: { msg: string }) => item.msg).join('；') : `请求失败（${response.status}）`
      throw new Error(detail)
    }
    return response.json()
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw new Error('请求超时，请重试')
    throw error
  } finally { window.clearTimeout(timer); external?.removeEventListener('abort',abort) }
}
function jsonBody(body: unknown, method = 'POST'): RequestInit {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
}
export function createEvaluationSession(body: { name: string; current_phase?: string; expected_people?: number }): Promise<EvaluationSession> {
  return request('/api/sessions', jsonBody(body))
}
export async function exportEvent(id: string): Promise<void> {
  const response = await authFetch(`/api/events/${encodeURIComponent(id)}/export`)
  if (!response.ok) throw new Error('导出失败，请重试')
  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url; link.download = `event-${id}.json`
  document.body.appendChild(link); link.click(); link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export async function fetchEventDetail(eventId: string, signal?: AbortSignal): Promise<EventDetail> {
  return request(`/api/events/${encodeURIComponent(eventId)}`,{signal})
}
