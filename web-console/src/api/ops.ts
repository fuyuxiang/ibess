import { authFetch } from './auth'

export type OpsHealth = {
  ai_engine: 'online' | 'idle' | 'offline' | 'unknown'
  last_event_seconds: number | null
  streams_ready: number
  streams_total: number
  streams_missing: string[]
  pending: number
  checked_at: string
}

export async function fetchOpsHealth(): Promise<OpsHealth> {
  const response = await authFetch('/api/ops/health', { cache: 'no-store' })
  if (!response.ok) throw new Error('运行态势不可用')
  return response.json()
}
