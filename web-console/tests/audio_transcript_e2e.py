"""录音上传→真模型分段→私有音频回放，专用16000隔离环境。"""
import io
import json
import os
from pathlib import Path
import re
import wave

from playwright.sync_api import expect, sync_playwright

ROOT=Path(__file__).resolve().parent
FIXTURE=ROOT.parents[1]/'services/audio-worker/tests/two_speaker_fixture.wav'
BASE='http://localhost:16000'


def main():
    if os.environ.get('AUDIO_E2E_ISOLATED')!='1':
        raise RuntimeError('需要显式确认16000专用临时环境')
    report={'status':'RUNNING','scope':'真实模型，官方样例拼接，临时PG schema及音频volume，无现场音频','checks':[],'page_errors':[]}
    def passed(name):
        report['checks'].append(name); print('PASS',name,flush=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        context=browser.new_context(viewport={'width':1600,'height':1200})
        page=context.new_page()
        page.on('pageerror',lambda error:report['page_errors'].append(str(error)))
        headers={'X-Requested-With':'XMLHttpRequest','Origin':BASE}
        try:
            state=context.request.get(BASE+'/api/auth/status').json()
            credentials={'username':'test_admin','password':'IntegrationTest12345'}
            endpoint='/api/auth/login' if state['initialized'] else '/api/auth/setup'
            r=context.request.post(BASE+endpoint,data={**credentials,**({'display_name':'隔离音频测试'} if not state['initialized'] else {})},headers=headers)
            assert r.status in (200,201),f'HTTP {r.status}'
            headers['X-CSRF-Token']=r.json()['csrf_token']
            page.goto(BASE+'/audio',wait_until='load')
            panel=page.locator('.audio-transcript-panel')
            expect(panel).to_be_visible()
            page.get_by_label('选择 PCM WAV 文件').set_input_files(str(FIXTURE))
            with page.expect_response(lambda r:r.url==BASE+'/api/audio/recordings' and r.request.method=='POST') as response:
                page.get_by_role('button',name='上传并开始识别',exact=True).click()
            assert response.value.status==202
            record=response.value.json();identifier=record['recording_id']
            expect(panel.locator('.at-segment-row')).to_have_count(9,timeout=120000)
            detail=context.request.get(BASE+'/api/audio/recordings/'+identifier).json()
            assert detail['status']=='ready' and detail['speaker_count']==2
            assert {seg['speaker'] for seg in detail['segments']}=={'A','B'}
            expect(panel.locator('.at-segment-list')).to_contain_text('说话人 A')
            expect(panel.locator('.at-segment-list')).to_contain_text('说话人 B')
            passed('上传后真实CAM++生成9段，匿名A/B与相对时间在页面显示')

            first=detail['segments'][0]
            full=context.request.get(BASE+detail['playback_url'])
            clip=context.request.get(BASE+first['playback_url'])
            assert full.status==clip.status==200
            assert clip.headers['cache-control']=='no-store'
            with wave.open(io.BytesIO(full.body()),'rb') as f:
                rate=f.getframerate();f.setpos(round(first['start_ms']*rate/1000))
                expected=f.readframes(round(first['end_ms']*rate/1000)-round(first['start_ms']*rate/1000))
            with wave.open(io.BytesIO(clip.body()),'rb') as f:
                assert f.readframes(f.getnframes())==expected
            page.get_by_role('button',name=re.compile('回放此片段$')).first.click()
            expect(page.locator('audio[aria-label="录音原音播放器"]')).to_be_visible()
            page.wait_for_function("() => {const a=document.querySelector('audio[aria-label=\"录音原音播放器\"]');return a && Number.isFinite(a.duration) && a.duration>0}")
            duration=page.locator('audio').evaluate('(a)=>a.duration')
            assert abs(duration-(first['end_ms']-first['start_ms'])/1000)<0.002
            passed('原音片段采样与全片对应区间完全一致；浏览器可解码播放')

            page.get_by_role('button',name=re.compile('播放整条原音$')).click()
            page.wait_for_function("() => {const a=document.querySelector('audio[aria-label=\"录音原音播放器\"]');return a && Number.isFinite(a.duration) && a.duration>20}")
            assert abs(page.locator('audio').evaluate('(a)=>a.duration')-detail['duration_ms']/1000)<0.002
            page.get_by_role('button',name=re.compile('停止回放$')).click()
            expect(page.locator('audio')).to_have_count(0)
            passed('整条原音可播放、停止清空播放器，不同时播放多段')
            page.screenshot(path=str(ROOT/'audio-transcript-real-model.png'),full_page=True,animations='disabled')
            for width in [1366,1600,1920]:
                page.set_viewport_size({'width':width,'height':1150})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
            passed('1366、1600、1920桌面无横向溢出')

            with browser.new_context() as anon:
                for path in ['/api/audio/recordings', '/api/audio/recordings/'+identifier,first['playback_url'],detail['playback_url']]:
                    assert anon.request.get(BASE+path).status==401
            passed('录音、文字及片段原音均需管理员认证')
            page.reload(wait_until='load')
            expect(panel).to_be_visible()
            page.locator('.at-recording-choice').filter(has_text='two_speaker_fixture.wav').first.click()
            expect(panel.locator('.at-segment-list')).to_contain_text('说话人 B')
            passed('刷新后转写与说话人分组仍由后台持久化记录恢复')

            page.get_by_label('选择 PCM WAV 文件').set_input_files({'name':'invalid.wav','mimeType':'audio/wav','buffer':b'fake wave'})
            with page.expect_response(lambda r:r.url==BASE+'/api/audio/recordings' and r.request.method=='POST') as response:
                page.get_by_role('button',name='上传并开始识别',exact=True).click()
            assert response.value.status==422
            expect(panel).to_contain_text('请上传完整的 PCM WAV 音频')
            passed('伪造WAV被拒绝，不伪造文字或说话人')
            assert not report['page_errors'],report['page_errors']
            report['status']='PASS'
            report['model']=detail['model'];report['speaker_model']=detail['speaker_model']
            report['segments']=[{k:s[k] for k in ['speaker','start_ms','end_ms']} for s in detail['segments']]
        except Exception:
            report['status']='FAIL'
            page.screenshot(path=str(ROOT/'audio-transcript-e2e-failure.png'),full_page=True)
            raise
        finally:
            (ROOT/'audio-transcript-e2e-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            context.close();browser.close()


if __name__=='__main__':main()
