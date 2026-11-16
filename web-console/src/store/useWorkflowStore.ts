import { create } from 'zustand'

interface WorkflowState {
  revision: number
}

/** 仅通知当前页面刷新；业务数据由各查询 Hook 持有。 */
export const useWorkflowStore = create<WorkflowState>(() => ({ revision: 0 }))

export function notifyWorkflowChange() {
  useWorkflowStore.setState(state => ({ revision: state.revision + 1 }))
}
