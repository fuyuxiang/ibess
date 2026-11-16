"""人员核验公开页及管理弹窗前端桩；不连接实际业务API、不创建管理员或人脸。

令牌在页面JS内生成，不保存到报告/截图/日志；截图屏蔽刚生成的链接。
python -B tests/participant_verification_frontend_test.py
"""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import subprocess
import threading

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent
WEB = ROOT.parent
PIXEL = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aB9sAAAAASUVORK5CYII=')
BUILD = r"""
const esbuild = require('esbuild');
const entry = `import React from 'react';import{createRoot}from'react-dom/client';import PersonVerification from'./src/pages/PersonVerification';import PersonVerificationAdminModal from'./src/components/face/PersonVerificationAdminModal';
const projects=[{project_id:'p1',name:'桩成员项目',code:'P1',owner:'桩',status:'running',start_at:null,end_at:null},{project_id:'p2',name:'不可选其他项目',code:'P2',owner:'桩',status:'running',start_at:null,end_at:null}];
const rooms=[{room_id:'r1',project_id:'p1',name:'桩启用室',code:'R1',enabled:true,mode:'centralized',location:'桩'},{room_id:'r2',project_id:'p1',name:'不可选停用室',code:'R2',enabled:false,mode:'centralized',location:'桩'},{room_id:'r3',project_id:'p2',name:'不可选其他室',code:'R3',enabled:true,mode:'centralized',location:'桩'}];
function Harness(){const[mode,setMode]=React.useState(location.pathname==='/verify'?'public':'none');const[person,setPerson]=React.useState(null);window.pvUnmount=()=>{setMode('none');setPerson(null)};window.pvPublic=()=>setMode('public');window.pvAdmin=(id,enabled=true)=>{setMode('admin');setPerson(id?{person_id:id,name:'桩人员-'+id,code:'TEST-'+id,unit:'桩单位',enabled,role:'expert'}:null)};return mode==='public'?<PersonVerification/>:mode==='admin'?<PersonVerificationAdminModal person={person} projects={projects} rooms={rooms} participants={[{membership_id:'m1',project_id:'p1',person_id:'a',role:'expert'}]} onClose={()=>setPerson(null)}/>:<div>独立桩测试</div>};createRoot(document.getElementById('root')).render(<Harness/>);`;
esbuild.build({stdin:{contents:entry,resolveDir:process.cwd(),loader:'tsx'},bundle:true,write:false,format:'iife',jsx:'automatic',target:'es2020',loader:{'.css':'empty'},plugins:[{name:'admin-stub',setup(b){b.onResolve({filter:/^\.\/auth$/},args=>args.importer.endsWith('participantVerification.ts')?{path:'auth',namespace:'stub'}:null);b.onLoad({filter:/.*/,namespace:'stub'},()=>({contents:'export const authFetch=(...args)=>window.pvAdminFetch(...args)',loader:'js'}))}}]}).then(r=>process.stdout.write(r.outputFiles[0].text)).catch(e=>{process.stderr.write(String(e));process.exit(1)});
"""

STUB = r"""
const encode=bytes=>btoa(String.fromCharCode(...bytes)).replaceAll('+','-').replaceAll('/','_').replaceAll('=','');
window.pvState={token:encode(crypto.getRandomValues(new Uint8Array(32))),token2:encode(crypto.getRandomValues(new Uint8Array(32))),publicCalls:[],adminCalls:[],submits:0,remaining:5,last:null,status:'active',ready:true,registered:{a:true,b:false},links:[],results:[],urlSet:new Set(),cameraCalls:0,stops:0};
const s=pvState;
const makeURL=URL.createObjectURL.bind(URL),revoke=URL.revokeObjectURL.bind(URL);
URL.createObjectURL=b=>{const url=makeURL(b);s.urlSet.add(url);return url};URL.revokeObjectURL=url=>{s.urlSet.delete(url);revoke(url)};
const response=r=>Response.json(r);
const result=(status='unmatched')=>({result_id:'result-'+s.submits,link_id:'link-a',status,matched:status==='invalid'?null:status==='matched',similarity:status==='invalid'?null:status==='matched'?0.85:0.15,threshold:status==='invalid'?null:0.363,checked_at:'2026-10-09T01:00:00Z',person_name:'桩核验人员',project_name:'桩成员项目',room_name:'桩启用室',model:'FRONTEND-STUB',reference_version:'v1',error:status==='invalid'?'照片中有多张人脸':null,liveness_checked:false});
const ctx=()=>({person:{name:'桩核验人员',code:'TEST-PUBLIC',unit:'桩单位'},project_name:'桩成员项目',room_name:'桩启用室',expires_at:'2099-10-09T02:00:00Z',status:s.status,remaining_attempts:s.remaining,model_ready:s.ready,message:s.status==='completed'?'已完成核验':s.ready?'请提交单人清晰照片':'模型未就绪',can_verify:s.status==='active'&&s.ready&&s.remaining>0,last_result:s.last});
const original=fetch.bind(window);
window.fetch=async(url,init={})=>{if(!String(url).startsWith('/api/'))return original(url,init);const h=new Headers(init.headers);s.publicCalls.push({path:String(url),method:init.method||'GET',credentials:init.credentials,cache:init.cache,redirect:init.redirect,hasBearer:h.get('Authorization')?.startsWith('Bearer '),xhr:h.get('X-Requested-With'),multipart:init.body instanceof FormData});
if(url==='/api/participant-verification/context'){if(s.delayContext)await new Promise(resolve=>s.finishContext=resolve);if(s.contextError)return Response.json({detail:'入口已过期或被撤销'},{status:s.contextError});return response(ctx())};
if(url==='/api/participant-verification/submit'){s.submits++;s.remaining--;if(s.delaySubmit)await new Promise(resolve=>s.finishSubmit=resolve);if(s.submitMode==='invalid'){s.last=result('invalid');return Response.json({detail:'照片中有多张人脸'},{status:422})};if(s.submitMode==='expired'){s.contextError=410;return Response.json({detail:'入口已过期或被撤销'},{status:410})};s.last=result(s.submitMode==='matched'?'matched':'unmatched');if(s.submitMode==='matched')s.status='completed';return response(s.last)};throw new Error('公开页调用了非公开API');};
window.pvAdminFetch=async(url,init={})=>{s.adminCalls.push({path:url,method:init.method||'GET'});const person=url.match(/persons\/([^/]+)/)?.[1];
if(url.includes('verification-results'))return response({items:s.results});
if(url.includes('verification-links')){if(init.method==='POST'){const body=JSON.parse(init.body);s.lastCreate=body;if(s.delayCreate)await new Promise(resolve=>s.finishCreate=resolve);s.links=s.links.map(row=>({...row,status:row.status==='active'?'revoked':row.status}));const item={link_id:'link-'+(s.links.length+1),person_id:person,created_at:'2026-10-09T01:00:00Z',expires_at:'2099-10-09T02:00:00Z',status:'active',attempts:0,max_attempts:5,project_id:body.project_id,room_id:body.room_id,project_name:body.project_id?'桩成员项目':null,room_name:body.room_id?'桩启用室':null};s.links.unshift(item);return response({...item,verification_url:'/verify#'+s.token})};if(init.method==='DELETE'){s.links=s.links.map(row=>({...row,status:'revoked'}));return response({ok:true})};return response({items:s.links})};return response({person_id:person,registered:!!s.registered[person],version:'v1'})};
Object.defineProperty(navigator,'mediaDevices',{configurable:true,value:{getUserMedia:async()=>{s.cameraCalls++;if(s.cameraDenied)throw new DOMException('test','NotAllowedError');if(s.delayCamera)await new Promise(resolve=>s.finishCamera=resolve);const c=document.createElement('canvas');c.width=320;c.height=240;c.getContext('2d').fillRect(0,0,320,240);const stream=c.captureStream(15);for(const track of stream.getTracks()){const stop=track.stop.bind(track);track.stop=()=>{s.stops++;stop()}}return stream}}});
"""


def main():
    built = subprocess.run(['node','-e',BUILD],cwd=WEB,capture_output=True,text=True,encoding='utf-8')
    if built.returncode: raise RuntimeError(built.stderr)
    js=built.stdout.encode('utf-8')
    css=(WEB/'src/person-verification.css').read_text(encoding='utf-8')
    html=('<!DOCTYPE html><html><head><meta charset="utf-8"><style>body{margin:0;min-width:1600px;font-family:"Microsoft YaHei",sans-serif}'+css+'</style></head><body><div id="root"></div><script>'+STUB+'</script><script src="/bundle.js"></script></body></html>').encode('utf-8')
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body,kind=(js,'application/javascript') if self.path=='/bundle.js' else (html,'text/html')
            self.send_response(200);self.send_header('Content-Type',kind+'; charset=utf-8');self.end_headers();self.wfile.write(body)
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    report={'scope':'独立前端桩，无真实管理员/人脸/公开令牌持久记录','status':'RUNNING','checks':[],'page_errors':[]}
    def passed(name): report['checks'].append(name);print('PASS',name,flush=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1366,'height':1050})
        page.on('pageerror',lambda error:report['page_errors'].append(str(error)))
        try:
            page.goto(f'http://127.0.0.1:{server.server_port}/verify',wait_until='load')
            page.wait_for_function("typeof pvPublic==='function'")
            expect(page.get_by_text('请打开管理员提供的核验链接',exact=True)).to_be_visible()
            assert page.evaluate('pvState.publicCalls.length===0&&pvState.adminCalls.length===0')
            def entry(second=False):
                page.evaluate('(second)=>{pvState.remaining=5;pvState.status="active";pvState.last=null;pvState.contextError=null;location.hash=second?pvState.token2:pvState.token}',second)
                expect(page.get_by_role('heading',name='桩核验人员',exact=True)).to_be_visible()
                expect(page.get_by_role('button',name=re.compile('刷新入口状态$'))).to_be_enabled()
            def upload(): page.get_by_label('上传核验照片').set_input_files({'name':'frontend-stub.png','mimeType':'image/png','buffer':PIXEL})
            def submit(): page.get_by_role('button',name=re.compile('提交一对一核验$')).click()
            entry()
            assert page.evaluate('location.hash===""&&sessionStorage.getItem("person-verification-token")===pvState.token&&localStorage.length===0')
            assert page.evaluate('pvState.cameraCalls===0')
            page.reload(wait_until='load')
            expect(page.get_by_role('heading',name='桩核验人员',exact=True)).to_be_visible()
            assert page.evaluate('pvState.adminCalls.length===0&&pvState.publicCalls.every(c=>c.path==="/api/participant-verification/context"&&c.credentials==="omit"&&c.cache==="no-store"&&c.redirect==="error"&&c.hasBearer)')
            passed('fragment消费并清除、sessionStorage刷新恢复、无管理员调用/令牌API URL')
            for width in [1366,1600,1920]:
                page.set_viewport_size({'width':width,'height':1050})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+2')
            page.set_viewport_size({'width':1366,'height':1050})
            passed('公开页覆盖全局min-width，三个桌面宽度无溢出')

            upload();submit()
            expect(page.locator('.pv-score-row')).to_contain_text('0.1500')
            expect(page.get_by_text('剩余 4 次提交',exact=True)).to_be_visible()
            expect(page.locator('.pv-result-card')).to_contain_text('未做活体检测')
            assert page.evaluate('pvState.publicCalls.filter(c=>c.method==="POST").every(c=>c.credentials==="omit"&&c.xhr==="XMLHttpRequest"&&c.multipart&&c.hasBearer)')
            passed('未匹配展示真实桩分数，更新次数、multipart Bearer凭证隔离')
            page.evaluate('pvState.submitMode="invalid"');submit()
            expect(page.get_by_text('剩余 3 次提交',exact=True)).to_be_visible()
            expect(page.locator('.pv-score-row')).to_have_count(0)
            expect(page.get_by_text('图片不合格',exact=True)).to_be_visible()
            passed('422刷新context和次数、清除旧分数、显示具体错误')

            page.evaluate('pvState.submitMode="matched"');submit()
            expect(page.get_by_text('核验已完成',exact=True)).to_be_visible()
            expect(page.locator('.pv-score-row')).to_contain_text('0.8500')
            expect(page.get_by_role('button',name=re.compile('提交一对一核验$'))).to_be_disabled()
            page.screenshot(path=str(ROOT/'participant-verification-public.png'),full_page=True)
            passed('匹配成功入口完成、禁止重复提交、不显示底照管理入口')
            entry(True)
            page.evaluate('pvState.submitMode="unmatched";pvState.delaySubmit=true');upload();submit()
            page.wait_for_function('!!pvState.finishSubmit')
            expect(page.get_by_role('button',name=re.compile('提交一对一核验$'))).to_be_disabled()
            page.get_by_role('button',name='清除核验入口',exact=True).click()
            page.evaluate('pvState.finishSubmit();pvState.delaySubmit=false')
            expect(page.get_by_text('请打开管理员提供的核验链接',exact=True)).to_be_visible()
            page.wait_for_function('pvState.urlSet.size===0')
            assert page.evaluate('sessionStorage.getItem("person-verification-token")===null')
            passed('忙碌禁重复、清除入口忽略迟到响应并释放objectURL/凭证')

            entry();page.evaluate('pvState.cameraDenied=true')
            page.get_by_role('button',name=re.compile('开启摄像头$')).click()
            expect(page.get_by_text('可改用上传照片',exact=True)).to_be_visible()
            upload();expect(page.get_by_role('button',name=re.compile('提交一对一核验$'))).to_be_enabled()
            page.evaluate('pvState.cameraDenied=false')
            page.get_by_role('button',name=re.compile('开启摄像头$')).click()
            expect(page.get_by_role('button',name=re.compile('拍\\s*照$'))).to_be_enabled()
            before=page.evaluate('pvState.stops')
            page.get_by_role('button',name=re.compile('拍\\s*照$')).click()
            expect(page.get_by_text('verification-camera.jpg',exact=True)).to_be_visible()
            assert page.evaluate('pvState.stops')>before
            passed('手动摄像头权限失败上传回退、拍照停止tracks')
            page.evaluate('pvState.delayCamera=true')
            page.get_by_role('button',name=re.compile('开启摄像头$')).click()
            page.wait_for_function('!!pvState.finishCamera')
            page.get_by_role('button',name='清除核验入口',exact=True).click()
            before=page.evaluate('pvState.stops')
            page.evaluate('pvState.finishCamera();pvState.delayCamera=false')
            page.wait_for_function('(n)=>pvState.stops>n',arg=before)
            passed('关闭入口后迟到摄像头授权立即停止')
            entry();upload();page.evaluate('pvState.submitMode="expired"');submit()
            expect(page.get_by_text('入口已过期或被撤销',exact=True)).to_be_visible()
            expect(page.locator('.pv-score-row')).to_have_count(0)
            expect(page.get_by_role('button',name=re.compile('提交一对一核验$'))).to_be_disabled()
            passed('410失效具体错误、不保留旧结果、不继续提交')

            page.evaluate('pvUnmount()')
            page.wait_for_function('pvState.urlSet.size===0')
            page.evaluate('pvAdmin("b")')
            expect(page.get_by_text('尚未登记底照',exact=True)).to_be_visible()
            expect(page.get_by_role('button',name=re.compile('生成核验链接$'))).to_be_disabled()
            page.evaluate('pvAdmin("a")')
            expect(page.get_by_role('button',name=re.compile('生成核验链接$'))).to_be_enabled()
            page.get_by_role('combobox',name='绑定项目',exact=True).focus()
            page.get_by_role('combobox',name='绑定项目',exact=True).press('ArrowDown')
            dropdown=page.locator('.ant-select-dropdown:visible')
            expect(dropdown).not_to_contain_text('不可选其他项目')
            dropdown.get_by_text('桩成员项目（P1）',exact=True).click()
            expect(dropdown).not_to_be_visible()
            page.get_by_role('combobox',name='绑定评标室',exact=True).focus()
            page.get_by_role('combobox',name='绑定评标室',exact=True).press('ArrowDown')
            dropdown=page.locator('.ant-select-dropdown:visible')
            expect(dropdown).not_to_contain_text('不可选停用室');expect(dropdown).not_to_contain_text('不可选其他室')
            dropdown.get_by_text('桩启用室（R1）',exact=True).click()
            page.get_by_role('button',name=re.compile('生成核验链接$')).click()
            expect(page.get_by_label('刚生成的核验链接')).to_be_visible()
            assert page.evaluate('pvState.lastCreate.project_id==="p1"&&pvState.lastCreate.room_id==="r1"&&pvState.lastCreate.expires_in_minutes===60')
            assert page.evaluate('!Object.values(localStorage).some(v=>v.includes(pvState.token))')
            page.screenshot(path=str(ROOT/'participant-verification-admin.png'),full_page=True,mask=[page.locator('.pv-fresh-link')])
            passed('管理员登记门槛、成员项目/启用室筛选、生成字段和临时链接')
            page.get_by_role('button',name=re.compile('刷新链接与结果$')).click()
            expect(page.get_by_label('刚生成的核验链接')).to_have_count(0)
            expect(page.get_by_role('button',name=re.compile('撤\\s*销$'))).to_be_enabled()
            page.get_by_role('button',name=re.compile('撤\\s*销$')).click()
            page.get_by_role('button',name='确认撤销',exact=True).click()
            expect(page.get_by_text('已撤销',exact=True)).to_be_visible()
            passed('刷新不重现原始令牌，active链接确认撤销')
            page.evaluate('pvState.delayCreate=true')
            page.get_by_role('button',name=re.compile('生成核验链接$')).click()
            page.wait_for_function('!!pvState.finishCreate')
            page.evaluate('pvAdmin("b")')
            page.evaluate('pvState.finishCreate();pvState.delayCreate=false')
            expect(page.get_by_text('尚未登记底照',exact=True)).to_be_visible()
            expect(page.get_by_label('刚生成的核验链接')).to_have_count(0)
            passed('管理员切换人员忽略迟到生成响应、链接仅创建响应内存')
            assert not report['page_errors']
            report.update(status='PASS',remaining_object_urls=page.evaluate('pvState.urlSet.size'))
        except Exception as cause:
            # 不能输出含 Authorization/fragment 的Playwright异常详情。
            safe_error=re.sub(r'[A-Za-z0-9_-]{43,}', '[REDACTED]', str(cause))
            report.update(status='FAIL',error=safe_error)
            page.screenshot(path=str(ROOT/'participant-verification-frontend-failure.png'),full_page=True,mask=[page.locator('.pv-fresh-link')])
            raise AssertionError('人员核验前端桩检查失败（敏感入口详情不输出）') from None
        finally:
            (ROOT/'participant-verification-frontend-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            browser.close();server.shutdown();server.server_close()


if __name__=='__main__': main()
