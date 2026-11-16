import { useEffect } from 'react'
import { create } from 'zustand'
import { fetchRegistry } from '../api/registry'
import type { RegistrySnapshot } from '../api/registry'

export interface RegistryState {
  data: RegistrySnapshot | null
  loading: boolean
  error: string | null
  /** force=true ensures a mutation is followed by a new snapshot even during an active read. */
  refresh: (force?: boolean) => Promise<void>
}

let activeRequest: Promise<void> | null = null
let queuedRefresh: Promise<void> | null = null
let loadedAt = 0

export const useRegistryStore = create<RegistryState>((set, get) => ({
  data: null,
  loading: false,
  error: null,
  refresh: (force = false) => {
    if (activeRequest) {
      if (!force) return activeRequest
      // One extra read after the active request prevents reusing a pre-save snapshot.
      if (!queuedRefresh) {
        queuedRefresh = activeRequest.then(() => {
          queuedRefresh = null
          return get().refresh()
        })
      }
      return queuedRefresh
    }
    set({ loading: true, error: null })
    activeRequest = fetchRegistry().then((data) => {
      loadedAt = Date.now()
      set({ data, error: null })
    }).catch((error: unknown) => {
      set({ error: error instanceof Error ? error.message : '基础配置加载失败，请重试' })
    }).finally(() => {
      activeRequest = null
      set({ loading: false })
    })
    return activeRequest
  },
}))

/** Shared snapshot with a 15-second mount cache and in-flight request deduplication. */
export function useRegistry(): RegistryState {
  const state = useRegistryStore()
  const refresh = state.refresh
  useEffect(() => {
    const current = useRegistryStore.getState()
    if (!current.data || current.error || Date.now() - loadedAt > 15000) void refresh()
  }, [refresh])
  return state
}
