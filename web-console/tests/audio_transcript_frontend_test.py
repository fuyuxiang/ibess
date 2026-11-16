"""音频转写面板独立前端桩：无正式API/管理员/识别模型写入。

内存生成静音PCM WAV仅验证播放器，不代表真实语音或识别结果。
python -B tests/audio_transcript_frontend_test.py
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import subprocess
import threading

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent
WEB = ROOT.parent
BUILD = r"""
const esbuild=require('esbuild');
const entry=`import React from'react';import{createRoot}from'react-dom/client';import AudioTranscriptPanel from'./src/components/audio/AudioTranscriptPanel';function Harness(){const[open,setOpen]=React.useState(true);window.audioPanelOpen=setOpen;return open?<AudioTranscriptPanel/>:<div>独立音频桩</div>};createRoot(document.getElementById('root')).render(<React.StrictMode><Harness/></React.StrictMode>);`;
esbuild.build({stdin:{contents:entry,resolveDir:process.cwd(),loader:'tsx'},bundle:true,write:false,format:'iife',jsx:'automatic',target:'es2020',loader:{'.css':'empty'},plugins:[{name:'auth-stub',setup(b){b.onResolve({filter:/^\.\/auth$/},args=>args.importer.endsWith('audioRecordings.ts')?{path:'auth',namespace:'stub'}:null);b.onLoad({filter:/.*/,namespace:'stub'},()=>({contents:'export const authFetch=(...args)=>window.audioStub(...args)',loader:'js'}))}}]}).then(r=>process.stdout.write(r.outputFiles[0].text)).catch(e=>{process.stderr.write(String(e));process.exit(1)});
"""
STUB = r"""
const rec=(id,title,status='ready')=>({recording_id:id,title,source_id:'stub-'+id,source_type:'upload',status,duration_ms:3000,created_at:'2026-10-09T01:00:00Z',processed_at:status==='ready'?'2026-10-09T01:01:00Z':null,model:status==='ready'?'PARAFORMER-STUB':null,speaker_model:status==='ready'?'CAMPP-STUB':null,speaker_count:status==='ready'?2:null,segment_count:status==='ready'?4:0,sha256:'frontend-test-sha',error:status==='failed'?'测试模型未加载':null,playback_url:status==='ready'?'/private-audio':null});
const segments=[{segment_id:'s1',speaker:'A',start_ms:125,end_ms:900,text:'前端桩第一段',playback_url:'/private'},{segment_id:'s2',speaker:'B',start_ms:1000.25,end_ms:1500.75,text:'前端桩第二段',playback_url:'/private'},{segment_id:'s3',speaker:null,start_ms:1600,end_ms:2200,text:'前端桩未知说话人',playback_url:'/private'},{segment_id:'bad',speaker:'Z',start_ms:2500,end_ms:4000,text:'越界桩片段不得显示',playback_url:'/private'}];
window.audioState={records:[rec('r1','已完成录音'),rec('r2','无人声录音'),rec('r3','失败录音','failed')],calls:[],inFlight:{},maxFlight:{},urls:new Set(),uploadCount:0,retryCount:0};const s=audioState;
const create=URL.createObjectURL.bind(URL),revoke=URL.revokeObjectURL.bind(URL);URL.createObjectURL=blob=>{const url=create(blob);s.urls.add(url);return url};URL.revokeObjectURL=url=>{s.urls.delete(url);revoke(url)};
const wav=()=>{const rate=8000,n=24000,buffer=new ArrayBuffer(44+n*2),v=new DataView(buffer);const ascii=(at,text)=>{for(let i=0;i<text.length;i++)v.setUint8(at+i,text.charCodeAt(i))};ascii(0,'RIFF');v.setUint32(4,36+n*2,true);ascii(8,'WAVE');ascii(12,'fmt ');v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);v.setUint32(24,rate,true);v.setUint32(28,rate*2,true);v.setUint16(32,2,true);v.setUint16(34,16,true);ascii(36,'data');v.setUint32(40,n*2,true);return buffer};
window.audioStub=async(url,init={})=>{const method=init.method||'GET';s.calls.push({url,method,multipart:init.body instanceof FormData});const key=method+' '+url;s.inFlight[key]=(s.inFlight[key]||0)+1;s.maxFlight[key]=Math.max(s.maxFlight[key]||0,s.inFlight[key]);try{
if(url==='/api/audio/recordings?limit=30'){if(s.delayList)await new Promise((resolve,reject)=>{s.finishList=resolve;init.signal?.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')),{once:true})});if(s.listError)return Response.json({detail:'测试列表读取失败'},{status:503});return Response.json({items:s.records})};
if(method==='POST'&&url==='/api/audio/recordings'){s.uploadCount++;const row=rec('uploaded','上传待处理录音','queued');s.records.unshift(row);s.progress=0;return Response.json(row,{status:202})};
const id=url.match(/recordings\/([^/]+)/)?.[1];const row=s.records.find(r=>r.recording_id===id);
if(url.endsWith('/retry')){s.retryCount++;row.status='queued';row.error=null;s.retryProgress=0;return Response.json(row)};
if(url.endsWith('/playback')){if(s.playError)return Response.json({detail:'测试原音读取失败'},{status:500});if(s.delayPlayback)await new Promise(resolve=>s.finishPlayback=resolve);return new Response(wav(),{headers:{'content-type':'audio/wav'}})};
if(s.delayDetail===id)await new Promise(resolve=>s.finishDetail=resolve);
if(id==='uploaded'){s.progress++;row.status=s.progress===1?'queued':s.progress===2?'processing':'ready';if(row.status==='ready')Object.assign(row,rec(id,row.title))};
if(id==='r3'&&row.status!=='failed'){s.retryProgress++;row.status=s.retryProgress===1?'queued':'ready';if(row.status==='ready')Object.assign(row,rec(id,row.title))};
return Response.json({...row,speaker_count:id==='r2'?0:row.speaker_count,segment_count:id==='r2'?0:row.segment_count,segments:row.status==='ready'&&id!=='r2'?segments:[]});
}finally{s.inFlight[key]--}};
"""


def main():
    result = subprocess.run(['node', '-e', BUILD],cwd=WEB,capture_output=True,text=True,encoding='utf-8')
    if result.returncode: raise RuntimeError(result.stderr)
    bundle=result.stdout.encode('utf-8')
    css=(WEB/'src/audio-transcript.css').read_text(encoding='utf-8')
    html=('<!DOCTYPE html><html><head><meta charset="utf-8"><style>body{margin:0;padding:24px;font-family:"Microsoft YaHei",sans-serif;background:#f4f8fc}'+css+'</style></head><body><div id="root"></div><script>'+STUB+'</script><script src="/bundle.js"></script></body></html>').encode('utf-8')
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body,mime=(bundle,'application/javascript') if self.path=='/bundle.js' else (html,'text/html')
            self.send_response(200);self.send_header('Content-Type',mime+';charset=utf-8');self.end_headers();self.wfile.write(body)
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    report={'scope':'临时前端桩，静音WAV，仅验证UI与生命周期、不验证识别模型','checks':[],'page_errors':[],'status':'RUNNING'}
    def passed(name): report['checks'].append(name); print('PASS',name,flush=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1366,'height':1100})
        page.on('pageerror',lambda error:report['page_errors'].append(str(error)))
        try:
            page.goto(f'http://127.0.0.1:{server.server_port}',wait_until='load')
            expect(page.locator('.at-detail-heading h3')).to_have_text('已完成录音')
            expect(page.locator('.at-segment-row')).to_have_count(3)
            expect(page.get_by_text('说话人 A',exact=True)).to_be_visible()
            expect(page.get_by_text('说话人 B',exact=True)).to_be_visible()
            expect(page.get_by_text('说话人未确定',exact=True)).to_be_visible()
            expect(page.get_by_text('越界桩片段不得显示',exact=True)).to_have_count(0)
            expect(page.locator('.at-segment-row').nth(1)).to_contain_text('1.000 秒 — 1.501 秒')
            passed('后端A/B/未知标签与相对小数时间，过滤越界片段不补造')
            def choose(title):
                page.locator('.at-recording-choice').filter(has_text=title).click()
                expect(page.locator('.at-detail-heading h3')).to_have_text(title)
            page.locator('.at-segment-row').nth(0).get_by_role('button',name=re.compile('回放此片段$')).click()
            expect(page.get_by_label('录音原音播放器')).to_be_visible()
            page.wait_for_function('document.querySelector("audio")&&!document.querySelector("audio").paused')
            page.evaluate('window.previousAudio=document.querySelector("audio")')
            page.locator('.at-segment-row').nth(1).get_by_role('button',name=re.compile('回放此片段$')).click()
            page.wait_for_function('previousAudio.paused')
            expect(page.locator('audio')).to_have_count(1)
            expect(page.locator('.at-player-title')).to_contain_text('说话人 B')
            assert page.evaluate('audioState.urls.size===1')
            assert page.locator('audio').get_attribute('src').startswith('blob:')
            passed('鉴权片段blob回放，点击新段先停旧音频，只有一个播放器和URL')
            page.get_by_role('button',name=re.compile('播放整条原音$')).click()
            expect(page.locator('.at-player-title')).to_contain_text('整条录音')
            page.get_by_role('button',name=re.compile('停止回放$')).click()
            expect(page.locator('audio')).to_have_count(0)
            page.wait_for_function('audioState.urls.size===0')
            passed('整条原音回放与停止释放URL')
            page.evaluate('audioState.playError=true')
            page.get_by_role('button',name=re.compile('播放整条原音$')).click()
            expect(page.get_by_text('原音加载失败：测试原音读取失败',exact=True)).to_be_visible()
            page.evaluate('audioState.playError=false;audioState.delayPlayback=true')
            page.get_by_role('button',name=re.compile('播放整条原音$')).click()
            page.wait_for_function('!!audioState.finishPlayback')
            choose('无人声录音')
            page.evaluate('audioState.finishPlayback();audioState.delayPlayback=false')
            page.wait_for_function('audioState.urls.size===0')
            expect(page.locator('audio')).to_have_count(0)
            expect(page.get_by_text('无有效转写片段。可能没有识别人声，页面不补造说话人或文字。',exact=True)).to_be_visible()
            passed('回放错误具体提示，切换忽略迟到blob，无人声合法空结果')
            page.evaluate('audioState.delayDetail="r1"')
            page.locator('.at-recording-choice').filter(has_text='已完成录音').click()
            page.wait_for_function('!!audioState.finishDetail')
            choose('失败录音')
            page.evaluate('audioState.finishDetail();audioState.delayDetail=null')
            expect(page.locator('.at-detail-heading h3')).to_have_text('失败录音')
            expect(page.get_by_text('测试模型未加载',exact=True)).to_be_visible()
            page.get_by_role('button',name='重试识别',exact=True).click()
            expect(page.get_by_role('button',name='重试识别',exact=True)).to_have_count(0)
            page.wait_for_function('audioState.retryCount===1')
            expect(page.locator('.at-main-card')).to_contain_text('识别完成',timeout=12000)
            passed('切换忽略迟到详情，failed才可手动重试且不重复排队')
            page.get_by_label('选择 PCM WAV 文件').set_input_files({'name':'not.wav.mp3','mimeType':'audio/mpeg','buffer':b'not wav'})
            expect(page.get_by_text('仅支持 PCM WAV 文件，请将录音转换为 WAV 后上传。',exact=True)).to_be_visible()
            expect(page.get_by_role('button',name='上传并开始识别',exact=True)).to_be_disabled()
            page.get_by_label('选择 PCM WAV 文件').set_input_files({'name':'frontend-only.wav','mimeType':'audio/wav','buffer':b'frontend stub'})
            page.get_by_role('button',name='上传并开始识别',exact=True).click()
            expect(page.get_by_text('PCM WAV 已上传并入库，识别尚未完成；请查看后台处理状态。',exact=True)).to_be_visible()
            expect(page.locator('.at-detail-heading h3')).to_have_text('上传待处理录音')
            expect(page.locator('.at-main-card')).to_contain_text('等待处理')
            expect(page.locator('.at-main-card')).to_contain_text('正在识别',timeout=7000)
            expect(page.locator('.at-main-card')).to_contain_text('识别完成',timeout=7000)
            assert page.evaluate('audioState.uploadCount===1&&audioState.calls.some(c=>c.method==="POST"&&c.url==="/api/audio/recordings"&&c.multipart)')
            assert page.evaluate('Object.values(audioState.maxFlight).every(n=>n===1)')
            passed('WAV前置校验，上传202为queued，轮询processing/ready且同路径无重叠')
            page.evaluate('audioState.listError=true')
            page.get_by_role('button',name=re.compile('刷新录音与状态$')).click()
            expect(page.get_by_text('录音列表加载失败：测试列表读取失败',exact=True)).to_be_visible()
            page.evaluate('audioState.listError=false')
            page.get_by_role('button',name='重试查询',exact=True).click()
            expect(page.get_by_text('录音列表加载失败：测试列表读取失败',exact=True)).to_have_count(0)
            passed('列表查询失败保留录音和明确重试入口')
            for width in (1366,1600,1920):
                page.set_viewport_size({'width':width,'height':1100})
                assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+2')
            page.screenshot(path=str(ROOT/'audio-transcript-frontend.png'),full_page=True)
            passed('1366/1600/1920桌面宽区无页面横向溢出')
            page.evaluate('audioState.delayPlayback=true;audioState.finishPlayback=null')
            page.get_by_role('button',name=re.compile('播放整条原音$')).click()
            page.wait_for_function('!!audioState.finishPlayback')
            page.evaluate('audioPanelOpen(false)')
            expect(page.locator('.audio-transcript-panel')).to_have_count(0)
            page.evaluate('audioState.finishPlayback();audioState.delayPlayback=false')
            page.wait_for_function('audioState.urls.size===0')
            assert not report['page_errors']
            report.update(status='PASS',remaining_object_urls=0,max_concurrent_same_path=page.evaluate('Math.max(...Object.values(audioState.maxFlight))'))
            passed('卸载中止轮询/回放，迟到音频不创建URL，无JS异常')
        except Exception as error:
            report.update(status='FAIL',error=str(error))
            page.screenshot(path=str(ROOT/'audio-transcript-frontend-failure.png'),full_page=True)
            raise
        finally:
            (ROOT/'audio-transcript-frontend-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            browser.close();server.shutdown();server.server_close()


if __name__=='__main__': main()
