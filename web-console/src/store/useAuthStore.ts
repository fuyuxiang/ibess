import { create } from 'zustand'
import { changeAdminPassword, configureAuthFetch, fetchAuthStatus, invalidateAuthRequests, loginAdmin, logoutAdmin, setupAdmin } from '../api/auth'
import type { AdminUser, ChangePasswordInput, LoginInput, SetupInput } from '../api/auth'
import { notifyWorkflowChange } from './useWorkflowStore'
import { useRegistryStore } from './useRegistryStore'

export interface AuthState {
  user: AdminUser | null
  initialized: boolean
  loading: boolean
  csrfToken: string | null
  checked: boolean
  error: string | null
  notice: string | null
  check: () => Promise<void>
  login: (input: LoginInput) => Promise<void>
  setup: (input: SetupInput) => Promise<void>
  logout: () => Promise<void>
  changePassword: (input: ChangePasswordInput) => Promise<void>
  expire: (reason?: string, broadcast?: boolean) => void
}

type SessionSignal = 'logout' | 'passwordchange' | 'expired'
const signalKey = 'eval-admin-session-signal'
let generation = 0
let checking: Promise<void> | null = null
let mutating = false
let channel: BroadcastChannel | null = null

function clearBusinessCaches() {
  notifyWorkflowChange()
  useRegistryStore.setState({ data: null, loading: false, error: null })
}
function announce(kind: SessionSignal) {
  const signal = { kind, at: Date.now(), nonce: Math.random().toString(36).slice(2) }
  if (channel) channel.postMessage(signal)
  else {
    try { localStorage.setItem(signalKey, JSON.stringify(signal)); localStorage.removeItem(signalKey) } catch { /* Browser storage can be unavailable. */ }
  }
}

export const useAuthStore = create<AuthState>((set, get) => ({
  user: null, initialized: false, loading: false, csrfToken: null, checked: false, error: null, notice: null,
  check: () => {
    if (checking) return checking
    const started = generation
    set({ loading: true, error: null })
    checking = fetchAuthStatus().then((status) => {
      if (started !== generation) return
      if (!status.user) clearBusinessCaches()
      set({ initialized: status.initialized, user: status.user, csrfToken: status.csrf_token, checked: true, loading: false, error: null })
    }).catch((error: unknown) => {
      if (started !== generation) return
      set({ loading: false, error: error instanceof Error ? error.message : '身份检查失败，请重试' })
    }).finally(() => { checking = null })
    return checking
  },
  login: async (input) => {
    if (mutating) return
    mutating = true
    const started = generation
    set({ loading: true, error: null, notice: null })
    try {
      const result = await loginAdmin(input)
      if (started !== generation) throw new Error('登录状态已变化，请重新登录')
      generation += 1
      invalidateAuthRequests()
      clearBusinessCaches()
      set({ user: result.user, csrfToken: result.csrf_token, initialized: true, checked: true })
    } finally { mutating = false; set({ loading: false }) }
  },
  setup: async (input) => {
    if (mutating) return
    mutating = true
    const started = generation
    set({ loading: true, error: null, notice: null })
    try {
      const result = await setupAdmin(input)
      if (started !== generation) throw new Error('登录状态已变化，请重新检查')
      generation += 1
      invalidateAuthRequests()
      clearBusinessCaches()
      set({ user: result.user, csrfToken: result.csrf_token, initialized: true, checked: true })
    } finally { mutating = false; set({ loading: false }) }
  },
  logout: async () => {
    if (mutating) return
    mutating = true
    set({ loading: true })
    try {
      await logoutAdmin()
      get().expire('已退出登录', false)
      announce('logout')
    } finally { mutating = false; set({ loading: false }) }
  },
  changePassword: async (input) => {
    if (mutating) return
    mutating = true
    set({ loading: true })
    try {
      await changeAdminPassword(input)
      get().expire('密码已修改，请使用新密码重新登录', false)
      announce('passwordchange')
    } finally { mutating = false; set({ loading: false }) }
  },
  expire: (reason = '登录已失效，请重新登录', broadcast = true) => {
    const hadSession = !!get().user
    generation += 1
    invalidateAuthRequests()
    set({ user: null, csrfToken: null, initialized: true, checked: true, loading: false, error: null, notice: reason })
    clearBusinessCaches()
    if (broadcast && hadSession) announce('expired')
  },
}))

configureAuthFetch({ getCsrfToken: () => useAuthStore.getState().csrfToken, onUnauthorized: () => useAuthStore.getState().expire() })

// 已结束的会话不能通过迟到的请求恢复业务配置缓存。
useRegistryStore.subscribe((state) => {
  if (!useAuthStore.getState().user && state.data) clearBusinessCaches()
})

function receiveSignal(value: unknown) {
  if (!value || typeof value !== 'object' || !('kind' in value)) return
  if (!['logout', 'passwordchange', 'expired'].includes(String(value.kind))) return
  useAuthStore.getState().expire(value.kind === 'passwordchange' ? '密码已在其他标签页修改，请重新登录' : '登录已在其他标签页结束，请重新登录', false)
}
if (typeof window !== 'undefined') {
  try {
    if (typeof BroadcastChannel !== 'undefined') {
      channel = new BroadcastChannel(signalKey)
      channel.onmessage = (event: MessageEvent<unknown>) => receiveSignal(event.data)
    }
  } catch { channel = null }
  window.addEventListener('storage', (event) => {
    if (event.key !== signalKey || !event.newValue) return
    try { receiveSignal(JSON.parse(event.newValue)) } catch { /* Ignore unrelated or malformed signals. */ }
  })
}
