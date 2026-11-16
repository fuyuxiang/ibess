import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

const source = readFileSync(new URL('./workflow-model.ts', import.meta.url), 'utf8')
const output = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 } }).outputText
const model = await import(`data:text/javascript;base64,${Buffer.from(output).toString('base64')}`)

test('请求标识唯一且支持没有 randomUUID 的 HTTP 演示环境', () => {
  assert.equal(typeof model.newRequestId, 'function')
  const ids = Array.from({ length: 100 }, () => model.newRequestId())
  assert.equal(new Set(ids).size, ids.length)
  assert.match(ids[0], /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  const cryptoDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'crypto')
  try {
    Object.defineProperty(globalThis, 'crypto', { configurable: true, value: { getRandomValues: (bytes) => { for (let i = 0; i < bytes.length; i++) bytes[i] = i; return bytes } } })
    assert.match(model.newRequestId(), /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  } finally { Object.defineProperty(globalThis, 'crypto', cryptoDescriptor) }
})

test('错误提示可读取异常、字符串和结构化响应，未知内容显示通用提示', () => {
  assert.equal(model.errorText(new Error('连接中断')), '连接中断')
  assert.equal(model.errorText('请重试'), '请重试')
  assert.equal(model.errorText({ message: '配置无效', code: 'INVALID' }), '配置无效')
  assert.equal(model.errorText({ code: 'CONFLICT' }), '请求失败（CONFLICT）')
  assert.equal(model.errorText(null), '请求失败，请刷新后重试')
})
