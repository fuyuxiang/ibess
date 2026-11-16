import { authFetch } from "./auth";
import type { EventItem } from "./client";
import { readErrorDetail } from "./errorText";

export interface SupervisionQuery {
  page?: number;
  page_size?: number;
  keyword?: string;
  project_id?: string;
  room_id?: string;
  session_id?: string;
  camera_id?: string;
  workflow_status?: string;
  queue?: "confirm" | "process" | "dispatch" | "review" | "active" | "archive";
  mine?: boolean;
  overdue?:boolean;
  category?: "behavior" | "technical" | "all";
  risk_level?: string;
  date_from?: string;
  date_to?: string;
  assignee?: string;
  resolution?: string;
}
export interface SupervisionResult {
  items: EventItem[];
  total: number;
  page: number;
  page_size: number;
  counts: Record<string, number>;
  summary: { confirm: number; process: number; archive: number };
}
export interface MonitoringSource {
  kind?: 'video'|'audio';
  source_name?: string;
  binding_id: string;
  project_id: string;
  project_name: string;
  room_id: string;
  room_name: string;
  session_id: string;
  session_name: string;
  session_status: string;
  phase_id: string;
  source_id: string;
  hls_url: string | null;
}
export interface MonitoringHealth {
  items: (MonitoringSource & {
    stream_status: string;
    analysis_status: string;
    recording_status: string;
    audio_status?: string;
    analysis_at?: string|null;
    status_basis?: string;
  })[];
  total: number;
  online: number;
  checked_at: string;
}
export type SupervisionAction =
  | "confirm"
  | "supplement"
  | "dismiss"
  | "assign"
  | "feedback"
  | "return"
  | "reopen"
  | "archive";
export interface Operation {
  action: SupervisionAction;
  note: string;
  expected_status: string;
  expected_version: number;
  request_id: string;
  assignee?: string;
  due_at?: string;
  resolution?: string;
  acknowledge_missing_evidence?: boolean;
  evidence_note?: string;
}
export async function supervisionRequest<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const controller = new AbortController();
  const external = init?.signal;
  const abort = () => controller.abort();
  if (external?.aborted) controller.abort();
  else external?.addEventListener('abort',abort,{once:true});
  const timer = window.setTimeout(() => controller.abort(), 20000);
  try {
    const response = await authFetch(`/api${path}`, {
      ...init,
      signal: controller.signal,
    });
    const body: unknown = await response.json().catch(() => null);
    if (!response.ok) throw new Error(readErrorDetail(body, response.status));
    if (body === null) throw new Error("服务返回数据无效，请重试");
    return body as T;
  } catch (error) {
    if (controller.signal.aborted)
      throw new Error("请求超时；提交操作可能已保存，请刷新核对或重试原操作");
    if (error instanceof TypeError)
      throw new Error("无法连接服务，请检查网络并重试");
    throw error;
  } finally {
    window.clearTimeout(timer);
    external?.removeEventListener('abort',abort);
  }
}
export function querySupervision(params: SupervisionQuery = {}, signal?: AbortSignal) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== "") query.set(key, String(value));
  });
  return supervisionRequest<SupervisionResult>(`/supervision/events?${query}`,{signal});
}
export function fetchSources(signal?:AbortSignal) {
  return supervisionRequest<{ items: MonitoringSource[] }>(
    "/supervision/sources", {signal},
  );
}
export function fetchMonitoringHealth(signal?:AbortSignal) {
  return supervisionRequest<MonitoringHealth>("/supervision/health",{signal});
}
export function operateEvent(id: string, body: Operation) {
  return supervisionRequest<{
    workflow_status: string;
    workflow_version: number;
  }>(`/supervision/events/${encodeURIComponent(id)}/actions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
export async function downloadEvidencePackage(id: string) {
  const response = await authFetch(
    `/api/events/${encodeURIComponent(id)}/evidence-package`,
  );
  if (!response.ok)
    throw new Error(
      readErrorDetail(await response.json().catch(() => null), response.status),
    );
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `evidence-${id}.zip`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
