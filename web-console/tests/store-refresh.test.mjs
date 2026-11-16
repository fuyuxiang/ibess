import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

const zustand = new URL('../node_modules/zustand/esm/index.mjs', import.meta.url).href
const source = readFileSync(new URL('../src/store/useWorkflowStore.ts', import.meta.url), 'utf8')
  .replace("from 'zustand'", `from '${zustand}'`)
const output = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 } }).outputText
const { useWorkflowStore, notifyWorkflowChange } = await import('data:text/javascript;base64,' + Buffer.from(output).toString('base64'))

test('业务变更通知所有当前订阅者，由页面自行刷新查询', () => {
  const revisions = []
  const unsubscribe = useWorkflowStore.subscribe(state => revisions.push(state.revision))
  const first = useWorkflowStore.getState().revision
  try {
    notifyWorkflowChange()
    notifyWorkflowChange()
    assert.deepEqual(revisions, [first + 1, first + 2])
  } finally { unsubscribe() }
})

test('取消订阅后不再收到业务通知，通知过程不请求旧统计接口', () => {
  let calls = 0
  const originalFetch = globalThis.fetch
  globalThis.fetch = () => { throw Error('刷新通知不应自行请求业务接口') }
  const unsubscribe = useWorkflowStore.subscribe(() => { calls++ })
  try {
    notifyWorkflowChange()
    unsubscribe()
    notifyWorkflowChange()
    assert.equal(calls, 1)
  } finally { unsubscribe(); globalThis.fetch = originalFetch }
})
