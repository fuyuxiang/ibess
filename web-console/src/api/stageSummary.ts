import { authFetch } from './auth'

export type StagePhase = {
  phase_id: string
  title: string
  state: 'done' | 'current' | 'pending'
  entered_at: string | null
  left_at: string | null
  event_count: number
  high: number
  medium: number
  low: number
  pending: number
  confirmed: number
  closed: number
  false_positive: number
  version: number | null
  scenes: string[]
  rule_versions: Record<string, string>
  reason: string | null
  actor: string | null
}

export type StageSummary = {
  project: { code: string | null; name: string | null; owner: string | null; room_name: string | null } | null
  session: {
    session_id: string
    name: string
    status: string
    current_phase: string
    current_title: string
    start_at: string | null
    expected_people?: number | null
  } | null
  phases: StagePhase[]
  total: { events: number; high: number; medium: number; pending: number; confirmed: number; closed: number; false_positive: number }
}

export async function fetchStageSummary(): Promise<StageSummary> {
  const response = await authFetch('/api/stages/summary')
  if (!response.ok) throw new Error(`阶段汇总读取失败（HTTP ${response.status}）`)
  return response.json()
}
