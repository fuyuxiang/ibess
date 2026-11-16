import { create } from 'zustand'
import { fetchStageConfig, fetchStageControl, fetchStageTimeline } from '../api/stages'
import type { StageConfig, StageControl, StageTimeline } from '../api/stages'

export interface PhaseState {
  selectedSessionId: string | null
  config: StageConfig | null
  control: StageControl | null
  timeline: StageTimeline['items']
  loading: boolean
  error: string | null
  controlError: string | null
  timelineError: string | null
  needsConfiguration: boolean
  selectSession: (id: string | null) => Promise<void>
  refresh: () => Promise<void>
}
const message = (error: unknown) => error instanceof Error ? error.message : '阶段信息加载失败，请重试'
const empty = { config: null, control: null, timeline: [], loading: false, error: null,
  controlError: null, timelineError: null, needsConfiguration: false }
let sequence = 0
let controller: AbortController | null = null
let pending: Promise<void> | null = null

export const usePhaseStore = create<PhaseState>()((set, get) => ({
  ...empty, selectedSessionId: null,
  selectSession: (id) => {
    sequence++; controller?.abort(); controller = null; pending = null
    set({ ...empty, selectedSessionId: id?.trim() || null })
    return get().refresh()
  },
  refresh: () => {
    const id = get().selectedSessionId
    if (!id) return Promise.resolve()
    if (pending) return pending
    const current = ++sequence
    const requestController = new AbortController(); controller = requestController
    set({ ...empty, loading: true })
    const work = (async () => {
      const [configuration, confirmation, history] = await Promise.allSettled([
        fetchStageConfig(id, requestController.signal), fetchStageControl(id, requestController.signal),
        fetchStageTimeline(id, requestController.signal),
      ])
      if (current !== sequence || requestController.signal.aborted || get().selectedSessionId !== id) return
      let config: StageConfig | null = null
      let control: StageControl | null = null
      let error: string | null = null; let controlError: string | null = null; let timelineError: string | null = null
      let needsConfiguration = false
      let timeline: StageTimeline['items'] = []
      if (configuration.status === 'fulfilled') {
        const value = configuration.value
        if (value?.session_id !== id || value.contract_version !== '2.0.0' || !value.stage_snapshot_id
            || !Number.isInteger(value.phase_version) || value.phase_version < 1) error = '阶段响应场次或版本不匹配，请刷新'
        else config = value
      } else {
        const failure = configuration.reason
        needsConfiguration = failure?.status === 409 && failure?.code === 'PHASE_NOT_CONFIGURED'
        if (!needsConfiguration) error = message(failure)
      }
      if (confirmation.status === 'fulfilled' && config) {
        const value = confirmation.value
        if (value?.session_id !== id || value.stage_snapshot_id !== config.stage_snapshot_id
            || value.phase_version !== config.phase_version || value.phase_id !== config.phase_id) controlError = '控制快照或版本已变化，请刷新'
        else control = value
      } else if (confirmation.status === 'rejected') controlError = message(confirmation.reason)
      if (history.status === 'fulfilled') {
        const value = history.value
        if (value?.session_id !== id || !Array.isArray(value.items)
            || value.items.some(item => item.session_id !== id)) timelineError = '时间轴响应场次不匹配'
        else timeline = value.items
      } else timelineError = message(history.reason)
      set({ config, control, timeline, error, controlError, timelineError, needsConfiguration, loading: false })
    })().catch((error: unknown) => {
      if (current === sequence && !requestController.signal.aborted) set({ ...empty, error: message(error) })
    }).finally(() => {
      if (current === sequence) { pending = null; controller = null }
    })
    pending = work
    return work
  },
}))
