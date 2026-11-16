import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
let api = {}
try {
  let source = readFileSync(new URL('../src/api/participantVerification.ts', import.meta.url), 'utf8')
  source = source.replace(/import\s*\{\s*authFetch\s*\}\s*from\s*['"]\.\/auth['"]/, 'const authFetch = (...args: any[]) => (globalThis as any).__adminFetch(...args)')
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 } }).outputText
  api = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`)
} catch (error) { if (error.code !== 'ENOENT') throw error }
const token = 'A'.repeat(43)
function browser(hash = '', stored = null) {
  const values = new Map(stored ? [['person-verification-token',stored]] : [])
  globalThis.sessionStorage = { getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key) }
  globalThis.location = { hash, pathname:'/verify',search:'' }
  globalThis.history = {state:null,replaceState:(_state,_title,url)=>{ assert.equal(url,'/verify'); globalThis.location.hash='' }}
  return values
}
test('fragment立即清除，只进入sessionStorage；刷新恢复，显式坏入口不沿用旧令牌', () => {
  assert.equal(typeof api.consumeVerificationToken,'function')
  const values=browser('#'+token)
  assert.equal(api.consumeVerificationToken(),token)
  assert.equal(globalThis.location.hash,'')
  assert.equal(values.get('person-verification-token'),token)
  assert.equal(api.consumeVerificationToken(),token)
  browser('#invalid',token)
  assert.equal(api.consumeVerificationToken(),null)
  assert.equal(globalThis.sessionStorage.getItem('person-verification-token'),null)
  browser('',token);api.clearVerificationToken()
  assert.equal(api.consumeVerificationToken(),null)
})
test('公开API只Bearer普通fetch，omit凭证/no-store/error跳转，令牌不进入URL',async()=>{
  assert.equal(typeof api.fetchVerificationContext,'function')
  const calls=[]
  globalThis.fetch=async(url,init)=>{calls.push({url,init});return Response.json({status:'active'})}
  globalThis.__adminFetch=()=>{throw new Error('公开页不应调用管理员authFetch')}
  await api.fetchVerificationContext(token)
  const file=new File(['pixel'],'test.png',{type:'image/png'})
  await api.submitVerification(token,file)
  assert.deepEqual(calls.map(c=>c.url),['/api/participant-verification/context','/api/participant-verification/submit'])
  for(const {url,init} of calls){assert.equal(url.includes(token),false);assert.equal(init.credentials,'omit');assert.equal(init.cache,'no-store');assert.equal(init.redirect,'error');assert.equal(new Headers(init.headers).get('Authorization'),'Bearer '+token)}
  assert.equal(new Headers(calls[1].init.headers).get('X-Requested-With'),'XMLHttpRequest')
  assert.equal(new Headers(calls[1].init.headers).has('Content-Type'),false)
  assert.equal(calls[1].init.body.get('file').name,'test.png')
})
test('管理员links/results/profile与撤销走authFetch，生成URL仅相对verify fragment',async()=>{
  assert.equal(typeof api.createVerificationLink,'function')
  const calls=[]
  globalThis.__adminFetch=async(url,init={})=>{calls.push({url,init});return Response.json(url.endsWith('verification-links')&&init.method==='POST'?{verification_url:'/verify#'+token}:{items:[]})}
  await api.fetchVerificationLinks('a/b');await api.fetchVerificationResults('a/b');await api.fetchVerificationProfile('a/b')
  await api.createVerificationLink('a/b',{expires_in_minutes:60,project_id:null,room_id:null})
  await api.revokeVerificationLink('link/id')
  assert.equal(calls[0].url,'/api/faces/persons/a%2Fb/verification-links')
  assert.equal(calls[4].url,'/api/faces/verification-links/link%2Fid')
  assert.equal(calls[4].init.method,'DELETE')
  assert.equal(api.isVerificationUrl('/verify#'+token),true)
  for(const url of ['https://evil.test/verify#'+token,'//evil.test/verify#'+token,'/verify?token='+token,'/verify#bad'])assert.equal(api.isVerificationUrl(url),false)
})
test('公开错误保留状态/具体原因且上传校验不发送不合格格式',async()=>{
  assert.equal(typeof api.VerificationApiError,'function')
  for(const status of [404,409,410,422,429,503]){
    globalThis.fetch=async()=>Response.json({detail:'核验测试错误'},{status})
    await assert.rejects(api.fetchVerificationContext(token),error=>error.status===status&&error.message==='核验测试错误')
  }
  assert.ok(api.validateVerificationFile(new File([],'empty.png',{type:'image/png'})))
  assert.ok(api.validateVerificationFile(new File(['x'],'photo.gif',{type:'image/gif'})))
  assert.ok(api.validateVerificationFile(new File([new Uint8Array(5*1024*1024+1)],'large.png',{type:'image/png'})))
  assert.equal(api.validateVerificationFile(new File(['x'],'photo.png',{type:'image/png'})),'')
})
