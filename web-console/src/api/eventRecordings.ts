import { supervisionRequest } from './supervision'
export interface EventRecording {
  index:number; file_name:string; start_epoch:number; offset_seconds:number; duration_seconds:number;
  duration_estimated:boolean; contains_event:boolean; seek_seconds:number; preserved:boolean; playback_url:string
}
export interface EventRecordings {camera_id:string;occurred_at:string;event_covered:boolean;items:EventRecording[];total:number}
export function fetchEventRecordings(id:string,signal?:AbortSignal) {
  return supervisionRequest<EventRecordings>(`/events/${encodeURIComponent(id)}/recordings`,{signal})
}
