import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, existsSync } from 'node:fs'
import ts from 'typescript'

function url(code) { return 'data:text/javascript;base64,' + Buffer.from(code).toString('base64') }
async function loadApi() {
  const file = new URL('../src/api/zones.ts', import.meta.url)
  assert.ok(existsSync(file), '3.3区域客户端尚未实现')
  const source = readFileSync(file, 'utf8').replace("from './auth'", `from '${url('export const authFetch=(...args)=>globalThis.__zoneFetch(...args)')}'`)
  const output = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext } }).outputText
  return import(url(output + '\n//' + Math.random()))
}

test('区域API使用同源鉴权，编码场次/源ID并发送版本化归一化配置', async () => {
  const api = await loadApi(); const calls = []
  globalThis.__zoneFetch = async (path, options) => { calls.push({ path, options }); return Response.json({ config_version: 1 }) }
  await api.fetchZoneConfig('session/1', 'camera / A')
  const body = { config_version: 0, zones: [], entry_lines: [] }
  await api.saveZoneConfig('session/1', 'camera / A', body)
  assert.deepEqual(calls.map(call => call.path), [
    '/api/sessions/session%2F1/sources/camera%20%2F%20A/zones',
    '/api/sessions/session%2F1/sources/camera%20%2F%20A/zones',
  ])
  assert.equal(calls[0].options.method, undefined)
  assert.equal(calls[1].options.method, 'PUT')
  assert.deepEqual(JSON.parse(calls[1].options.body), body)
  assert.equal(new Headers(calls[1].options.headers).get('Content-Type'), 'application/json')
})

test('区域API拒绝空场次和空源', async () => {
  const api = await loadApi()
  await assert.rejects(api.fetchZoneConfig('', 'camera'), /场次/)
  await assert.rejects(api.fetchZoneConfig('session', '  '), /视频源/)
})

test('区域API向调用方保留版本冲突和校验错误状态', async () => {
  const api = await loadApi()
  for (const [status, code] of [[409, 'VERSION_CONFLICT'], [422, 'INVALID_ZONE'], [503, 'ZONE_STORAGE_UNAVAILABLE']]) {
    globalThis.__zoneFetch = async () => Response.json({ detail: { code, message: '区域请求失败' } }, { status })
    await assert.rejects(api.fetchZoneConfig('session', 'camera'), error => error.status === status && error.code === code)
  }
})

test('区域API传递AbortSignal并支持响应超时', async () => {
  const api = await loadApi(); const controller = new AbortController(); let received
  globalThis.__zoneFetch = async (_path, options) => {
    received = options.signal
    return new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true }))
  }
  const pending = api.fetchZoneConfig('session', 'camera', controller.signal)
  controller.abort()
  await assert.rejects(pending, error => error.name === 'AbortError')
  assert.ok(received.aborted)
  await assert.rejects(api.fetchZoneConfig('session', 'camera', undefined, 5), error => error.code === 'REQUEST_TIMEOUT')
})
