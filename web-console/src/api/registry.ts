import { authFetch } from './auth'

export type ProjectStatus = 'draft' | 'running' | 'closed'
export type RoomMode = 'centralized' | 'remote'
export type PersonRole = 'expert' | 'supervisor' | 'staff' | 'owner' | 'agent' | 'bidder'

export interface RegistryProject {
  project_id: string
  code: string
  name: string
  owner: string | null
  start_at: string | null
  end_at: string | null
  status: ProjectStatus
}
export interface RegistryRoom {
  room_id: string
  project_id: string
  code: string
  name: string
  location: string | null
  mode: RoomMode
  enabled: boolean
}
export interface RegistryPerson {
  person_id: string
  code: string
  name: string
  unit: string
  role: PersonRole
  enabled: boolean
}
export interface RegistrySeat {
  seat_id: string
  room_id: string
  project_id: string
  code: string
  person_id: string | null
  enabled: boolean
}
export interface RegistryParticipant {
  membership_id: string
  project_id: string
  person_id: string
  role: PersonRole
}
export interface RegistrySnapshot {
  projects: RegistryProject[]
  rooms: RegistryRoom[]
  persons: RegistryPerson[]
  seats: RegistrySeat[]
  participants: RegistryParticipant[]
}
export type ProjectInput = Omit<RegistryProject, 'project_id'>
export type RoomInput = Omit<RegistryRoom, 'room_id'>
export type PersonInput = Omit<RegistryPerson, 'person_id'>
export type SeatInput = Omit<RegistrySeat, 'seat_id' | 'project_id'>
export type ParticipantInput = Omit<RegistryParticipant, 'membership_id'>

export interface RegistryEntities {
  projects: RegistryProject
  rooms: RegistryRoom
  persons: RegistryPerson
  seats: RegistrySeat
  participants: RegistryParticipant
}
export interface RegistryInputs {
  projects: ProjectInput
  rooms: RoomInput
  persons: PersonInput
  seats: SeatInput
  participants: ParticipantInput
}
export type RegistryEntity = keyof RegistryEntities

function responseError(body: unknown, status: number): string {
  if (body && typeof body === 'object') {
    const record = body as Record<string, unknown>
    const detail = record.detail ?? record.message
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) return detail.map((item: unknown) => {
      if (typeof item === 'string') return item
      if (item && typeof item === 'object' && 'msg' in item) return String(item.msg)
      return '输入参数无效'
    }).join('；')
  }
  return `操作失败（HTTP ${status}），请重试`
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const controller = new AbortController()
  const timer = globalThis.setTimeout(() => controller.abort(), 15000)
  try {
    const response = await authFetch(`/api/registry${path}`, { ...options, signal: controller.signal })
    const body: unknown = await response.json().catch(() => null)
    if (!response.ok) throw new Error(responseError(body, response.status))
    return body as T
  } catch (error) {
    if (controller.signal.aborted) throw new Error('请求超时，请刷新后确认结果')
    if (error instanceof TypeError) throw new Error('无法连接后端，请检查网络后重试')
    throw error
  } finally { globalThis.clearTimeout(timer) }
}

export function fetchRegistry(): Promise<RegistrySnapshot> { return request('') }
export function createRegistryItem<K extends RegistryEntity>(entity: K, input: RegistryInputs[K]): Promise<RegistryEntities[K]> {
  return request(`/${entity}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input) })
}
export function updateRegistryItem<K extends RegistryEntity>(entity: K, id: string, input: RegistryInputs[K]): Promise<RegistryEntities[K]> {
  return request(`/${entity}/${encodeURIComponent(id)}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input) })
}
export function deleteRegistryItem(entity: RegistryEntity, id: string): Promise<{ ok: boolean }> {
  return request(`/${entity}/${encodeURIComponent(id)}`, { method: 'DELETE' })
}
