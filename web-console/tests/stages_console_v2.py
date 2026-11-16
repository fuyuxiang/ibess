"""3.2独立浏览器桩验收，运行实际React组件，不连接业务API或模型。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from pathlib import Path
import subprocess
import tempfile
import threading

from playwright.sync_api import expect, sync_playwright

WEB = Path(__file__).resolve().parents[1]
BUILD = r"""
const esbuild=require('esbuild');
const entry=`import React from 'react';import{createRoot}from'react-dom/client';import Stages from './src/pages/Stages';createRoot(document.getElementById('root')).render(<React.StrictMode><Stages/></React.StrictMode>);`;
esbuild.build({stdin:{contents:entry,resolveDir:process.cwd(),loader:'tsx'},bundle:true,write:false,outdir:'stage-test-memory',format:'iife',jsx:'automatic',target:'es2020',plugins:[{name:'auth-stub',setup(b){b.onResolve({filter:/^\.\/auth$/},a=>a.importer.includes('/api/')||a.importer.includes('\\api\\')?{path:'auth',namespace:'stub'}:null);b.onLoad({filter:/.*/,namespace:'stub'},()=>({contents:'export const authFetch=(...a)=>window.stageFetch(...a)',loader:'js'}))}}]}).then(r=>process.stdout.write(JSON.stringify(r.outputFiles.map(f=>({path:f.path,text:f.text}))))).catch(e=>{console.error(e);process.exit(1)});
"""
STUB = r"""
window.s={calls:[],configs:{},bindings:{},history:{},writes:0,conflict:false,failure:false,delayWrite:false};
const s=window.s;
const cfg=(id,version=1,phase='DETAILED')=>({contract_version:'2.0.0',project_id:'p1',room_id:'r1',session_id:id,source_id:'camera-1',stage_snapshot_id:'snap-'+id+'-'+version,phase_id:phase,phase_version:version,effective_at:'2026-10-14T02:00:00Z',enabled_scenes:['S03'],always_on_scenes:['S10'],assets:[],exceptions:[],rule_versions:{S03:'s03-v1',S10:'s10-v1'},sync_status:'pending'});
s.configs.s1=cfg('s1');s.history.s1=[{...s.configs.s1,actor_id:'测试管理员',reason:'构造初始快照',created_at:'2026-10-14T02:00:00Z'}];
s.configs.s3=cfg('s3',1,'REPORT');s.history.s3=[{...s.configs.s3,actor_id:'测试管理员',reason:'关闭前快照',created_at:'2026-10-14T02:00:00Z'}];
s.records=[{session_id:'s1',name:'构造场次一',status:'running',current_phase:'DETAILED',expected_people:0},{session_id:'s2',name:'未配置场次',status:'draft',current_phase:'review',expected_people:0},{session_id:'s3',name:'关闭场次',status:'closed',current_phase:'REPORT',expected_people:0}];
const fail=(code,message,status)=>Response.json({detail:{code,message}},{status});
window.stageFetch=async(path,options={})=>{
const method=options.method||'GET';s.calls.push({path,method});const body=options.body?JSON.parse(options.body):null;
if(path==='/api/registry')return Response.json({projects:[{project_id:'p1',name:'测试评标项目',status:'running'}],rooms:[{room_id:'r1',project_id:'p1',name:'测试评标室',enabled:true}],persons:[],seats:[],participants:[]});
if(path==='/api/sessions'&&method==='GET')return Response.json(s.records);
if(path==='/api/sessions'&&method==='POST'){const row={session_id:'s-new',name:body.name,status:'draft',current_phase:body.current_phase||'review',expected_people:0};s.records.push(row);return Response.json(row,{status:200})}
const match=path.match(/^\/api\/sessions\/([^/]+)\/(.*)$/);if(!match)return fail('NOT_FOUND','未知测试路径',404);
const id=decodeURIComponent(match[1]),endpoint=match[2];
if(endpoint==='timeline')return Response.json({session_id:id,items:s.history[id]||[]});
if(endpoint==='control-status'){
if(!s.configs[id])return fail('PHASE_NOT_CONFIGURED','尚未配置',409);
return Response.json({session_id:id,stage_snapshot_id:s.configs[id].stage_snapshot_id,phase_id:s.configs[id].phase_id,phase_version:s.configs[id].phase_version,delivery_status:'published',sync_status:s.controlState||'pending',required_consumers:['media','event'],acknowledged_consumers:s.controlState==='applied'?['media','event']:[],attempts:1,last_error_code:null});}
if(endpoint==='binding'&&method==='PUT'){if(s.bindings[id]&&JSON.stringify(s.bindings[id])!==JSON.stringify(body))return fail('BINDING_CONFLICT','场次已有不同业务绑定',409);s.bindings[id]=body;return Response.json({...body,session_id:id,binding_id:'binding-'+id,active:true});}
if(endpoint==='phase-config'&&method==='GET')return s.configs[id]?Response.json(s.configs[id]):fail('PHASE_NOT_CONFIGURED','尚未配置阶段',409);
if((endpoint==='phase-config'&&method==='PUT')||(endpoint==='phase'&&method==='PATCH')){
s.writes++;s.lastWrite={id,body};if(s.delayWrite)await new Promise(resolve=>s.finishWrite=resolve);
if(s.closeOnWrite){s.closeOnWrite=false;s.records=s.records.map(row=>row.session_id===id?{...row,status:'closed'}:row);return fail('SESSION_CLOSED','已关闭场次不能切换阶段',409);}
if(s.failure)return fail('PHASE_STORAGE_UNAVAILABLE','测试后台暂不可用',503);
if(s.conflict){s.conflict=false;s.configs[id]={...s.configs[id],phase_version:s.configs[id].phase_version+1,stage_snapshot_id:'new-concurrent-snapshot'};return fail('VERSION_CONFLICT','配置版本已变化',409)}
const next={...cfg(id,body.expected_version+1,body.phase),...s.bindings[id],enabled_scenes:body.enabled_scenes,rule_versions:body.rule_versions,assets:body.assets||[],exceptions:body.exceptions||[]};s.configs[id]=next;(s.history[id]??=[]).push({...next,actor_id:'测试管理员',reason:body.reason,created_at:'2026-10-14T02:01:00Z'});return Response.json(next,{status:201});}
return fail('NOT_FOUND','未知端点',404);
};
"""


def run():
    build = subprocess.run(['node', '-e', BUILD], cwd=WEB, capture_output=True, text=True, encoding='utf-8', check=True)
    output = json.loads(build.stdout)
    script = next(f['text'] for f in output if f['path'].endswith('.js'))
    css = '\n'.join(f['text'] for f in output if f['path'].endswith('.css'))
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            if self.path == '/app.js': payload, content = script.encode(), 'application/javascript'
            else:
                payload = (f'<!doctype html><meta charset="utf-8"><style>body{{margin:0;padding:24px;background:#f7f8fa;font-family:Microsoft YaHei,Arial,sans-serif}}*{{box-sizing:border-box}}{css}</style><div id="root"></div><script>{STUB}</script><script src="/app.js"></script>').encode()
                content = 'text/html; charset=utf-8'
            self.send_response(200); self.send_header('Content-Type', content); self.end_headers(); self.wfile.write(payload)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    screenshots = Path(tempfile.mkdtemp(prefix='poc-stage-console-'))
    checks = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width':1600, 'height':1100})
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.wait_for_load_state('networkidle')
            expect(page.get_by_role('combobox',name='选择评标场次')).to_be_visible()
            assert not page.evaluate("s.calls.some(c=>c.method!=='GET')")
            assert not page.evaluate("s.calls.some(c=>c.path.includes('/phase-config'))")
            checks.append('打开页面只读列表，不自动创建或默认选择场次')
            def choose_session(name):
                page.locator('.stage-toolbar .ant-select-selector').click()
                page.get_by_title(name,exact=True).click()
                page.keyboard.press('Escape')
                expect(page.locator('.ant-select-dropdown:visible')).to_have_count(0)
            choose_session('构造场次一')
            expect(page.locator('.phase-badge')).to_contain_text('已发送，待应用')
            expect(page.locator('.stage-current')).to_contain_text('详细评审')
            page.get_by_role('button',name='编辑当前配置',exact=True).click()
            def choose_phase(name):
                page.locator('.stage-config-form > label .ant-select-selector').click()
                page.get_by_title(name,exact=True).click()
                expect(page.locator('.ant-select-dropdown:visible')).to_have_count(0)
            choose_phase('必要澄清')
            choose_phase('详细评审')
            expect(page.get_by_role('checkbox',name='S03 离岗越界',exact=True)).to_be_checked()
            expect(page.get_by_role('checkbox',name='S02 设备使用',exact=True)).not_to_be_checked()
            expect(page.get_by_role('checkbox',name='S06 倾向性发言',exact=True)).not_to_be_checked()
            page.get_by_text('S05 跨区接触',exact=True).click()
            expect(page.get_by_role('checkbox',name='S05 跨区接触',exact=True)).to_be_checked()
            choose_phase('必要澄清');choose_phase('详细评审')
            expect(page.get_by_role('checkbox',name='S05 跨区接触',exact=True)).to_be_checked()
            page.get_by_role('dialog').get_by_role('button',name=re.compile(r'^取\s*消$')).click()
            checks.append('目标阶段来回切换保留实际场景与本次编辑，不扩大识别范围')
            page.get_by_role('button',name='进入下一阶段',exact=True).click()
            expect(page.get_by_role('dialog')).to_be_visible()
            page.get_by_role('button',name='确认保存配置',exact=True).click()
            expect(page.get_by_text('请填写切换或配置原因',exact=True)).to_be_visible()
            page.get_by_role('textbox',name='阶段切换原因').fill('进入澄清并保留书面记录')
            page.get_by_role('textbox',name='S07规则版本').fill('s07-v1')
            page.evaluate('s.delayWrite=true')
            page.get_by_role('button',name='确认保存配置',exact=True).dblclick()
            page.wait_for_function('!!s.finishWrite')
            assert page.evaluate('s.writes')==1
            page.evaluate('s.finishWrite();s.delayWrite=false')
            expect(page.get_by_role('dialog')).to_have_count(0)
            expect(page.locator('.stage-current')).to_contain_text('必要澄清')
            assert page.evaluate('s.lastWrite.body.expected_version')==1
            assert page.evaluate('s.lastWrite.body.reason')=='进入澄清并保留书面记录'
            checks.append('切换前确认原因，版本写入和重复点击合并')
            page.get_by_role('button',name='上一阶段',exact=True).click()
            page.get_by_role('textbox',name='阶段切换原因').fill('回退核对评审过程')
            for scene in ('S02','S03','S06'):
                page.get_by_role('textbox',name=scene+'规则版本').fill(scene.lower()+'-v1')
            page.evaluate('s.conflict=true')
            page.get_by_role('button',name='确认保存配置',exact=True).click()
            expect(page.get_by_role('dialog')).to_have_count(0)
            expect(page.get_by_text('配置已被更新，请确认最新版本后重新操作',exact=True)).to_be_visible()
            expect(page.locator('.phase-badge-version')).to_have_text('v3')
            checks.append('版本冲突不重复保存，刷新最新版本并提示重新确认')
            page.evaluate("s.controlState='missing_asset'")
            page.get_by_role('button',name='刷新阶段',exact=True).click()
            expect(page.locator('.phase-badge')).to_contain_text('素材缺失')
            page.evaluate("s.controlState='applied'")
            page.get_by_role('button',name='刷新阶段',exact=True).click()
            expect(page.locator('.phase-badge')).to_contain_text('已应用')
            expect(page.get_by_text('媒体播放状态未接入',exact=True)).to_be_visible()
            checks.append('素材缺失/已应用区分，控制ACK不假作媒体播放')
            choose_session('未配置场次')
            expect(page.locator('.phase-badge')).to_contain_text('阶段待配置')
            page.get_by_role('button',name='配置初始阶段',exact=True).click()
            page.get_by_role('combobox',name='选择项目').click();page.get_by_title('测试评标项目',exact=True).click()
            page.get_by_role('combobox',name='选择评标室').click();page.get_by_title('测试评标室',exact=True).click()
            page.get_by_role('textbox',name='视频或音频源编号').fill('camera-1')
            page.get_by_role('textbox',name='S10规则版本').fill('s10-v1')
            page.get_by_role('textbox',name='阶段切换原因').fill('显式初始化开标阶段')
            page.evaluate('s.failure=true')
            page.get_by_role('button',name='确认保存配置',exact=True).click()
            expect(page.get_by_text('测试后台暂不可用',exact=True)).to_be_visible()
            expect(page.get_by_role('textbox',name='视频或音频源编号')).to_be_disabled()
            expect(page.get_by_role('combobox',name='选择项目')).to_be_disabled()
            expect(page.get_by_role('combobox',name='选择评标室')).to_be_disabled()
            page.evaluate('s.failure=false')
            page.get_by_role('button',name='确认保存配置',exact=True).click()
            expect(page.get_by_role('dialog')).to_have_count(0)
            expect(page.locator('.stage-current')).to_contain_text('开标记录')
            assert page.evaluate('s.bindings.s2.source_id')=='camera-1'
            checks.append('未配置场次明确业务绑定和规则版本，不借用旧场次')
            assert page.evaluate('s.calls.filter(c=>c.path==="/api/sessions/s2/binding"&&c.method==="PUT").length')==1
            checks.append('初始绑定成功后锁定绑定，仅重试失败的阶段配置')
            choose_session('构造场次一')
            page.get_by_role('button',name='进入下一阶段',exact=True).click()
            page.get_by_role('textbox',name='阶段切换原因').fill('失败后仍保留原因')
            page.evaluate('s.failure=true')
            page.get_by_role('button',name='确认保存配置',exact=True).click()
            expect(page.get_by_text('测试后台暂不可用',exact=True)).to_be_visible()
            expect(page.get_by_role('textbox',name='阶段切换原因')).to_have_value('失败后仍保留原因')
            page.evaluate('s.failure=false');page.get_by_role('dialog').get_by_role('button',name=re.compile(r'^取\s*消$')).click()
            checks.append('保存失败保留输入，供重试而非假成功')
            choose_session('关闭场次')
            expect(page.get_by_text('场次已关闭，仅可查看配置与历史',exact=True)).to_be_visible()
            expect(page.get_by_role('button',name='上一阶段',exact=True)).to_be_disabled()
            expect(page.get_by_role('button',name='进入下一阶段',exact=True)).to_be_disabled()
            expect(page.get_by_role('button',name='编辑当前配置',exact=True)).to_be_disabled()
            assert page.locator('.stage-node:disabled').count()==9
            checks.append('关闭场次只读，禁止初始化或切换阶段')
            writes_before=page.evaluate('s.calls.filter(c=>c.method!=="GET").length')
            page.get_by_role('button',name='新建场次',exact=True).click()
            page.get_by_role('button',name='创建场次',exact=True).click()
            expect(page.get_by_text('请填写场次名称',exact=True)).to_be_visible()
            assert page.evaluate('s.calls.filter(c=>c.method!=="GET").length')==writes_before
            page.get_by_role('textbox',name='新建场次名称').fill('用户明确新建的测试场次')
            page.get_by_role('button',name='创建场次',exact=True).click()
            expect(page.get_by_role('dialog')).to_have_count(0)
            expect(page.locator('.phase-badge')).to_contain_text('阶段待配置')
            assert page.evaluate('s.calls.filter(c=>c.method==="POST"&&c.path==="/api/sessions").length')==1
            assert page.evaluate('s.configs["s-new"]===undefined')
            checks.append('显式新建仅创建草稿，不自动绑定或初始化阶段')
            choose_session('构造场次一')
            expect(page.locator('.stage-current')).to_contain_text('必要澄清')
            phase_ids=('OPENING','ATTENDANCE','RECUSAL','PRELIMINARY','DETAILED','CLARIFICATION','SCORING','REPORT','OFFLINE_QA')
            for index,phase in enumerate(phase_ids):
                version=page.evaluate('s.configs.s1.phase_version')
                writes=page.evaluate('s.writes')
                page.locator('.stage-node').nth(index).click()
                page.get_by_role('textbox',name='阶段切换原因').fill('九阶段逐项验证 '+phase)
                for field in page.locator('.stage-rule-fields input').all():
                    if not field.input_value():field.fill('fixture-rule-v1')
                page.get_by_role('button',name='确认保存配置',exact=True).click()
                expect(page.get_by_role('dialog')).to_have_count(0)
                expect(page.locator('.phase-badge-version')).to_have_text('v'+str(version+1))
                assert page.evaluate('s.lastWrite.body.phase')==phase
                assert page.evaluate('s.lastWrite.body.expected_version')==version
                assert page.evaluate('s.writes')==writes+1
                assert page.evaluate('s.lastWrite.body.rule_versions.S10')
                expect(page.locator('.stage-history-card')).to_contain_text('九阶段逐项验证 '+phase)
            checks.append('九阶段逐项切换均保存版本/原因/全程S10并刷新时间轴（API桩）')
            page.get_by_role('button',name='编辑当前配置',exact=True).click()
            page.get_by_role('textbox',name='阶段切换原因').fill('外部关闭后不继续修改')
            page.evaluate('s.closeOnWrite=true')
            page.get_by_role('button',name='确认保存配置',exact=True).click()
            expect(page.get_by_role('dialog')).to_have_count(0)
            expect(page.get_by_text('场次已关闭，仅可查看配置与历史',exact=True)).to_be_visible()
            expect(page.get_by_role('button',name='编辑当前配置',exact=True)).to_be_disabled()
            assert page.locator('.stage-node:disabled').count()==9
            checks.append('保存遇到外部关闭立即转只读，不继续重复保存')
            for width in (1366,1600,1920):
                page.set_viewport_size({'width':width,'height':1100})
                page.evaluate("document.body.style.marginLeft='200px';document.body.style.padding='16px'")
                assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'),width
                expect(page.locator('.stage-node')).to_have_count(9)
                expect(page.locator('.stage-node').last).to_be_in_viewport()
                page.screenshot(path=str(screenshots/f'stages-{width}.png'),full_page=True)
            assert not errors,errors
            checks.append('1366/1600/1920预留200px侧栏后无横向溢出，九阶段齐全，无JS异常')
            browser.close()
        print(json.dumps({'checks':checks,'passed':len(checks),'nature':'isolated browser API fixtures, no real model','screenshots':str(screenshots)},ensure_ascii=False))
    finally: server.shutdown();server.server_close()


if __name__=='__main__':run()
