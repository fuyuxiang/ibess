import { supervisionRequest } from './supervision'
import type { DrawnZone, EntryLine } from './zones'
export interface RoomChannel {
  source_id:string;name:string;kind:'video'|'audio';zones:DrawnZone[];entry_lines:EntryLine[]
}
export interface RoomPreset {
  preset_id:string;room_id:string;version:number;scheme:'basic'|'standard';primary_source_id:string;
  channels:RoomChannel[];note:string;created_by:string;created_at:string
}
export const schemeLabels={basic:'基础行为监管',standard:'完整现场监管'}
export function fetchRoomPresets(roomId:string,signal?:AbortSignal){
  return supervisionRequest<{items:RoomPreset[]}>(`/supervision/room-presets/${encodeURIComponent(roomId)}`,{signal})
}
export function saveRoomPreset(roomId:string,body:{expected_version:number;scheme:'basic'|'standard';primary_source_id:string;channels:RoomChannel[];note:string}){
  return supervisionRequest<RoomPreset>(`/supervision/room-presets/${encodeURIComponent(roomId)}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
}
