"""3.3区域编辑器隔离浏览器测试：实际React/Ant Design组件+API内存桩。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading

from playwright.sync_api import expect, sync_playwright

WEB = Path(__file__).resolve().parents[1]
BUILD = r"""
const esbuild=require('esbuild');
const entry=`import React from 'react';import{createRoot}from'react-dom/client';import ZoneEditor from './src/components/stages/ZoneEditor';const config={project_id:'p1',room_id:'r1',session_id:'s1',source_id:'camera-1',config_version:0,zones:[],entry_lines:[]};const seats=[{seat_id:'seat-a',room_id:'r1',project_id:'p1',code:'A01',enabled:true}];const snapshot=location.pathname==='/missing'?null:'data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="900"><rect width="100%" height="100%" fill="#dce5eb"/><path d="M0 800L1600 800" stroke="#768999" stroke-width="5"/></svg>');createRoot(document.getElementById('root')).render(<ZoneEditor snapshotUrl={snapshot} config={config} seats={seats}/>);`;
esbuild.build({stdin:{contents:entry,resolveDir:process.cwd(),loader:'tsx'},bundle:true,write:false,outdir:'zone-test-memory',format:'iife',jsx:'automatic',target:'es2020',plugins:[{name:'zone-stubs',setup(b){b.onResolve({filter:/^\.\/auth$/},()=>({path:'auth',namespace:'zone-stub'}));b.onResolve({filter:/api\/registry$/},()=>({path:'registry',namespace:'zone-stub'}));b.onLoad({filter:/.*/,namespace:'zone-stub'},a=>({contents:a.path==='auth'?'export const authFetch=(...args)=>window.zoneFetch(...args)':'export const fetchRegistry=()=>window.zoneRegistryFetch()',loader:'js'}))}}]}).then(r=>process.stdout.write(JSON.stringify(r.outputFiles.map(f=>({path:f.path,text:f.text}))))).catch(e=>{console.error(e);process.exit(1)});
"""
STUB = r"""
window.zoneCalls=[];window.zoneFetch=async(path,options={})=>{const body=options.body?JSON.parse(options.body):null;window.zoneCalls.push({path,method:options.method||'GET',body});if(options.method==='PUT')return Response.json({...window.zoneConfig,...body,config_version:window.zoneConfig.config_version+1},{status:200});return Response.json(window.zoneConfig)};
window.zoneConfig={project_id:'p1',room_id:'r1',session_id:'s1',source_id:'camera-1',config_version:0,zones:[],entry_lines:[]};window.zoneRegistryFetch=async()=>({seats:[]});
"""


def run():
    build = subprocess.run(['node', '-e', BUILD], cwd=WEB, capture_output=True, text=True, encoding='utf-8', check=True)
    output = json.loads(build.stdout)
    script = next(item['text'] for item in output if item['path'].endswith('.js'))
    css = '\n'.join(item['text'] for item in output if item['path'].endswith('.css'))
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            if self.path == '/app.js': payload, content = script.encode(), 'application/javascript'
            else:
                payload = (f'<!doctype html><meta charset="utf-8"><style>body{{margin:0;padding:20px;background:#f7f8fa;font-family:Microsoft YaHei,Arial,sans-serif}}*{{box-sizing:border-box}}{css}</style><div id="root"></div><script>{STUB}</script><script src="/app.js"></script>').encode()
                content = 'text/html; charset=utf-8'
            self.send_response(200); self.send_header('Content-Type', content); self.end_headers(); self.wfile.write(payload)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    screenshots = Path(tempfile.mkdtemp(prefix='poc-zone-editor-'))
    checks = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1366, 'height': 1000})
            errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/missing')
            expect(page.get_by_text('未提供画面快照', exact=True)).to_be_visible()
            expect(page.get_by_role('button', name='保存区域配置', exact=True)).to_be_disabled()
            assert page.evaluate('zoneCalls.filter(call=>call.method==="PUT").length') == 0
            checks.append('没有快照时禁用保存，不生成区域写请求')

            page.goto(f'http://127.0.0.1:{server.server_port}/editor')
            expect(page.get_by_text('静态快照已加载', exact=True)).to_be_visible()
            page.get_by_role('combobox', name='关联工位').click()
            page.get_by_text('A01', exact=True).click()
            svg = page.locator('.zone-overlay')
            bounds = svg.bounding_box()
            for x, y in ((.12,.16),(.42,.16),(.42,.46),(.12,.46)):
                page.mouse.click(bounds['x']+bounds['width']*x,bounds['y']+bounds['height']*y)
            expect(page.get_by_role('button', name='完成区域', exact=True)).to_be_enabled()
            page.get_by_role('button', name='完成区域', exact=True).click()
            expect(page.locator('.zone-shape-seat polygon')).to_have_count(1)
            polygon_before = page.locator('.zone-shape-seat polygon').get_attribute('points')
            for width in (1366, 1600, 1920):
                page.set_viewport_size({'width': width, 'height': 1000})
                expect(page.locator('.zone-shape-seat polygon')).to_have_attribute('points', polygon_before)
                assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'), width
            checks.append('归一化多边形随视口缩放保持坐标不变')

            page.get_by_text('出入口线', exact=True).click()
            page.locator('.zone-tool-panel .ant-select-selector').last.click()
            page.get_by_text('进入评标区', exact=True).click()
            bounds = page.locator('.zone-overlay').bounding_box()
            page.mouse.click(bounds['x']+bounds['width']*.78,bounds['y']+bounds['height']*.18)
            page.mouse.click(bounds['x']+bounds['width']*.78,bounds['y']+bounds['height']*.82)
            expect(page.get_by_role('button', name='完成门线', exact=True)).to_be_enabled()
            page.get_by_role('button', name='完成门线', exact=True).click()
            expect(page.locator('.zone-entry-line line')).to_have_count(1)
            await_save = page.get_by_role('button', name='保存区域配置', exact=True)
            expect(await_save).to_be_enabled()
            await_save.click()
            expect(page.get_by_text('区域配置已保存，版本 v1', exact=True)).to_be_visible()
            body = page.evaluate('zoneCalls.find(call=>call.method==="PUT").body')
            assert body['config_version'] == 0 and len(body['zones']) == 1 and len(body['entry_lines']) == 1
            assert body['zones'][0]['seat_id'] == 'seat-a'
            assert len(body['zones'][0]['polygon']) == 4
            assert body['entry_lines'][0]['direction'] == 'in'
            checks.append('工位区/出入口线保存版本、工位关联与归一化顶点')
            page.screenshot(path=str(screenshots/'zone-editor-1920.png'), full_page=True)

            page.locator('[aria-label^="删除"]').first.click()
            expect(page.locator('.zone-shape-seat polygon')).to_have_count(0)
            assert not errors, errors
            checks.append('区域可删除，浏览器无JavaScript异常')
            browser.close()
        print(json.dumps({'passed':len(checks),'checks':checks,'nature':'isolated browser with in-memory API and a static SVG snapshot','screenshots':str(screenshots)},ensure_ascii=False))
    finally: server.shutdown(); server.server_close()


if __name__ == '__main__': run()
