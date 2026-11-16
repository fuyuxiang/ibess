import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

async function loadApi(name) {
  const source = readFileSync(new URL(`../src/api/${name}.ts`, import.meta.url), 'utf8')
    .replace(/import\s*\{\s*authFetch\s*\}\s*from\s*['"]\.\/auth['"]/, 'const authFetch = (...args: any[]) => (globalThis as any).__personnelFetch(...args)')
    .replace(/import\s*\{\s*readErrorDetail\s*\}\s*from\s*['"]\.\/errorText['"]/, 'const readErrorDetail = () => "request failed"')
  const output = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 } }).outputText
  return import(`data:text/javascript;base64,${Buffer.from(output).toString('base64')}`)
}

test('场次内名单读取和保存携带同一场次，不依赖客户端身份开关', async () => {
  const api = await loadApi('rosters')
  const calls = []
  globalThis.__personnelFetch = async (url, init) => { calls.push({url, init}); return Response.json({items:[]}) }
  await api.fetchRoster('room/a', undefined, 'session/a')
  await api.confirmRoster('room/a', {session_id:'session/a',expected_version:'a'.repeat(64),selections:[],recipients:[],information_visibility:'public'})
  assert.equal(calls[0].url, '/api/rosters/room%2Fa?session_id=session%2Fa')
  assert.equal(calls[1].url, '/api/rosters/room%2Fa/confirm')
  assert.equal(calls[1].init.method, 'POST')
  const body = JSON.parse(calls[1].init.body)
  assert.equal(body.session_id, 'session/a')
  assert.equal(body.identity_verification_enabled, undefined)
})

test('场次内人员登记与撤销沿用事实接口，并保留场次范围', async () => {
  const api = await loadApi('facts')
  const calls = []
  globalThis.__personnelFetch = async (url, init) => { calls.push({url, init}); return Response.json({ok:true}) }
  await api.createFact({session_id:'s1',project_id:'p1',room_id:'r1',person_id:'person1',fact_type:'check_in'})
  await api.deleteFact('fact/a', 'session/a')
  assert.equal(calls[0].url, '/api/facts')
  assert.equal(JSON.parse(calls[0].init.body).session_id, 's1')
  assert.equal(calls[1].url, '/api/facts/fact%2Fa?session_id=session%2Fa')
  assert.equal(calls[1].init.method, 'DELETE')
})
