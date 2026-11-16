export const riskColor: Record<string, string> = {
  high: 'red',
  medium: 'gold',
  low: 'blue',
  info: 'default',
}

export const riskText: Record<string, string> = {
  high: '高风险',
  medium: '中风险',
  low: '低风险',
  info: '信息',
}

export function formatTime(value?: string | null) {
  if (!value) return '--:--:--'
  return new Date(value).toLocaleTimeString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' })
}

export const eventTypeLabels: Record<string, string> = {
  whispering:'疑似交头接耳', workstation_visit:'疑似串岗', document_transfer:'疑似资料递接',
  frequent_absence:'频繁离席', joint_absence:'多人同时离席', unauthorized_entry:'疑似未授权人员进入',
  seat_vacancy:'工位空置', close_contact:'近距离接触待核查', document_carry_out:'疑似资料携离', order_motion:"现场活动异常待核查",
  crowd_gathering: '人员聚集', seat_absence: '异常离席', camera_blacked: '视频遮挡',
  video_offline: '设备离线', sensitive_word: '敏感词提示', transcript_text: '语音转写',
  long_talking: '持续讲话', audio_offline: '音频采集异常', person_detected: '人员检测',
  zone_intrusion: '限制区域越界',
  manual_observation: '人工现场观察', language_risk: '交流内容待核查', phone_call: '疑似通话', phone_capture: '疑似拍摄', phone_unstored: "入口携带手机", phone_detected: '检出手机',
}
export function formatDateTime(value?: string | null) {
  if (!value) return '—'
  return new Date(value).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' })
}

export function eventLabel(code: string): string {
  if (eventTypeLabels[code]) return eventTypeLabels[code]
  const scene = /^(?:semantic_)?s(0[1-9]|10)(?:[-_]|$)/i.exec(code)?.[1]
  return scene ? ({ '01':'通讯设备入场异常', '02':'疑似使用通讯设备', '03':'异常离席', '04':'身份与授权异常', '05':'跨区域活动异常', '06':'疑似倾向性施压', '07':'疑似保密风险', '08':'疑似拍摄或文件传播', '09':'现场秩序异常', '10':"音视频完整性异常" }[scene] || code) : code
}

// Display labels only; service status codes remain unchanged.
export const recognitionStatusLabels: Record<string,string> = {
  ready:'识别就绪',not_enabled:'本场未启用身份核查',camera_degraded:'画面异常，识别暂停',
  model_not_configured:'视觉模型未配置',binding_missing:'未绑定场次',
  zones_or_binding_missing:'区域或场次未配置',entry_line_or_binding_missing:'出入口线或场次未配置',
  entry_line_missing:'出入口线未配置',face_or_authorization_unverified:'身份或授权待核验',
  model_or_database_unavailable:'核验服务暂不可用',review_failed:'画面复核失败',
}
