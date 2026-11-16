/** 从后端错误响应体提取可读文本：兼容 string / FastAPI 422 detail 数组 / 结构化对象。 */
export function readErrorDetail(body: unknown, status: number): string {
  const raw = body && typeof body === 'object' && 'detail' in body ? (body as { detail: unknown }).detail : null
  if (typeof raw === 'string' && raw.trim()) return raw
  if (Array.isArray(raw)) {
    const parts = raw.map((item) => {
      if (item && typeof item === 'object' && 'msg' in item) return String((item as { msg: unknown }).msg)
      return JSON.stringify(item)
    }).filter(Boolean)
    if (parts.length) return parts.join('；')
  }
  if (raw && typeof raw === 'object') {
    if ('message' in raw && raw.message) return String((raw as { message: unknown }).message)
    return JSON.stringify(raw).slice(0, 300)
  }
  return `请求失败（${status}）`
}
