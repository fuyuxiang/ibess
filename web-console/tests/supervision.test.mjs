import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

const source=readFileSync(new URL('../src/components/workflow/supervision-model.ts',import.meta.url),'utf8')
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2020}}).outputText
const model=await import('data:text/javascript;base64,'+Buffer.from(compiled).toString('base64'))
const event=(workflow_status,status='PENDING')=>({workflow_status,status})

test('监管流程以持久化监管状态为准，设备自动关闭不等同于归档',()=>{
  assert.equal(model.workflowStatus(event('SUPPLEMENT','CLOSED')),'SUPPLEMENT')
  assert.equal(model.workflowStatus({status:'CLOSED'}),'LEGACY_CLOSED')
  assert.equal(model.workflowStep({status:'CLOSED'}),1)
  assert.deepEqual(model.allowedActions({status:'CLOSED'}),['confirm','supplement','dismiss'])
})
test('需先登记处置和提交反馈，才能人工复核归档',()=>{
  assert.deepEqual(model.allowedActions(event('CONFIRMED')),['assign','reopen'])
  assert.deepEqual(model.allowedActions(event('ASSIGNED')),['feedback','assign','reopen'])
  assert.deepEqual(model.allowedActions(event('FEEDBACK')),['archive','return','reopen'])
  assert.deepEqual(model.allowedActions(event('ARCHIVED')),['reopen'])
})
test('补充核查保持在人工确认环节，旧误报记录须先重新核查才能更正',()=>{
  assert.equal(model.workflowStep(event('SUPPLEMENT')),1)
  assert.deepEqual(model.allowedActions(event('SUPPLEMENT')),['confirm','supplement','dismiss'])
  assert.equal(model.workflowStatus({status:'FALSE_POSITIVE'}),'ARCHIVED')
  assert.deepEqual(model.allowedActions({status:'FALSE_POSITIVE'}),['reopen'])
})
test('逾期只标记未归档事件，不把历史逾期已完成任务误报为当前积压',()=>{
  assert.equal(model.overdue({...event('ASSIGNED'),due_at:'2000-01-01T00:00:00Z'}),true)
  assert.equal(model.overdue({...event('ARCHIVED'),due_at:'2000-01-01T00:00:00Z'}),false)
  assert.equal(model.overdue({...event('ASSIGNED'),due_at:null}),false)
})

test('原始人员检测等基础观察不作为违规任务处置',()=>{
  const observation={...event('PENDING'),category:'observation'}
  assert.deepEqual(model.allowedActions(observation),[])
  assert.equal(model.workflowStep(observation),0)
})

const queueSource=readFileSync(new URL('../src/components/workflow/refresh-queue.ts',import.meta.url),'utf8')
const queueJs=ts.transpileModule(queueSource,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2020}}).outputText
const {createRefreshQueue}=await import('data:text/javascript;base64,'+Buffer.from(queueJs).toString('base64'))
test('连续刷新合并，慢请求成功结果仍显示，任意时刻只有一个查询',async()=>{
  const deferred=[],values=[];let active=0,max=0;
  const q=createRefreshQueue({load:()=>new Promise(resolve=>{active++;max=Math.max(max,active);deferred.push(value=>{active--;resolve(value)})}),data:v=>values.push(v),error:()=>{},loading:()=>{}})
  const first=q.refresh();void q.refresh();void q.refresh();
  assert.equal(deferred.length,1);deferred.shift()(1);await first;
  assert.deepEqual(values,[1]);assert.equal(deferred.length,1);
  deferred.shift()(2);await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(values,[1,2]);assert.equal(max,1);q.dispose();
})
test('查询切换取消旧请求，旧结果不会覆盖新页面',async()=>{
  let resolve,signal;const values=[];
  const q=createRefreshQueue({load:s=>{signal=s;return new Promise(r=>{resolve=r})},data:v=>values.push(v),error:()=>{},loading:()=>{}})
  const first=q.refresh();q.dispose();assert.equal(signal.aborted,true);resolve('old');await first;assert.deepEqual(values,[]);
})
test('后台刷新失败保留已取得的数据，后续可以恢复',async()=>{
  let attempt=0;const values=[],errors=[];
  const q=createRefreshQueue({load:async()=>{if(++attempt===2)throw Error('断网');return attempt},data:v=>values.push(v),error:e=>errors.push(e.message),loading:()=>{}})
  await q.refresh();await q.refresh();assert.deepEqual(values,[1]);assert.deepEqual(errors,['断网']);await q.refresh();assert.deepEqual(values,[1,3]);q.dispose();
})

const windowSource=readFileSync(new URL('../src/components/audio/semantic-window.ts',import.meta.url),'utf8')
const windowJs=ts.transpileModule(windowSource,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2020}}).outputText
const {semanticWindow}=await import('data:text/javascript;base64,'+Buffer.from(windowJs).toString('base64'))
test('语义窗口保留跨起点的完整原话，不悄悄丢掉边界句段',()=>{
  const result=semanticWindow([{start_ms:0,end_ms:20000},{start_ms:20000,end_ms:45000},{start_ms:45000,end_ms:70000}],10000,70000)
  assert.deepEqual(result,{start:0,end:45000,segments:[{start_ms:0,end_ms:20000},{start_ms:20000,end_ms:45000}]})
})
test('超长句段与无效窗口明确报错',()=>{
  assert.throws(()=>semanticWindow([{start_ms:0,end_ms:61000}],0,70000),/超过60秒/)
  assert.throws(()=>semanticWindow([],0,70000),/有效转写/)
})

test('历史更正留痕以业务术语展示，保留原认定和更正原因',()=>{
  const note='ARCHIVED → SUPPLEMENT；原认定与处置：{"status":"ARCHIVED","resolution":"confirmed","feedback":"原反馈"}；重新核查依据：找到新的授权依据'
  assert.equal(model.auditNote(note),'已归档 → 补充核查中；原人工结论：确认异常；原流程状态：已归档；重新核查依据：找到新的授权依据')
  assert.ok(note.includes('"feedback":"原反馈"'))
})
