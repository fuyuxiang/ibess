import type { EventItem } from "../../api/client";
import type { SupervisionAction } from "../../api/supervision";

export const workflowLabels: Record<string, string> = {
  PENDING: "待确认",
  SUPPLEMENT: "补充核查中",
  CONFIRMED: "待登记处置",
  ASSIGNED: "待处置反馈",
  FEEDBACK: "待复核",
  ARCHIVED: "已归档",
  LEGACY_CLOSED: "历史关闭待核查",
};
export const workflowColors: Record<string, string> = {
  PENDING: "orange",
  SUPPLEMENT: "gold",
  CONFIRMED: "blue",
  ASSIGNED: "processing",
  FEEDBACK: "purple",
  ARCHIVED: "default",
  LEGACY_CLOSED: "gold",
};
export const actionLabels: Record<SupervisionAction, string> = {
  confirm: "确认异常",
  supplement: "补充核查",
  dismiss: "排除并归档",
  assign: "登记处置任务",
  feedback: "提交处置反馈",
  return: "退回继续处置",
  archive: "复核归档",
  reopen: "重新核查 / 更正认定",
};
const transitions: Record<string, SupervisionAction[]> = {
  PENDING: ["confirm", "supplement", "dismiss"],
  SUPPLEMENT: ["confirm", "supplement", "dismiss"],
  CONFIRMED: ["assign", "reopen"],
  ASSIGNED: ["feedback", "assign", "reopen"],
  FEEDBACK: ["archive", "return", "reopen"],
  LEGACY_CLOSED: ["confirm", "supplement", "dismiss"],
  ARCHIVED: ["reopen"],
};
export const resolutionLabels: Record<string,string> = {confirmed:'确认异常',false_positive:'识别误报',authorized_exception:'授权例外',normal:'正常现场行为'};
export const evidenceLabels: Record<string,string> = {READY:'关联资料已保全',PARTIAL:'录像保全存在缺口',PENDING:'录像保全中',UNAVAILABLE:'关联资料不可用',NOT_APPLICABLE:'业务模拟，无真实现场资料'};
export function auditNote(value:string) {
  // 兼容历史更正记录的JSON说明；原始记录保留在导出资料中。
  const readable = value.replace(/原认定与处置：(\{.*\})；重新核查依据：([\s\S]*)$/,(_,before,reason)=>{
    try {
      const row = JSON.parse(before) as {status:string;resolution:string|null};
      return `原人工结论：${resolutionLabels[row.resolution || ''] || '尚未认定'}；原流程状态：${workflowLabels[row.status] || row.status}；重新核查依据：${reason}`;
    } catch { return `原认定与处置：${before}；重新核查依据：${reason}`; }
  });
  return readable.replace(/\b(PENDING|SUPPLEMENT|CONFIRMED|ASSIGNED|FEEDBACK|ARCHIVED|LEGACY_CLOSED)\b/g,code=>workflowLabels[code]);
}
export const factLabels:Record<string,string> = {stage_binding:'场次与阶段绑定',recipient_authorization:'接收对象授权依据',confidentiality_policy:'保密政策依据',information_visibility:'信息公开状态',clarification_permission:'澄清许可',authorization_context_missing:'授权业务上下文',binding_missing:'业务范围绑定'};
export function workflowStatus(event: EventItem) {
  return (
    event.workflow_status ||
    ({
      CONFIRMED: "CONFIRMED",
      FALSE_POSITIVE: "ARCHIVED",
      CLOSED: "LEGACY_CLOSED",
    }[event.status] ??
      "PENDING")
  );
}
export function allowedActions(event: EventItem) {
  if (event.category === "observation") return [];
  return transitions[workflowStatus(event)] || [];
}
export function workflowStep(event: EventItem) {
  if (event.category === "observation") return 0;
  const state = workflowStatus(event);
  return state === "ARCHIVED"
    ? 3
    : ["CONFIRMED", "ASSIGNED", "FEEDBACK"].includes(state)
      ? 2
      : 1;
}
export function overdue(event: EventItem) {
  return (
    workflowStatus(event) !== "ARCHIVED" &&
    Boolean(event.due_at && Date.parse(event.due_at) < Date.now())
  );
}
