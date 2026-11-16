import { authFetch } from './auth'
export interface RosterPerson {
  person_id:string; name:string; role:string; seat_id:string|null; seat_code:string|null; fact_id:string
  registered:boolean; enabled:boolean; verified:boolean; verification_result_id:string|null; recipient_authorized:boolean
  verifications:{result_id:string;checked_at:string;source:string;similarity:number|null}[]
}
export interface RoomRoster {
  room_id:string;project_id:string;version:string;items:RosterPerson[];needs_confirmation:boolean;identity_verification_enabled:boolean
  confirmed_at:string|null;confirmed_by:string|null;information_visibility:'public'|'confidential';confidentiality_policy_id:string|null
  history:{at:string;actor:string;count:number;current:boolean}[]
}
export interface RosterConfirmation {
  expected_version:string;session_id?:string;selections:{person_id:string;verification_result_id:string}[];recipients:string[]
  information_visibility:'public'|'confidential';confidentiality_policy_id?:string
}
async function request<T>(path:string,init?:RequestInit):Promise<T>{
  const response=await authFetch('/api/rosters/'+path,init)
  const data=await response.json()
  if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'名单操作失败，请刷新核对')
  return data
}
export function fetchRoster(roomId:string,signal?:AbortSignal,sessionId?:string){return request<RoomRoster>(encodeURIComponent(roomId)+(sessionId?'?session_id='+encodeURIComponent(sessionId):''),{signal})}
export function confirmRoster(roomId:string,body:RosterConfirmation){return request<RoomRoster>(encodeURIComponent(roomId)+'/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})}
