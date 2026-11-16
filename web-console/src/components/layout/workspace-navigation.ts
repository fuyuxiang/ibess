export function workspaceMode(path: string) {
  if (['/', '/overview', '/monitor'].includes(path)) return 'duty'
  if (path === '/admin' || path.startsWith('/admin/')) return 'admin'
  if (path === '/sessions' || path.startsWith('/sessions/')) return 'sessions'
  return 'operations'
}

export function activeWorkspace(path: string) {
  const mode = workspaceMode(path)
  if (mode === 'duty') return '/overview'
  if (mode === 'admin') return '/admin'
  if (mode === 'sessions') return '/sessions'
  if (path === '/archives' || path === '/evidence') return '/archives'
  return '/events'
}

export function returnLabel(path: string) {
  const pathname = path.split('?')[0]
  if (workspaceMode(pathname) === 'duty') return '返回现场值守'
  if (pathname === '/archives') return '返回监管档案'
  if (workspaceMode(pathname) === 'sessions') return '返回监管场次'
  return '返回告警队列'
}
