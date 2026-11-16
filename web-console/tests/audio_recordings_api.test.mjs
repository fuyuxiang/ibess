import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

let api = {}
try {
  let source = readFileSync(new URL('../src/api/audioRecordings.ts', import.meta.url), 'utf8')
  source = source.replace(/import\s*\{\s*authFetch\s*\}\s*from\s*['"]\.\/auth['"]/, 'const authFetch=(...args:any[])=>(globalThis as any).__audioFetch(...args)')
  const output = ts.transpileModule(source, {compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2020}}).outputText
  api = await import(`data:text/javascript;base64,${Buffer.from(output).toString('base64')}`)
} catch (error) { if (error.code !== 'ENOENT') throw error }

test('上传前只按非空、WAV扩展和20MiB限制，PCM/时长由后端验证', () => {
  assert.equal(typeof api.validateAudioFile,'function')
  assert.equal(api.validateAudioFile(new File(['wav'],'sample.WAV',{type:'application/octet-stream'})),'')
  assert.ok(api.validateAudioFile(new File([],'empty.wav')))
  assert.ok(api.validateAudioFile(new File(['x'],'fake.mp3',{type:'audio/wav'})))
  assert.ok(api.validateAudioFile(new File([new Uint8Array(20*1024*1024+1)],'large.wav')))
})

test('鉴权列表/详情/上传202/手动重试请求，multipart不设置Content-Type', async () => {
  assert.equal(typeof api.uploadAudioRecording,'function')
  const calls=[]
  globalThis.__audioFetch=async(url,init={})=>{calls.push({url,init});return Response.json({recording_id:'id',status:'queued'},{status:init.method==='POST'&&!url.endsWith('retry')?202:200})}
  await api.fetchAudioRecordings()
  await api.fetchAudioDetail('a/b')
  const file=new File(['PCM'],'demo.wav')
  assert.equal((await api.uploadAudioRecording(file)).status,'queued')
  await api.retryAudioRecording('a/b')
  assert.deepEqual(calls.map(c=>c.url),['/api/audio/recordings?limit=30','/api/audio/recordings/a%2Fb','/api/audio/recordings','/api/audio/recordings/a%2Fb/retry'])
  assert.equal(calls[2].init.body instanceof FormData,true)
  assert.equal(calls[2].init.body.get('file').name,'demo.wav')
  assert.equal(new Headers(calls[2].init.headers).has('Content-Type'),false)
  assert.equal(calls[3].init.method,'POST')
})

test('整段和片段回放读取鉴权WAV blob；错误具体传递，异常内容拒绝播放',async()=>{
  assert.equal(typeof api.fetchAudioPlayback,'function')
  const calls=[]
  globalThis.__audioFetch=async(url)=>{calls.push(url);return new Response('WAV',{headers:{'content-type':'audio/wav'}})}
  const blob=await api.fetchAudioPlayback('r/1')
  assert.equal(blob.type,'audio/wav')
  await api.fetchAudioPlayback('r/1','s/1')
  assert.deepEqual(calls,['/api/audio/recordings/r%2F1/playback','/api/audio/recordings/r%2F1/segments/s%2F1/playback'])
  globalThis.__audioFetch=async()=>Response.json({detail:'PCM WAV文件格式不支持'},{status:422})
  await assert.rejects(api.fetchAudioPlayback('r'),error=>error.status===422&&error.message==='PCM WAV文件格式不支持')
  globalThis.__audioFetch=async()=>new Response('html',{headers:{'content-type':'text/html'}})
  await assert.rejects(api.fetchAudioPlayback('r'),/回放内容/)
})

test('只保留有限且录音范围内end>start的片段，未知说话人不假造标签',()=>{
  assert.equal(typeof api.validAudioSegments,'function')
  const base={segment_id:'s1',speaker:null,start_ms:125.5,end_ms:1000.25,text:'真实返回文字',playback_url:'/private'}
  const segments=[base,{...base,segment_id:'s2',speaker:'C',start_ms:1100,end_ms:1900},
    {...base,start_ms:NaN},{...base,end_ms:Infinity},{...base,start_ms:-1},{...base,end_ms:2500},{...base,end_ms:125.5}]
  assert.equal(api.validAudioSegments(segments,2000).length,2)
  assert.equal(api.validAudioSegments(segments,NaN).length,0)
  assert.equal(api.speakerLabel(null),'说话人未确定')
  assert.equal(api.speakerLabel('C'),'说话人 C')
  assert.equal(api.formatAudioTime(125.5),'0.126 秒')
})

test('外部AbortSignal传入后中止请求，不吞掉Abort错误',async()=>{
  assert.equal(typeof api.fetchAudioDetail,'function')
  globalThis.__audioFetch=async(_url,{signal})=>new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')),{once:true}))
  const controller=new AbortController()
  const pending=api.fetchAudioDetail('r',controller.signal)
  controller.abort()
  await assert.rejects(pending,error=>error.name==='AbortError')
})
