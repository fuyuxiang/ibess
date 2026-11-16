"""真实人员页与后台结果闭环；仅专用15000端口临时schema，报告不保存授权令牌。"""
import io
import json
import os
from pathlib import Path
import re

from PIL import Image, ImageEnhance
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT.parents[1] / 'services/api/tests/face_fixtures'
BASE = 'http://localhost:15000'


def image_payload(image):
    out = io.BytesIO()
    image.save(out, 'PNG')
    return {'name': 'candidate.png', 'mimeType': 'image/png', 'buffer': out.getvalue()}


def main():
    if os.environ.get('PARTICIPANT_E2E_ISOLATED') != '1':
        raise RuntimeError('需要显式确认15000专用隔离环境')
    report = {'status':'RUNNING', 'scope':'临时schema；真实官方模型；公开授权样例；不修改正式账号或底照', 'checks':[], 'page_errors':[]}
    def passed(name):
        report['checks'].append(name)
        print('PASS', name, flush=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        admin = browser.new_context(viewport={'width':1600,'height':1150})
        participant = browser.new_context(viewport={'width':1366,'height':1050})
        admin_page = admin.new_page()
        page = participant.new_page()
        for target in [admin_page, page]:
            target.on('pageerror', lambda error: report['page_errors'].append(str(error)))
        headers = {'X-Requested-With':'XMLHttpRequest','Origin':BASE}
        try:
            status = admin.request.get(BASE + '/api/auth/status').json()
            credentials = {'username':'test_admin','password':'IntegrationTest12345'}
            endpoint = '/api/auth/login' if status['initialized'] else '/api/auth/setup'
            r = admin.request.post(BASE + endpoint, data={**credentials, **({'display_name':'隔离人员核验测试'} if not status['initialized'] else {})}, headers=headers)
            assert r.status in (200,201), f'HTTP {r.status}'
            headers['X-CSRF-Token'] = r.json()['csrf_token']
            code = 'PARTICIPANT-E2E-' + str(os.getpid())
            r = admin.request.post(BASE + '/api/registry/persons', data={'code':code,'name':'人员端核验测试样例','unit':'公开照片专用测试','role':'expert','enabled':True},headers=headers)
            assert r.status == 201
            identity = r.json()
            url = BASE + '/api/faces/persons/' + identity['person_id']
            r = admin.request.put(url, headers=headers, multipart={'file':{'name':'reference.png','mimeType':'image/png','buffer':(FIXTURES/'astronaut.png').read_bytes()}})
            assert r.status == 200, f'registration HTTP {r.status}'
            admin_page.goto(BASE+'/registry',wait_until='load')
            admin_page.get_by_role('tab',name='人员',exact=True).click()
            admin_page.get_by_role('row').filter(has_text=code).get_by_role('button',name='人员核验',exact=True).click()
            expect(admin_page.get_by_role('button',name=re.compile('生成核验链接$'))).to_be_enabled()
            with admin_page.expect_response(lambda r:r.url==url+'/verification-links' and r.request.method=='POST') as response:
                admin_page.get_by_role('button',name=re.compile('生成核验链接$')).click()
            created = response.value.json()
            assert response.value.status==200 and created['verification_url'].startswith('/verify#')
            passed('后台人员行生成限时链接，底照继续由管理员维护')

            captured_requests=[]
            page.on('request',lambda request:captured_requests.append(request.url))
            page.goto(BASE+created['verification_url'],wait_until='networkidle')
            expect(page.locator('.pv-context-card')).to_contain_text(identity['name'])
            expect(page.get_by_role('button',name=re.compile('刷新入口状态$'))).to_be_enabled()
            assert not page.url.split('#')[1:] and participant.cookies()==[]
            assert '/api/auth/' not in '\n'.join(captured_requests)
            assert all('#' not in r and created['verification_url'].split('#')[1] not in r for r in captured_requests)
            expect(page.locator('.app-sider')).to_have_count(0)
            assert participant.request.get(BASE+'/api/faces/status').status==401
            passed('独立人员页无需管理员登录，令牌不进入请求URL，不能访问后台')

            def submit(photo):
                page.get_by_label('上传核验照片').set_input_files(photo)
                with page.expect_response(lambda r:r.url==BASE+'/api/participant-verification/submit') as response:
                    page.get_by_role('button',name=re.compile('提交一对一核验$')).click()
                expect(page.get_by_role('button',name=re.compile('刷新入口状态$'))).to_be_enabled()
                return response.value
            r=submit(str(FIXTURES/'grace_hopper.jpg'))
            assert r.status==200 and r.json()['status']=='unmatched'
            expect(page.locator('.pv-result-card')).to_contain_text('相似度未达到门槛')
            expect(page.locator('.pv-context-card')).to_contain_text('剩余 4 次提交')
            passed('不同人员未通过，保留重试机会并更新剩余次数')

            r=submit(image_payload(Image.new('RGB',(512,512),'white')))
            assert r.status==422
            expect(page.locator('.pv-result-card')).to_contain_text('图片不合格')
            expect(page.locator('.pv-score-row')).to_have_count(0)
            expect(page.locator('.pv-context-card')).to_contain_text('剩余 3 次提交')
            passed('无脸图片提示不合格，清除旧分数并计入尝试记录')

            with Image.open(FIXTURES/'astronaut.png') as original:
                variant=ImageEnhance.Brightness(original.rotate(3,resample=Image.Resampling.BICUBIC)).enhance(0.8)
                photo=image_payload(variant)
            page.get_by_label('上传核验照片').set_input_files(photo)
            for width in [1366,1600,1920]:
                page.set_viewport_size({'width':width,'height':1150})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
            page.screenshot(path=str(ROOT/'participant-verification-ready.png'),full_page=True)
            r=submit(photo)
            assert r.status==200 and r.json()['status']=='matched'
            report['same_variant_score']=r.json()['similarity']
            expect(page.locator('.pv-result-card')).to_contain_text('相似度达到门槛')
            expect(page.locator('.pv-context-card')).to_contain_text('核验已完成')
            expect(page.get_by_role('button',name=re.compile('提交一对一核验$'))).to_be_disabled()
            page.screenshot(path=str(ROOT/'participant-verification-completed.png'),full_page=True)
            page.reload(wait_until='networkidle')
            expect(page.locator('.pv-context-card')).to_contain_text('核验已完成')
            passed('同照片调暗旋转版本真实匹配通过，完成后不可重复提交；刷新仍可查看')

            admin_page.get_by_role('button',name=re.compile('刷新链接与结果$')).click()
            expect(admin_page.locator('.pv-admin-card').filter(has_text='人员核验结果')).to_contain_text('匹配通过')
            records=admin.request.get(url+'/verification-results').json()['items']
            assert len(records)==3 and {r['status'] for r in records}=={'matched','unmatched','invalid'}
            assert all('photo' not in r and 'embedding' not in r for r in records)
            # 清除当前创建链接的内存展示后截屏，报告不携带令牌。
            admin_page.get_by_role('button',name='Close',exact=True).click()
            expect(admin_page.locator('.pv-admin-session')).to_have_count(0)
            admin_page.get_by_role('row').filter(has_text=code).get_by_role('button',name='人员核验',exact=True).click()
            expect(admin_page.locator('.pv-admin-card').filter(has_text='人员核验结果')).to_contain_text('匹配通过')
            admin_page.wait_for_function("() => { const e=document.querySelector('.person-verification-admin-modal'); return e && getComputedStyle(e).opacity==='1' && !e.className.includes('zoom-enter') }")
            admin_page.screenshot(path=str(ROOT/'participant-verification-admin-results.png'),full_page=True,animations='disabled')
            passed('后台可查看通过、未通过、图片不合格三条持久化记录，不保存候选图')

            with admin_page.expect_response(lambda r:r.url==url+'/verification-links' and r.request.method=='POST') as response:
                admin_page.get_by_role('button',name=re.compile('生成核验链接$')).click()
            second=response.value.json()
            public_headers={'Authorization':'Bearer '+second['verification_url'].split('#')[1]}
            assert participant.request.get(BASE+'/api/participant-verification/context',headers=public_headers).status==200
            link_id=second['link_id']
            assert admin.request.delete(BASE+'/api/faces/verification-links/'+link_id,headers=headers).status==200
            page.goto(BASE+second['verification_url'],wait_until='networkidle')
            expect(page.get_by_text('核验链接已过期、撤销或人员已停用，请联系管理员',exact=True)).to_be_visible()
            expect(page.get_by_role('button',name=re.compile('提交一对一核验$'))).to_be_disabled()
            passed('后台撤销链接后人员页明确失效，不能提交')
            assert not report['page_errors'],report['page_errors']
            report['status']='PASS'
        except Exception:
            report['status']='FAIL'
            # 不截后台失败图，防止生成链接令牌被保存。
            page.screenshot(path=str(ROOT/'participant-verification-e2e-failure.png'),full_page=True)
            raise
        finally:
            (ROOT/'participant-verification-e2e-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            participant.close();admin.close();browser.close()


if __name__=='__main__':
    main()
