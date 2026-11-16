"""独立前端桩验收；内存打包与临时HTTP服务，不访问业务API、不创建账号/真人资料。

python -B tests/face_match_frontend_test.py
桩图片为1px纯色，仅验证交互与生命周期，不验证人脸推理。
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
const entry = `import React from 'react'; import {createRoot} from 'react-dom/client'; import FaceMatchModal from './src/components/face/FaceMatchModal';
function Harness(){const [person,setPerson]=React.useState(null);window.faceSelect=(id,enabled=true)=>setPerson(id?{person_id:id,name:'前端桩人员-'+id,code:'TEST-'+id,unit:'无真人资料',role:'expert',enabled}:null);return <FaceMatchModal person={person} onClose={()=>setPerson(null)}/>};createRoot(document.getElementById('root')).render(<Harness/>);`;
esbuild.build({stdin:{contents:entry,resolveDir:process.cwd(),loader:'tsx'},bundle:true,write:false,format:'iife',target:'es2020',jsx:'automatic',loader:{'.css':'empty'},plugins:[{name:'stub-auth',setup(build){build.onResolve({filter:/^\.\/auth$/},args=>args.importer.endsWith('faces.ts')?{path:'auth',namespace:'stub'}:null);build.onLoad({filter:/.*/,namespace:'stub'},()=>({contents:'export const authFetch = (...args) => window.faceStubFetch(...args)',loader:'js'}));}}]}).then(r=>process.stdout.write(r.outputFiles[0].text)).catch(e=>{process.stderr.write(String(e));process.exit(1)});
"""

STUB = r"""
window.faceState={registered:{a:true,b:false},version:'v1',ready:true,calls:[],cameraCalls:0,stops:0,urls:new Set(),errors:[],delayVerify:false,delayProfile:false};
const state=window.faceState;
const createURL=URL.createObjectURL.bind(URL),revokeURL=URL.revokeObjectURL.bind(URL);
URL.createObjectURL=blob=>{const url=createURL(blob);state.urls.add(url);return url};
URL.revokeObjectURL=url=>{state.urls.delete(url);revokeURL(url)};
const pixel=Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aB9sAAAAASUVORK5CYII='),c=>c.charCodeAt(0));
window.faceStubFetch=async(url,init={})=>{
state.calls.push({url,method:init.method||'GET',multipart:init.body instanceof FormData});
const id=url.match(/persons\/([^/]+)/)?.[1];
if(url.endsWith('/status'))return Response.json({ready:state.ready,model:'FRONTEND-STUB',threshold:0.363,message:state.ready?'就绪':'测试模型未就绪'});
if(url.endsWith('/photo'))return new Response(pixel,{headers:{'content-type':'image/png'}});
if(url.endsWith('/verify')){if(state.delayVerify)await new Promise(resolve=>state.finishVerify=resolve);if(state.verifyError)return Response.json({detail:state.verifyError},{status:422});return Response.json({person_id:id,person_name:'前端桩人员-'+id,matched:true,similarity:0.8,threshold:0.363,model:'FRONTEND-STUB',checked_at:'2026-10-09T01:00:00Z',reference_version:state.version,liveness_checked:false})};
if(init.method==='DELETE'){state.registered[id]=false;return Response.json({ok:true})};
if(init.method==='PUT'){state.registered[id]=true;state.version='v2'};
if(state.delayProfile&&id==='a')await new Promise(resolve=>state.finishProfile=resolve);
return Response.json({person_id:id,registered:!!state.registered[id],registered_at:state.registered[id]?'2026-10-09T00:00:00Z':null,model:'FRONTEND-STUB',photo_url:'/api/faces/persons/'+id+'/photo',version:state.registered[id]?state.version:null});
};
const media = {getUserMedia:async()=>{state.cameraCalls++;if(state.cameraDenied)throw new DOMException('test','NotAllowedError');if(state.delayCamera)await new Promise(resolve=>state.finishCamera=resolve);const canvas=document.createElement('canvas');canvas.width=320;canvas.height=240;const context=canvas.getContext('2d');context.fillStyle='white';context.fillRect(0,0,320,240);const stream=canvas.captureStream(15);for(const track of stream.getTracks()){const stop=track.stop.bind(track);track.stop=()=>{state.stops++;stop()}}return stream}};
Object.defineProperty(navigator,'mediaDevices',{configurable:true,value:media});
"""


def main():
    output = subprocess.run(['node', '-e', BUILD], cwd=WEB, capture_output=True, text=True, encoding='utf-8')
    if output.returncode:
        raise RuntimeError(output.stderr)
    bundle = output.stdout.encode('utf-8')
    css = (WEB / 'src/face-match.css').read_text(encoding='utf-8')
    html = ('<!DOCTYPE html><html><head><meta charset="utf-8"><style>body{margin:0;font-family:"Microsoft YaHei",sans-serif;background:#f3f7fc}' + css + '</style></head><body><div id="root"></div><script>' + STUB + '</script><script src="/bundle.js"></script></body></html>').encode('utf-8')
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body, kind = (bundle, 'application/javascript') if self.path == '/bundle.js' else (html, 'text/html')
            self.send_response(200); self.send_header('Content-Type', kind + '; charset=utf-8'); self.end_headers(); self.wfile.write(body)
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    report = {'scope': '独立前端桩，无管理员/真实人脸/业务API写入，不验证真实推理', 'checks': [], 'page_errors': [], 'status': 'RUNNING'}
    def passed(name):
        report['checks'].append(name); print('PASS', name, flush=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1366,'height':1100})
        page.on('pageerror', lambda error: report['page_errors'].append(str(error)))
        try:
            page.goto(f'http://127.0.0.1:{server.server_port}', wait_until='load')
            page.wait_for_function("typeof faceSelect==='function'")
            def select(person='a', enabled=True):
                page.evaluate('([id,enabled])=>faceSelect(id,enabled)', [person,enabled])
                if person:
                    expect(page.get_by_role('button',name=re.compile('刷新登记信息$'))).to_be_enabled()
                    expect(page.locator('.face-person-bar')).to_contain_text('前端桩人员-'+person)
                else:
                    expect(page.locator('.face-match-session')).to_have_count(0)
            def upload(kind):
                page.get_by_label('选择登记照片' if kind=='reference' else '上传候选照片').set_input_files({'name':'frontend-stub.png','mimeType':'image/png','buffer':PIXEL})
            select()
            expect(page.locator('.face-photo-stage img')).to_have_count(1)
            assert page.locator('.face-photo-stage img').get_attribute('src').startswith('blob:')
            assert page.evaluate('faceState.cameraCalls') == 0
            expect(page.get_by_role('button',name=re.compile('开始一对一核验$'))).to_be_disabled()
            passed('登记照片鉴权blob；未点击不启用摄像头；无候选禁核验')

            upload('candidate')
            page.get_by_role('button',name=re.compile('开始一对一核验$')).click()
            expect(page.locator('.face-result-values')).to_contain_text('0.8000')
            expect(page.locator('.face-result-panel')).to_contain_text('非准确率')
            expect(page.locator('.face-result-panel')).to_contain_text('未做活体检测')
            page.screenshot(path=str(ROOT/'face-match-frontend-result-view.png'), full_page=True)
            passed('结果只显示桩响应相似度、门槛及活体限制')

            upload('reference')
            page.get_by_role('button',name='替换登记照片',exact=True).click()
            assert not page.evaluate("faceState.calls.some(c=>c.method==='PUT')")
            page.get_by_role('button',name='确认替换',exact=True).click()
            expect(page.get_by_text('登记照片已保存，旧核验结果已清除。',exact=True)).to_be_visible()
            expect(page.locator('.face-result-values')).to_have_count(0)
            assert page.evaluate("faceState.calls.some(c=>c.method==='PUT'&&c.multipart)")
            passed('替换登记确认后才multipart保存，清除旧结果')

            page.evaluate('faceState.verifyError="照片中检测到多张脸"')
            page.get_by_role('button',name=re.compile('开始一对一核验$')).click()
            expect(page.get_by_text('照片中检测到多张脸',exact=True)).to_be_visible()
            expect(page.locator('.face-result-values')).to_have_count(0)
            page.evaluate('faceState.verifyError=null;faceState.delayVerify=true')
            page.get_by_role('button',name=re.compile('开始一对一核验$')).click()
            page.wait_for_function('!!faceState.finishVerify')
            expect(page.get_by_role('button',name=re.compile('开始一对一核验$'))).to_be_disabled()
            select('b')
            page.evaluate('faceState.finishVerify();faceState.delayVerify=false')
            expect(page.locator('.face-result-values')).to_have_count(0)
            expect(page.get_by_role('button',name=re.compile('开始一对一核验$'))).to_be_disabled()
            passed('多脸错误不造分数；忙碌禁重复；切换人员忽略迟到核验')

            select(None)
            page.wait_for_function('faceState.urls.size===0')
            page.evaluate('faceState.delayProfile=true')
            page.evaluate('faceSelect("a")')
            page.wait_for_function('!!faceState.finishProfile')
            select('b')
            page.evaluate('faceState.finishProfile();faceState.delayProfile=false')
            expect(page.locator('.face-person-bar')).to_contain_text('前端桩人员-b')
            expect(page.locator('.face-photo-stage img')).to_have_count(0)
            passed('切换人员忽略迟到profile/photo，关闭释放全部objectURL')

            select(None); select('a',False)
            expect(page.get_by_label('选择登记照片')).to_be_disabled()
            expect(page.get_by_role('button',name=re.compile('开始一对一核验$'))).to_be_disabled()
            select(None); page.evaluate('faceState.ready=false'); select()
            expect(page.get_by_role('button',name=re.compile('开始一对一核验$'))).to_be_disabled()
            expect(page.get_by_text('模型尚未就绪',exact=True)).to_be_visible()
            passed('停用、模型未就绪禁登记/核验')

            select(None); page.evaluate('faceState.ready=true;faceState.cameraDenied=true'); select()
            page.get_by_role('button',name=re.compile('手动启用摄像头$')).click()
            expect(page.get_by_text('摄像头不可用',exact=True)).to_be_visible()
            upload('candidate')
            expect(page.get_by_role('button',name=re.compile('开始一对一核验$'))).to_be_enabled()
            passed('摄像头权限失败明确提示并可上传回退')

            page.evaluate('faceState.cameraDenied=false')
            page.get_by_role('button',name=re.compile('手动启用摄像头$')).click()
            expect(page.get_by_role('button',name=re.compile('拍照作为候选$'))).to_be_enabled()
            stops = page.evaluate('faceState.stops')
            page.get_by_role('button',name=re.compile('拍照作为候选$')).click()
            expect(page.get_by_text('camera-candidate.jpg',exact=True)).to_be_visible()
            assert page.evaluate('faceState.stops') > stops
            page.get_by_role('button',name='清除候选',exact=True).click()
            expect(page.locator('.face-photo-stage img')).to_have_count(1)
            passed('手动摄像头单次拍照停止tracks；清除候选释放预览')

            page.evaluate('faceState.delayCamera=true')
            page.get_by_role('button',name=re.compile('手动启用摄像头$')).click()
            page.wait_for_function('!!faceState.finishCamera')
            select(None)
            stops = page.evaluate('faceState.stops')
            page.evaluate('faceState.finishCamera();faceState.delayCamera=false')
            page.wait_for_function('(previous)=>faceState.stops>previous', arg=stops)
            page.wait_for_function('faceState.urls.size===0')
            passed('关闭弹窗后迟到摄像头授权立即停止tracks')

            select()
            page.get_by_role('button',name=re.compile('删除登记$')).click()
            assert not page.evaluate("faceState.calls.some(c=>c.method==='DELETE')")
            page.get_by_role('button',name='确认删除',exact=True).click()
            expect(page.get_by_text('登记照片与特征已删除，旧核验结果已清除。',exact=True)).to_be_visible()
            expect(page.get_by_role('button',name=re.compile('开始一对一核验$'))).to_be_disabled()
            passed('删除确认后才调用DELETE；清除登记照片和旧结果')
            expect(page.get_by_role('dialog',name='确认删除人脸登记？',exact=True)).not_to_be_visible()
            page.wait_for_timeout(350)
            page.screenshot(path=str(ROOT/'face-match-frontend.png'), full_page=True)
            assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+2')
            assert not report['page_errors'], report['page_errors']
            passed('1366弹窗无横向溢出、无JS异常')
            select(None)
            page.wait_for_function('faceState.urls.size===0')
            report.update(status='PASS',remaining_object_urls=0,camera_stops=page.evaluate('faceState.stops'))
        except Exception as error:
            report.update(status='FAIL',error=str(error))
            page.screenshot(path=str(ROOT/'face-match-frontend-failure.png'),full_page=True)
            raise
        finally:
            (ROOT/'face-match-frontend-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            browser.close(); server.shutdown(); server.server_close()


if __name__ == '__main__':
    main()
