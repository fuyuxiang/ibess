import { supervisionRequest } from './supervision'
import type { RoomPreset } from './roomMonitoring'
export const lifecycleLabels: Record<string,string> = { preparing:'评前准备', running:'正在监管', closing:"待归档", archived:'已归档', supplement_required:"待补充归档", unmanaged:'待纳入流程' }
export const lifecycleColors: Record<string,string> = { preparing:'blue', running:'green', closing:'orange', archived:'default', supplement_required:'red', unmanaged:'default' }
export interface SessionRow {
  session_id:string; name:string; status:string; lifecycle:string; project_name:string; room_name:string; room_id?:string|null; phase_id:string;
  started_at:string|null; ended_at:string|null; pending_events:number; total_events:number
}
export interface SessionDirectory {items:SessionRow[];total:number;page:number;page_size:number;summary:{preparing:number;running:number;closed:number}}
export interface WorkflowCheck {key:string;label:string;passed:boolean;required?:boolean;action?:string;manual?:boolean;wait_seconds?:number}
export interface SessionWorkflow extends SessionRow {
  version:number; managed:boolean; project_id:string|null;room_id:string|null; phase_version:number;started_by:string|null;
  identity_verification_enabled:boolean;
  monitoring_scheme:string;monitoring_scheme_label:string;preparation_snapshot:(Partial<RoomPreset>&{profile_version?:string})|null;
  archived_report_id:string|null;archived_at:string|null;media_pending:number;
  checks:Record<string,{confirmed:boolean;actor:string;at:string}>;
  readiness:{items:WorkflowCheck[];ready:boolean;roster_count:number;seat_count:number;entrance_count:number};
  archive_checks:WorkflowCheck[];
  channels:{source_id:string;name:string;kind:string;active:boolean}[];
  control:{sync_status:string;phase_version:number;required_consumers:string[];acknowledged_consumers:string[]}|null;
  phases:{id:string;title:string;scenes:string[]}[];
  journal:{journal_id:string;kind:string;note:string;actor:string;at:string;detail:Record<string,unknown>}[];
  runtime_notice:string
}
export const workflowPath = (id:string) => `/supervision/workflows/${encodeURIComponent(id)}`
export function fetchWorkflow(id:string,signal?:AbortSignal) { return supervisionRequest<SessionWorkflow>(workflowPath(id),{signal}) }
export function workflowWrite<T=SessionWorkflow>(id:string,action:string,body:unknown,method='POST') {
  return supervisionRequest<T>(`${workflowPath(id)}/${action}`,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
}
