import type { RegistryRoom, RegistrySnapshot } from '../api/registry'

export function activeRooms(data: RegistrySnapshot | null): RegistryRoom[] {
  if (!data) return []
  return data.rooms.filter((room) => room.enabled && data.projects.some((project) => project.project_id === room.project_id && project.status !== 'closed')).sort((a, b) => {
    const projectA = data.projects.find((project) => project.project_id === a.project_id)?.code || ''
    const projectB = data.projects.find((project) => project.project_id === b.project_id)?.code || ''
    return projectA.localeCompare(projectB) || a.code.localeCompare(b.code, 'zh-CN', { numeric: true })
  })
}

export function roomLabel(room: RegistryRoom, data: RegistrySnapshot | null): string {
  const project = data?.projects.find((item) => item.project_id === room.project_id)
  return `${project ? `${project.name}（${project.code}）` : '未记录项目'} / ${room.name}（${room.code}）`
}
