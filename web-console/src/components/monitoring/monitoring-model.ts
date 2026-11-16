import type { EventItem } from '../../api/client'
import type { MonitoringSource } from '../../api/supervision'

/** Match the deployed camera aliases, never infer an event's session from its camera alone. */
export function channelId(value: string) {
  if (value === 'camera-real-01' || value === 'cam-ezviz') return 'cam-ezviz'
  const numbered = /^(?:camera-|cam)(\d+)$/.exec(value)
  return numbered ? 'cam' + (numbered[1].replace(/^0+/, '') || '0') : value
}
export function findEventSource(event: Pick<EventItem, 'session_id'|'camera_id'>, sources: MonitoringSource[]) {
  if (!event.session_id || !event.camera_id) return null
  const matches = sources.filter(source => source.kind !== 'audio' && source.session_id === event.session_id && channelId(source.source_id) === channelId(event.camera_id))
  return matches.length === 1 ? matches[0] : null
}
export function sourceIdentity(source: MonitoringSource) { return source.session_id + ':' + source.source_id }
export function sourceMatches(source: MonitoringSource, params: URLSearchParams) {
  return (!params.get('project_id') || source.project_id === params.get('project_id')) &&
    (!params.get('room_id') || source.room_id === params.get('room_id')) &&
    (!params.get('session_id') || source.session_id === params.get('session_id'))
}
export function pageForSource(sources: MonitoringSource[], id: string, capacity: number) {
  const index = sources.findIndex(source => sourceIdentity(source) === id)
  return index < 0 ? 0 : Math.floor(index / Math.max(1, capacity))
}
export function signalTone(status?: string) {
  return status === 'online' || status === 'running' || status === 'recent' ? 'ok'
    : status === 'offline' || status === 'input_error' || status === 'stale' ? 'bad' : 'unknown'
}
