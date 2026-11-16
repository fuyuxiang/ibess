import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, existsSync } from 'node:fs'
import ts from 'typescript'
import { renderToStaticMarkup } from 'react-dom/server'
import { createElement } from 'react'

function url(code) { return 'data:text/javascript;base64,' + Buffer.from(code).toString('base64') }
async function load(path, replacements = {}) {
  const file = new URL(path, import.meta.url)
  assert.ok(existsSync(file), `任务3.1尚未提供 ${path}`)
  let source = readFileSync(file, 'utf8')
  for (const [from, to] of Object.entries(replacements)) source = source.replaceAll(from, to)
  let output = ts.transpileModule(source, { compilerOptions: {
    target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText
  output = output.replace('"react/jsx-runtime"', `"${new URL('../node_modules/react/jsx-runtime.js', import.meta.url).href}"`)
  return import(url(output + '\n// ' + Math.random()))
}
async function api() {
  return load('../src/api/stages.ts', { "from './auth'": `from '${url('export const authFetch=(...a)=>globalThis.__phaseFetch(...a)')}'` })
}
const config = (session = 's1', version = 1) => ({ contract_version: '2.0.0', project_id: 'p', room_id: 'r',
  session_id: session, source_id: 'camera', stage_snapshot_id: `snap-${session}-${version}`, phase_id: 'DETAIL',
  phase_version: version, effective_at: '2026-10-14T02:00:00Z', enabled_scenes: ['S03'], always_on_scenes: ['S10'],
  assets: [], exceptions: [], rule_versions: { S03: 'v1', S10: 'v1' }, sync_status: 'pending' })
const control = (session = 's1', version = 1, sync = 'pending', delivery = 'published') => ({
  session_id: session, stage_snapshot_id: `snap-${session}-${version}`, phase_id: 'DETAIL', phase_version: version,
  required_consumers: ['media', 'event'], acknowledged_consumers: [], sync_status: sync, delivery_status: delivery,
  attempts: 1, last_error_code: null })
const tick = async () => { await new Promise(resolve => setImmediate(resolve)) }

test('客户端只调用同源authFetch，编码场次ID，写入不传actor或消费者凭据', async () => {
  const a = await api(); const calls = []
  globalThis.__phaseFetch = async (path, options) => { calls.push({ path, options }); return Response.json({}) }
  await a.fetchStageSessions(); await a.fetchStageConfig('s/1'); await a.fetchStageControl('s/1'); await a.fetchStageTimeline('s/1')
  await a.bindStageSession('s/1', { project_id: 'p', room_id: 'r', source_id: 'camera' })
  const body = { phase: 'REPORT', expected_version: 1, enabled_scenes: [], rule_versions: { S10: 'v1' }, reason: '交接' }
  await a.saveStageConfig('s/1', body); await a.changeStage('s/1', body)
  assert.deepEqual(calls.map(c => c.path), ['/api/sessions', '/api/sessions/s%2F1/phase-config',
    '/api/sessions/s%2F1/control-status', '/api/sessions/s%2F1/timeline', '/api/sessions/s%2F1/binding',
    '/api/sessions/s%2F1/phase-config', '/api/sessions/s%2F1/phase'])
  assert.deepEqual(calls.slice(4).map(c => c.options.method), ['PUT', 'PUT', 'PATCH'])
  assert.deepEqual(JSON.parse(calls[6].options.body), body)
  await assert.rejects(a.fetchStageConfig('  '), /场次/)
})

test('401/409保留状态与业务code；不把故障当未配置', async () => {
  const a = await api()
  for (const [status, code] of [[401, 'UNAUTHORIZED'], [409, 'VERSION_CONFLICT'], [503, 'PHASE_STORAGE_UNAVAILABLE']]) {
    globalThis.__phaseFetch = async () => Response.json({ detail: { code, message: '安全错误说明' } }, { status })
    await assert.rejects(a.fetchStageConfig('s1'), e => e.status === status && e.code === code && e.message === '安全错误说明')
  }
})

test('外部取消信号和超时有界，不吞掉外部AbortError', async () => {
  const a = await api(); const controller = new AbortController()
  globalThis.__phaseFetch = async (_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true })
  })
  const request = a.fetchStageConfig('s1', controller.signal); controller.abort()
  await assert.rejects(request, e => e.name === 'AbortError')
  await assert.rejects(a.fetchStageConfig('s1', undefined, 5), e => e.code === 'REQUEST_TIMEOUT')
})

test('状态标签区分配置/发送/应用；版本不符或确认集合不齐绝不显示已应用', async () => {
  const a = await api(); const c = config()
  assert.equal(a.describePhaseStatus(null, null).label, '请选择场次')
  assert.equal(a.describePhaseStatus(c, null).label, '应用状态待确认')
  assert.equal(a.describePhaseStatus(c, control('s1', 1, 'pending', 'pending')).label, '待发送')
  assert.equal(a.describePhaseStatus(c, control()).label, '已发送，待应用')
  assert.equal(a.describePhaseStatus(c, control('s1', 1, 'missing_asset')).label, '素材缺失')
  assert.equal(a.describePhaseStatus(c, control('s1', 1, 'failed')).label, '应用失败')
  assert.equal(a.describePhaseStatus(c, control('s1', 1, 'partially_applied')).label, '部分应用')
  const ok = { ...control('s1', 1, 'applied'), acknowledged_consumers: ['event', 'media'] }
  assert.equal(a.describePhaseStatus(c, ok).label, '已应用')
  for (const invalid of [control('s1', 1, 'applied'), { ...ok, required_consumers: [] },
    { ...ok, stage_snapshot_id: 'wrong' }, { ...ok, session_id: 's2' }, { ...ok, phase_version: 2 }]) {
    assert.notEqual(a.describePhaseStatus(c, invalid).label, '已应用')
  }
})

async function store() {
  const a = await api()
  const methods = 'fetchStageConfig,fetchStageControl,fetchStageTimeline'.split(',')
  const fake = url(methods.map(n => `export const ${n}=(...args)=>globalThis.__phaseApi.${n}(...args);`).join('') +
    'export class StageApiError extends Error {constructor(message,status,code){super(message);this.status=status;this.code=code}}')
  return { a, ...(await load('../src/store/usePhaseStore.ts', {
    "from 'zustand'": `from '${new URL('../node_modules/zustand/esm/index.mjs', import.meta.url).href}'`,
    "from '../api/stages'": `from '${fake}'`,
  })) }
}
const successApi = () => ({ fetchStageConfig: async s => config(s), fetchStageControl: async s => control(s),
  fetchStageTimeline: async s => ({ session_id: s, items: [] }) })

test('未选择场次不发请求，不默认选最新场次或自动创建', async () => {
  let calls = 0; globalThis.__phaseApi = new Proxy({}, { get: () => () => { calls++; return Promise.resolve({}) } })
  const { usePhaseStore } = await store()
  await usePhaseStore.getState().refresh()
  assert.equal(calls, 0); assert.equal(usePhaseStore.getState().selectedSessionId, null)
})

test('切换场次取消旧请求，即便旧接口忽略取消返回也不能覆盖新场次', async () => {
  const pending = []; globalThis.__phaseApi = Object.fromEntries(['fetchStageConfig', 'fetchStageControl', 'fetchStageTimeline'].map(method =>
    [method, (id, signal) => new Promise(resolve => pending.push({ method, id, signal, resolve }))]))
  const { usePhaseStore } = await store()
  const first = usePhaseStore.getState().selectSession('s1'); await tick()
  const second = usePhaseStore.getState().selectSession('s2'); await tick()
  assert.equal(pending.filter(p => p.id === 's1').every(p => p.signal.aborted), true)
  const reply = p => p.method === 'fetchStageConfig' ? config(p.id) : p.method === 'fetchStageControl' ? control(p.id) : { session_id: p.id, items: [] }
  pending.filter(p => p.id === 's2').forEach(p => p.resolve(reply(p))); await second
  pending.filter(p => p.id === 's1').forEach(p => p.resolve(reply(p))); await first
  assert.equal(usePhaseStore.getState().config.session_id, 's2')
  assert.equal(usePhaseStore.getState().loading, false)
  await usePhaseStore.getState().selectSession(null)
  assert.equal(usePhaseStore.getState().config, null)
})

test('并发刷新合并，配置/控制版本不匹配单独标错', async () => {
  let count = 0; let release
  globalThis.__phaseApi = { ...successApi(), fetchStageConfig: s => { count++; return new Promise(r => { release = () => r(config(s)) }) },
    fetchStageControl: async s => control(s, 2, 'applied') }
  const { usePhaseStore } = await store()
  const first = usePhaseStore.getState().selectSession('s1'); await tick()
  const second = usePhaseStore.getState().refresh(); release(); await Promise.all([first, second])
  assert.equal(count, 1); assert.equal(usePhaseStore.getState().control, null)
  assert.match(usePhaseStore.getState().controlError, /版本|快照/)
})

test('未配置与服务失败分开处理，失败刷新清掉旧已应用状态', async () => {
  globalThis.__phaseApi = successApi(); const { usePhaseStore } = await store()
  await usePhaseStore.getState().selectSession('s1')
  globalThis.__phaseApi = { ...successApi(), fetchStageControl: async () => { throw new Error('服务暂不可用') } }
  await usePhaseStore.getState().refresh()
  assert.equal(usePhaseStore.getState().control, null)
  assert.match(usePhaseStore.getState().controlError, /暂不可用/)
  const failure = Object.assign(new Error('未配置'), { status: 409, code: 'PHASE_NOT_CONFIGURED' })
  globalThis.__phaseApi = { ...successApi(), fetchStageConfig: async () => { throw failure } }
  await usePhaseStore.getState().refresh()
  assert.equal(usePhaseStore.getState().needsConfiguration, true)
  assert.equal(usePhaseStore.getState().config, null)
  assert.equal(usePhaseStore.getState().control, null)
})

test('跨场次响应不进入状态，失败保留重试入口', async () => {
  globalThis.__phaseApi = { ...successApi(), fetchStageConfig: async () => config('other') }
  const { usePhaseStore } = await store(); await usePhaseStore.getState().selectSession('s1')
  assert.equal(usePhaseStore.getState().config, null)
  assert.match(usePhaseStore.getState().error, /场次/)
  globalThis.__phaseApi = successApi(); await usePhaseStore.getState().refresh()
  assert.equal(usePhaseStore.getState().config.session_id, 's1')
})

test('PhaseBadge输出可读阶段/版本/状态文本，失败时不显示已应用', async () => {
  // 组件纯props展示，使用实际模块导出避免生产样式依赖进入Node。
  const actualApiSource = readFileSync(new URL('../src/api/stages.ts', import.meta.url), 'utf8')
    .replace("from './auth'", `from '${url('export const authFetch=()=>{}')}'`)
  const realApiUrl = url(ts.transpileModule(actualApiSource, { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText)
  const { default: PhaseBadge } = await load('../src/components/stages/PhaseBadge.tsx', {
    "from '../../api/stages'": `from '${realApiUrl}'`, "import '../../phase-badge.css'": '',
    '"react/jsx-runtime"': `"${new URL('../node_modules/react/jsx-runtime.js', import.meta.url).href}"`,
  })
  const html = renderToStaticMarkup(createElement(PhaseBadge, { config: config(), control: control(), loading: false }))
  assert.match(html, /详细评审/); assert.match(html, /v1/); assert.match(html, /待应用/)
  assert.doesNotMatch(renderToStaticMarkup(createElement(PhaseBadge, { config: config(), control: control(), error: '连接失败' })), /已应用/)
})

test('实际authFetch保留401失效回调并为写请求附带CSRF，不携带消费者token', async () => {
  const previousLocation = globalThis.location; const previousFetch = globalThis.fetch
  const auth = await load('../src/api/auth.ts')
  const actualAuthSource = readFileSync(new URL('../src/api/auth.ts', import.meta.url), 'utf8')
  // 单一模块URL让configureAuthFetch与被测客户端使用同一个认证状态。
  const authUrl = url(ts.transpileModule(actualAuthSource, { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText)
  const actualAuth = await import(authUrl)
  const a = await load('../src/api/stages.ts', { "from './auth'": `from '${authUrl}'` })
  let expired = 0; const calls = []
  globalThis.location = { origin: 'http://localhost:3000' }
  actualAuth.configureAuthFetch({ getCsrfToken: () => 'test-csrf', onUnauthorized: () => { expired++ } })
  try {
    globalThis.fetch = async (path, options) => { calls.push({ path, options }); return Response.json({ detail: '登录已失效' }, { status: 401 }) }
    await assert.rejects(a.fetchStageConfig('s1'), e => e.status === 401)
    assert.equal(expired, 1)
    globalThis.fetch = async (path, options) => { calls.push({ path, options }); return Response.json(config()) }
    await a.changeStage('s1', { phase: 'REPORT', expected_version: 1, enabled_scenes: [], rule_versions: { S10: 'v1' }, reason: 'Test' })
    const headers = new Headers(calls[1].options.headers)
    assert.equal(headers.get('X-CSRF-Token'), 'test-csrf')
    assert.equal(headers.get('X-Requested-With'), 'XMLHttpRequest')
    assert.equal(headers.has('Authorization'), false)
    assert.equal(calls[1].options.credentials, 'same-origin')
    assert.equal(calls[1].options.cache, 'no-store')
  } finally { globalThis.location = previousLocation; globalThis.fetch = previousFetch }
  assert.equal(typeof auth.authFetch, 'function')
})

test('取消后迟到成功的底层响应也不返回旧数据', async () => {
  const a = await api(); let finish
  globalThis.__phaseFetch = async () => new Promise(r => { finish = () => r(Response.json(config())) })
  const abort = new AbortController(); const pending = a.fetchStageConfig('s1', abort.signal)
  abort.abort(); finish()
  await assert.rejects(pending, e => e.name === 'AbortError')
})

test('配置刷新失败不保留先前已应用结论，时间轴跨场次被拒绝', async () => {
  globalThis.__phaseApi = { ...successApi(), fetchStageControl: async s => ({ ...control(s, 1, 'applied'), acknowledged_consumers: ['media', 'event'] }) }
  const { usePhaseStore } = await store(); await usePhaseStore.getState().selectSession('s1')
  assert.equal(usePhaseStore.getState().control.sync_status, 'applied')
  globalThis.__phaseApi = { ...successApi(), fetchStageConfig: async () => { throw new Error('数据库暂不可用') },
    fetchStageTimeline: async s => ({ session_id: s, items: [config('other')] }) }
  await usePhaseStore.getState().refresh()
  assert.equal(usePhaseStore.getState().config, null)
  assert.equal(usePhaseStore.getState().control, null)
  assert.equal(usePhaseStore.getState().needsConfiguration, false)
  assert.match(usePhaseStore.getState().error, /暂不可用/)
  assert.match(usePhaseStore.getState().timelineError, /场次/)
})
