"""真实模型 + 正式页面端到端测试，只允许专用 14000 隔离环境。"""
import io
import json
import os
from pathlib import Path
import re

from PIL import Image, ImageEnhance
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT.parents[1] / 'services/api/tests/face_fixtures'
BASE = 'http://localhost:14000'


def payload(image, name='candidate.png'):
    out = io.BytesIO()
    image.save(out, 'PNG')
    return {'name': name, 'mimeType': 'image/png', 'buffer': out.getvalue()}


def main():
    if os.environ.get('FACE_E2E_ISOLATED') != '1':
        raise RuntimeError('仅允许显式确认的隔离测试环境；禁止正式账号初始化')
    report = {'status': 'RUNNING', 'checks': [], 'errors': [], 'scope': '临时schema，真实CPU模型；公开测试照片；不写正式资料'}
    def passed(name):
        report['checks'].append(name)
        print('PASS', name, flush=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1600, 'height': 1200})
        page = context.new_page()
        page.on('pageerror', lambda error: report['errors'].append(str(error)))
        headers = {'X-Requested-With': 'XMLHttpRequest', 'Origin': BASE}
        try:
            status = context.request.get(BASE + '/api/auth/status').json()
            credentials = {'username': 'test_admin', 'password': 'IntegrationTest12345'}
            if not status['initialized']:
                r = context.request.post(BASE + '/api/auth/setup', data={**credentials, 'display_name': '隔离匹配测试'}, headers=headers)
            else:
                r = context.request.post(BASE + '/api/auth/login', data=credentials, headers=headers)
            assert r.status in (200, 201), f'isolated login HTTP {r.status}'
            headers['X-CSRF-Token'] = r.json()['csrf_token']
            r = context.request.post(BASE + '/api/registry/persons', data={
                'code': 'FACE-E2E-' + str(os.getpid()), 'name': '人脸核验测试样例', 'unit': '仅测试公开照片', 'role': 'expert', 'enabled': True,
            }, headers=headers)
            assert r.status == 201, r.text()
            person_id = r.json()['person_id']
            url = BASE + '/api/faces/persons/' + person_id
            page.goto(BASE + '/registry', wait_until='load')
            page.get_by_role('tab', name=re.compile('人员')).click()
            row = page.get_by_role('row').filter(has_text='FACE-E2E-' + str(os.getpid()))
            row.get_by_role('button', name='人脸匹配', exact=True).click()
            expect(page.get_by_role('button', name=re.compile('刷新登记信息$'))).to_be_enabled()
            expect(page.get_by_role('button', name=re.compile('开始一对一核验$'))).to_be_disabled()
            passed('人员行入口打开弹窗；未登记不能核验')

            page.get_by_label('选择登记照片').set_input_files(str(FIXTURES / 'astronaut.png'))
            page.get_by_role('button', name='保存登记照片', exact=True).click()
            expect(page.get_by_text('登记照片已保存，旧核验结果已清除。', exact=True)).to_be_visible()
            expect(page.locator('.face-photo-stage img[alt$="的鉴权登记照片"]')).to_be_visible()
            assert page.locator('.face-photo-stage img[alt$="的鉴权登记照片"]').evaluate('(img)=>img.complete && img.naturalWidth === 112')
            metadata = context.request.get(url).json()
            assert metadata['registered'] and 'embedding' not in metadata
            passed('真实照片登记持久化；112像素鉴权缩略图可见；不泄露特征')

            image = Image.open(FIXTURES / 'astronaut.png').convert('RGB')
            variant = ImageEnhance.Brightness(image.rotate(3, resample=Image.Resampling.BICUBIC)).enhance(0.8)
            page.get_by_label('上传候选照片').set_input_files(payload(variant))
            with page.expect_response(lambda r: r.url == url + '/verify' and r.request.method == 'POST') as response:
                page.get_by_role('button', name=re.compile('开始一对一核验$')).click()
            result = response.value.json()
            assert result['matched'] and not result['liveness_checked'], result
            expect(page.locator('.face-result-values')).to_contain_text('相似度达到门槛')
            expect(page.locator('.face-result-values')).to_contain_text(f"{result['similarity']:.4f}")
            report['same_variant_score'] = result['similarity']
            page.screenshot(path=str(ROOT / 'face-match-real-model.png'), full_page=True)
            passed('同照片旋转调暗版本真实模型通过；页面显示后台分数')

            page.get_by_label('上传候选照片').set_input_files(str(FIXTURES / 'grace_hopper.jpg'))
            with page.expect_response(lambda r: r.url == url + '/verify') as response:
                page.get_by_role('button', name=re.compile('开始一对一核验$')).click()
            result = response.value.json()
            assert not result['matched'], result
            expect(page.locator('.face-result-values')).to_contain_text('相似度未达到门槛')
            report['different_score'] = result['similarity']
            passed('不同人员照片未匹配，不显示通过')

            page.get_by_label('上传候选照片').set_input_files(payload(Image.new('RGB', (512, 512), 'white')))
            with page.expect_response(lambda r: r.url == url + '/verify') as response:
                page.get_by_role('button', name=re.compile('开始一对一核验$')).click()
            assert response.value.status == 422
            expect(page.get_by_text('未检测到清晰人脸，请正对镜头并保证光线充足', exact=True)).to_be_visible()
            expect(page.locator('.face-result-values')).to_have_count(0)
            passed('无脸图片给出明确错误，不保留上一条核验结论')

            page.get_by_role('button', name='Close', exact=True).click()
            expect(page.locator('.face-match-session')).to_have_count(0)
            page.reload(wait_until='load')
            page.get_by_role('tab', name=re.compile('人员')).click()
            page.get_by_role('row').filter(has_text='FACE-E2E-' + str(os.getpid())).get_by_role('button', name='人脸匹配', exact=True).click()
            expect(page.locator('.face-photo-stage img[alt$="的鉴权登记照片"]')).to_be_visible()
            assert context.request.get(url).json()['version'] == metadata['version']
            passed('刷新重开后登记仍存在且版本一致')
            for width in [1366, 1600, 1920]:
                page.set_viewport_size({'width': width, 'height': 1200})
                assert page.locator('.face-match-modal').evaluate('(e)=>e.scrollWidth <= e.clientWidth + 1')
            passed('1366、1600、1920桌面弹窗无横向溢出')

            page.get_by_role('button', name=re.compile('删除登记$')).click()
            page.get_by_role('button', name='确认删除', exact=True).click()
            expect(page.get_by_text('登记照片与特征已删除，旧核验结果已清除。', exact=True)).to_be_visible()
            assert not context.request.get(url).json()['registered']
            assert context.request.get(url + '/photo').status == 404
            passed('页面确认删除后数据库和照片入口均清除')
            assert not report['errors'], report['errors']
            report['status'] = 'PASS'
        except Exception:
            report['status'] = 'FAIL'
            page.screenshot(path=str(ROOT / 'face-match-real-model-failure.png'), full_page=True)
            raise
        finally:
            (ROOT / 'face-match-e2e-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            context.close()
            browser.close()


if __name__ == '__main__':
    main()
