import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

let api = {}
try {
  let source = readFileSync(new URL('../src/api/faces.ts', import.meta.url), 'utf8')
  source = source.replace(/import\s*\{\s*authFetch\s*\}\s*from\s*['"]\.\/auth['"]/, 'const authFetch = (...args: any[]) => (globalThis as any).__faceAuthFetch(...args)')
  const output = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 } }).outputText
  api = await import(`data:text/javascript;base64,${Buffer.from(output).toString('base64')}`)
} catch (error) { if (error.code !== 'ENOENT') throw error }

test('仅接受非空 JPG/PNG 且不超过 5MiB', () => {
  assert.equal(typeof api.validateFaceFile, 'function')
  assert.equal(api.validateFaceFile(new File(['photo'], 'photo.jpg', {type:'image/jpeg'})), '')
  assert.equal(api.validateFaceFile(new File(['photo'], 'photo.png', {type:'image/png'})), '')
  for (const file of [new File([], 'empty.jpg', {type:'image/jpeg'}), new File(['x'], 'fake.gif', {type:'image/gif'}), new File(['x'], 'fake.txt', {type:'image/jpeg'}), new File([new Uint8Array(5 * 1024 * 1024 + 1)], 'large.png', {type:'image/png'})]) assert.ok(api.validateFaceFile(file))
})

test('status/profile/photo 走 authFetch，photo 返回 blob 而非公开图片URL', async () => {
  assert.equal(typeof api.fetchFacePhoto, 'function')
  const calls = []
  globalThis.__faceAuthFetch = async (url, init) => {
    calls.push({url,init})
    return url.endsWith('/photo') ? new Response('jpeg', {headers:{'Content-Type':'image/jpeg'}}) : Response.json({ready:true,registered:true})
  }
  await api.fetchFaceStatus()
  await api.fetchFaceProfile('a/b')
  const photo = await api.fetchFacePhoto('a/b')
  assert.equal(photo.type, 'image/jpeg')
  assert.equal(await photo.text(), 'jpeg')
  assert.deepEqual(calls.map(c=>c.url), ['/api/faces/status','/api/faces/persons/a%2Fb','/api/faces/persons/a%2Fb/photo'])
})

test('PUT登记/POST核验使用单 file multipart、不手写Content-Type，DELETE鉴权', async () => {
  assert.equal(typeof api.registerFace, 'function')
  const calls = []
  globalThis.__faceAuthFetch = async (url, init) => { calls.push({url,init}); return Response.json({ok:true}) }
  const file = new File(['photo'], 'candidate.png', {type:'image/png'})
  const signal = new AbortController().signal
  await api.registerFace('person-1', file, signal)
  await api.verifyFace('person-1', file, signal)
  await api.deleteFace('person-1', signal)
  assert.deepEqual(calls.map(c=>c.init.method), ['PUT','POST','DELETE'])
  assert.equal(calls[1].url, '/api/faces/persons/person-1/verify?record=true')
  for (const call of calls.slice(0,2)) {
    assert.equal(call.init.body instanceof FormData, true)
    assert.deepEqual([...call.init.body.keys()], ['file'])
    assert.equal(call.init.body.get('file').name, file.name)
    assert.equal(new Headers(call.init.headers).has('content-type'), false)
    assert.equal(call.init.signal, signal)
  }
})

test('明确传递无脸/多脸422、版本冲突409、模型未就绪503、超限413错误', async () => {
  assert.equal(typeof api.FaceApiError, 'function')
  for (const [status, detail] of [[422,'照片中检测到多张脸'],[409,'登记版本已变化，请重新核验'],[503,'模型未就绪'],[413,'照片超过大小限制']]) {
    globalThis.__faceAuthFetch = async () => Response.json({detail}, {status})
    await assert.rejects(api.fetchFaceProfile('person-1'), error => error instanceof api.FaceApiError && error.status === status && error.message === detail)
  }
  globalThis.__faceAuthFetch = async () => Response.json({detail:[{msg:'无效图像'}]}, {status:422})
  await assert.rejects(api.fetchFaceStatus(), /无效图像/)
})
