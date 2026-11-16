import { authFetch } from './auth';

export interface EvidenceContext {
  annotations:{annotation_id:string;recording_id:string;segment_id:string;kind:string;original:{text:string;speaker:string|null;start_ms:number;end_ms:number};correction:string;actor:string;at:string}[];
  range:{started_at:string|null;ended_at:string|null};
  videos:{source_id:string;started_at:string;ended_at:string;note:string;actor:string}[];
  video_candidates:{source_id:string;name:string}[];
  audio:{recording_id:string;title:string;capture_started_at:string|null;window_start_ms:number;window_end_ms:number;duration_ms:number;playback_url:string;note:string;actor:string;segments?:{segment_id:string;speaker:string|null;start_ms:number;end_ms:number;text:string}[]}[];
  audio_candidates:{recording_id:string;title:string;capture_started_at:string;duration_ms:number}[];
  facts:{fact_id:string;type:string;person_id:string|null;person_name?:string|null;seat_id:string|null;seat_code?:string|null;occurred_at:string;registered_at:string;registered_after_event:boolean;note:string|null}[];
  authorization:{snapshot_id:string;at:string;phase_id:string|null;visibility:string;policy:string|null;entries:{person_id:string;name:string;role:string;identity_verified:boolean;recipient_authorized:boolean}[]}|null;
  identity_notice:string;
}
export interface SessionItem {
  session_id:string;name:string;status:string;phase_id:string;project_id:string;project_name:string;room_id:string;room_name:string;
  started_at:string;ended_at:string|null;channels:{source_id:string;name:string;kind:string;active:boolean}[];
  latest_report:{report_id:string;at:string;actor:string}|null;
}
export interface SessionReport {
  report_id:string;at:string;actor:string;snapshot:{session_name:string;project_name:string;room_name:string;report_type:string;started_at:string;ended_at:string;
    unresolved:number;notice:string;events:{event_id:string;reason:string;workflow_status:string;resolution:string|null}[];
    coverage:{source_id:string;name:string;metric:string;samples:number;intervals:{started_at:string;ended_at:string;status:string;seconds:number}[]}[]};
}
export async function downloadWorkspace(path:string,filename:string){
  const response=await authFetch('/api/supervision'+path);
  if(!response.ok){const body=await response.json().catch(()=>null);throw Error(body?.detail||'导出暂不可用，请重试');}
  const blob=await response.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=filename;a.click();window.setTimeout(()=>URL.revokeObjectURL(url),30000);
}
export function beijingInput(value:string|null|undefined){return value?new Date(Date.parse(value)+8*3600000).toISOString().slice(0,19):'';}
export function beijingIso(value:string){if(!value)throw Error('请填写北京时间');const date=new Date(value+'+08:00');if(!Number.isFinite(date.getTime()))throw Error('时间格式无效');return date.toISOString();}
export const healthLabels:Record<string,string>={online:'画面可连接',offline:'不可用',running:'近期收到分析结果',recent:'近期有媒体活动',stale:'近期无录像活动',unknown:'待核验',input_error:'分析输入异常',not_applicable:'不适用',unconfigured:'未配置'};
