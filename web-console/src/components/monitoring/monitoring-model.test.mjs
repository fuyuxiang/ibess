import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
const source = readFileSync(new URL('./monitoring-model.ts',import.meta.url),'utf8')
const output = ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2020}}).outputText
const model = await import(`data:text/javascript;base64,${Buffer.from(output).toString('base64')}`)
const camera=(session,id,overrides={})=>({session_id:session,source_id:id,kind:'video',room_id:'r1',project_id:'p1',...overrides})
test('告警只联动同场次唯一机位，兼容实际摄像头别名',()=>{
 const a=camera('s1','camera-001'),b=camera('s2','cam1')
 assert.equal(model.findEventSource({session_id:'s1',camera_id:'cam1'},[a,b]),a)
 assert.equal(model.findEventSource({session_id:'s2',camera_id:'camera-0001'},[a,b]),b)
 assert.equal(model.findEventSource({session_id:'s3',camera_id:'cam1'},[a,b]),null)
 assert.equal(model.findEventSource({session_id:'',camera_id:'cam1'},[a,b]),null)
})
test('同场次机位歧义或仅有拾音通道时，不显示其他实时画面作为告警证据',()=>{
 const a=camera('s1','camera-001'),b=camera('s1','cam1')
 assert.equal(model.findEventSource({session_id:'s1',camera_id:'cam1'},[a,b]),null)
 assert.equal(model.findEventSource({session_id:'s1',camera_id:'cam1'},[camera('s1','cam1',{kind:'audio'})]),null)
 assert.equal(model.channelId('camera-real-01'),model.channelId('cam-ezviz'))
})
test('项目、评标室和场次筛选严格限制显示范围，失效范围不能回退到其他现场',()=>{
 const source=camera('s1','cam1')
 assert.equal(model.sourceMatches(source,new URLSearchParams('session_id=s1&room_id=r1&project_id=p1')),true)
 for(const query of ['session_id=s2','room_id=r2','project_id=p2'])assert.equal(model.sourceMatches(source,new URLSearchParams(query)),false)
})
test('机位选择和切换分屏会定位到正确分页，并保留不同场次相同来源标识',()=>{
 const sources=Array.from({length:11},(_,i)=>camera('s'+i,'cam1'))
 assert.equal(model.pageForSource(sources,model.sourceIdentity(sources[9]),4),2)
 assert.equal(model.pageForSource(sources,model.sourceIdentity(sources[9]),9),1)
 assert.equal(model.pageForSource(sources,'missing',4),0)
 assert.notEqual(model.sourceIdentity(sources[0]),model.sourceIdentity(sources[1]))
})
test('待核验或未配置状态不冒充在线；流、录像和分析使用各自事实',()=>{
 for(const status of ['online','running','recent'])assert.equal(model.signalTone(status),'ok')
 for(const status of ['offline','input_error','stale'])assert.equal(model.signalTone(status),'bad')
 for(const status of [undefined,'unknown','unconfigured','not_applicable'])assert.equal(model.signalTone(status),'unknown')
})

test('超出整数精度的来源编号仍按完整字符串匹配，不会联动相邻编号',()=>{
 const source=camera('s1','camera-9007199254740992')
 assert.equal(model.findEventSource({session_id:'s1',camera_id:'camera-9007199254740993'},[source]),null)
 assert.equal(model.findEventSource({session_id:'s1',camera_id:'cam09007199254740992'},[source]),source)
})
