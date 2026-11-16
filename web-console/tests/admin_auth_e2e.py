"""单管理员真实 UI 验收，仅运行于独立数据库的本机隔离服务。

部署通知后：python tests/admin_auth_e2e.py --base-url http://localhost:13000
离线准备检查：python tests/admin_auth_e2e.py --self-check
可设置 ADMIN_TEST_USERNAME=test_admin 和 ADMIN_TEST_PASSWORD；未提供时使用 secrets。
测试最后通过 UI 将密码恢复为初始测试密码，便于同一隔离库后续回归。
不访问正式 3000，不保存密码/Cookie/CSRF，不启动 trace/HAR 或录像。
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import sys
from urllib.parse import parse_qs, urlparse
import uuid

from playwright.sync_api import expect, sync_playwright
sys.dont_write_bytecode = True
from auth_browser_support import authenticate, isolated_base


ROOT = Path(__file__).resolve().parent
COOKIE_NAME = "eval_admin_session"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def fresh_password():
    return "Aa9_" + secrets.token_urlsafe(24)


def sanitize(value, private):
    """Playwright 失败信息可能包含 fill 参数；写文件/终端前统一脱敏。"""
    if isinstance(value, str):
        for secret in sorted((item for item in private if item), key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key.lower() in
                ("password", "new_password", "current_password", "csrf_token", "cookie", "token")
                else sanitize(item, private) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item, private) for item in value]
    return value


class AuthAcceptance:
    def __init__(self, browser, base, report, username, password, new_password):
        self.browser, self.base, self.report = browser, base, report
        self.username, self.password, self.new_password = username, password, new_password
        self.current_password = password
        self.wrong_password = fresh_password()
        self.private = [password, new_password, self.wrong_password]
        self.contexts = []
        self.context = self.context_new()
        self.page = self.page_new(self.context)
        self.event_id = None
        self.display_name = "浏览器回归演示管理员"

    def context_new(self):
        context = self.browser.new_context(viewport={"width": 1600, "height": 1000},
                                           locale="zh-CN", accept_downloads=True)
        context.set_default_timeout(15000)
        self.contexts.append(context)
        return context

    def page_new(self, context):
        page = context.new_page()
        page.on("pageerror", lambda error: self.report["page_errors"].append(str(error)))
        page.on("response", lambda response: self.report.setdefault("failed_requests", []).append({
            "path": urlparse(response.url).path, "status": response.status,
            "case": self.report["cases"][-1]["id"] if self.report["cases"] else None})
            if response.status >= 400 and urlparse(response.url).path.startswith("/api/") else None)
        return page

    def status(self, context=None):
        response = (context or self.context).request.get(self.base + "/api/auth/status", max_redirects=0)
        require(response.status == 200, f"status HTTP {response.status}，部署未就绪")
        require("no-store" in response.headers.get("cache-control", ""), "认证状态必须禁止缓存")
        data = response.json()
        if data.get("csrf_token"):
            self.private.append(data["csrf_token"])
        return data

    def capture(self, name, page=None):
        page = page or self.page
        target = ROOT / f"admin-auth-{self.report['run_id']}-{name}.png"
        try:
            page.screenshot(path=str(target), full_page=True, mask=[page.locator('input[type="password"]')])
            self.report["screenshots"].append(str(target))
        except Exception:
            self.report.setdefault("screenshot_errors", []).append("截图失败，未输出页面敏感信息")

    @contextmanager
    def case(self, number, name):
        record = {"id": f"AUTH-{number:02d}", "name": f"浏览器回归演示：{name}", "status": "RUNNING"}
        self.report["cases"].append(record)
        try:
            yield record
            record["status"] = "PASS"
            print("PASS", record["id"], record["name"], flush=True)
        except Exception as error:
            record.update(status="FAIL", error=sanitize(str(error), self.private))
            self.capture(f"failure-{number:02d}")
            raise

    def navigate(self, path, page=None):
        (page or self.page).goto(self.base + path, wait_until="domcontentloaded")

    def logged_in(self, page=None):
        page = page or self.page
        expect(page.get_by_role("button", name="管理员账户菜单", exact=True)).to_be_visible()
        expect(page.locator(".auth-account-name")).to_have_text(self.display_name)

    def login_screen(self, page=None):
        page = page or self.page
        expect(page).to_have_url(re.compile(r"/login(?:\?|$)"))
        expect(page.get_by_role("heading", name="欢迎登录", exact=True)).to_be_visible()
        expect(page.get_by_label("管理员名称", exact=True)).to_have_count(0)

    def submit_auth(self, password, *, setup=False, page=None, expected=None):
        page = page or self.page
        expected = (201 if setup else 200) if expected is None else expected
        page.get_by_label("管理员账号", exact=True).fill(self.username)
        page.get_by_label("密码", exact=True).fill(password)
        if setup:
            page.get_by_label("管理员名称", exact=True).fill(self.display_name)
            page.get_by_label("确认密码", exact=True).fill(password)
        path = "/api/auth/setup" if setup else "/api/auth/login"
        with page.expect_response(lambda response: urlparse(response.url).path == path and
                                  response.request.method == "POST") as pending:
            page.get_by_role("button", name="创建管理员并登录" if setup else re.compile(r"登\s*录$"),
                             exact=setup).click()
        response = pending.value
        require(response.status == expected, f"{'setup' if setup else 'login'} HTTP {response.status}，预期 {expected}")
        self.report["http_checks"].append({"path": path, "status": response.status, "via": "UI"})
        if expected in (200, 201):
            data = response.json()
            self.private.append(data.get("csrf_token", ""))
            require(data["user"]["username"] == self.username, "登录用户名不符")
            self.logged_in(page)
        else:
            expect(page.locator(".auth-inline-alert.ant-alert-error")).to_be_visible()

    def menu(self, label, page=None):
        page = page or self.page
        page.get_by_role("button", name="管理员账户菜单", exact=True).click()
        page.get_by_role("menuitem", name=re.compile(re.escape(label) + r"$")).click()

    def logout(self, page=None):
        page = page or self.page
        with page.expect_response(lambda response: urlparse(response.url).path == "/api/auth/logout") as pending:
            self.menu("退出登录", page)
        require(pending.value.status == 200, "退出接口失败")
        self.login_screen(page)

    def password_change(self, old, new):
        self.menu("修改密码")
        dialog = self.page.get_by_role("dialog", name="修改管理员密码")
        dialog.get_by_label("当前密码", exact=True).fill(old)
        dialog.get_by_label("新密码", exact=True).fill(new)
        dialog.get_by_label("确认新密码", exact=True).fill(new)
        with self.page.expect_response(lambda response: urlparse(response.url).path == "/api/auth/password") as pending:
            dialog.get_by_role("button", name="保存新密码", exact=True).click()
        require(pending.value.status == 200, "UI 修改密码失败")
        self.current_password = new
        self.login_screen()

    def restore_password_api(self):
        """只对本脚本测试账号操作；无论后续断言结果，保留既有 e2e 原始凭证。"""
        context = self.context_new()
        headers = {"X-Requested-With": "XMLHttpRequest", "Origin": self.base}
        login = context.request.post(self.base + "/api/auth/login", headers=headers,
            data={"username": self.username, "password": self.current_password})
        require(login.status == 200, "恢复原始测试密码时认证失败（凭证不输出）")
        csrf = login.json()["csrf_token"]
        self.private.append(csrf)
        headers["X-CSRF-Token"] = csrf
        changed = context.request.post(self.base + "/api/auth/password", headers=headers,
            data={"current_password": self.current_password, "new_password": self.password})
        require(changed.status == 200, "认证 API 恢复原始测试密码失败")
        self.current_password = self.password
        self.report["password_restored_to_initial"] = True
        # 改密会撤销全部会话。再次认证并退出，以验证原始密码和清 Cookie。
        login = context.request.post(self.base + "/api/auth/login", headers={
            "X-Requested-With": "XMLHttpRequest", "Origin": self.base},
            data={"username": self.username, "password": self.password})
        require(login.status == 200, "恢复后原始密码验证失败")
        csrf = login.json()["csrf_token"]
        self.private.append(csrf)
        logout = context.request.post(self.base + "/api/auth/logout", headers={
            "X-Requested-With": "XMLHttpRequest", "Origin": self.base, "X-CSRF-Token": csrf}, data={})
        require(logout.status == 200, "恢复密码后退出失败")
        context.close()

    def unauthorized(self, context=None):
        context = context or self.context
        for path in ("/api/registry", "/api/events/query", "/api/auth/audit"):
            response = context.request.get(self.base + path, max_redirects=0)
            require(response.status == 401, f"未登录或已撤销会话的 {path} 应返回 401，实际 {response.status}")
            self.report["http_checks"].append({"path": path, "status": 401, "via": "direct_API"})

    def run(self):
        with self.case(1, "隔离服务未初始化，未登录业务 API 401"):
            initial = self.status()
            if self.report.get("resume_setup_result"):
                require(initial["initialized"] is True, "首次 setup 成功记录对应的管理员应存在")
            else:
                require(initial["initialized"] is False, "需要全新隔离 schema；脚本不会重置已有管理员")
            self.unauthorized()
            self.navigate("/registry")
            expect(self.page.get_by_role("heading", name="欢迎登录" if initial["initialized"] else "创建管理员账号", exact=True)).to_be_visible()
            require(urlparse(self.page.url).port == urlparse(self.base).port, "不得跳往正式服务")
            self.capture("first-setup")

        with self.case(2, "首次 UI setup 创建唯一测试管理员并登录"):
            self.submit_auth(self.password, setup=not self.report.get("resume_setup_result"))
            require(urlparse(self.page.url).path == "/registry", "首次 setup 应回到原受保护路径")
            status = self.status()
            require(status["initialized"] and status["user"]["role"] == "admin", "初始化或身份不符")
            if self.report.get("resume_setup_result"):
                require(status["user"]["display_name"] == self.display_name, "续跑管理员展示名不符")
                audit_response = self.context.request.get(self.base + "/api/auth/audit")
                require(audit_response.status == 200 and any(row["action"] == "auth_setup" and row["actor"] == self.username for row in audit_response.json()), "需有上轮 UI setup 成功的后台审计证据")
            cookies = [cookie for cookie in self.context.cookies() if cookie["name"] == COOKIE_NAME]
            require(len(cookies) == 1 and cookies[0]["httpOnly"] and cookies[0]["sameSite"] == "Strict", "会话 Cookie 属性不符")
            self.private.append(cookies[0]["value"])
            self.capture("setup-logged-in")

        with self.case(3, "第二次 setup 直接 API 返回 409，不允许第二管理员"):
            fresh = self.context_new()
            response = fresh.request.post(self.base + "/api/auth/setup", headers={
                "X-Requested-With": "XMLHttpRequest", "Origin": self.base}, data={
                "username": "test_admin_second", "display_name": "浏览器回归演示第二账号", "password": self.password})
            require(response.status == 409, f"第二管理员 setup 应 409，实际 {response.status}")
            self.report["http_checks"].append({"path": "/api/auth/setup", "status": 409, "via": "direct_API"})
            require(self.status(fresh)["user"] is None, "失败 setup 不应创建会话")

        with self.case(4, "刷新保留会话，Cookie 凭证不进入 Web Storage"):
            self.page.reload(wait_until="domcontentloaded")
            self.logged_in()
            require(self.status()["user"]["username"] == self.username, "刷新后身份丢失")
            storage = self.page.evaluate("({local:Object.entries(localStorage),session:Object.entries(sessionStorage),cookie:document.cookie})")
            require(COOKIE_NAME not in storage["cookie"], "HttpOnly Cookie 不应可被 JS 读取")
            serialized = json.dumps(storage)
            require(not any(secret in serialized for secret in self.private if secret), "密码或会话凭证不得保存到可读存储")

        with self.case(5, "退出后未登录直连 API 401，无公开注册入口"):
            self.logout()
            self.unauthorized()
            require(self.status()["user"] is None, "退出后 status 不应有用户")
            self.capture("logged-out")

        with self.case(6, "错误密码拒绝，深链登录保留查询参数并回跳"):
            deep_path = "/history?keyword=" + self.report["run_id"]
            self.navigate(deep_path)
            self.login_screen()
            require(parse_qs(urlparse(self.page.url).query).get("returnTo") == [deep_path], "深链 returnTo 不符")
            self.submit_auth(self.wrong_password, expected=401)
            require(self.status()["user"] is None, "错密码不应得到会话")
            self.capture("wrong-password")
            self.submit_auth(self.password)
            expect(self.page).to_have_url(self.base + deep_path)
            expect(self.page.locator("#wf-keyword-false")).to_have_value(self.report["run_id"])

        with self.case(7, "登录业务 UI 生成模拟事件，不接入真实视频"):
            self.navigate("/overview")
            self.logged_in()
            expect(self.page.get_by_text("业务演示 · 视频未接入", exact=True).first).to_be_visible()
            self.page.get_by_role("button", name=re.compile(r"生成模拟事件$")).click()
            dialog = self.page.get_by_role("dialog", name="生成模拟事件", exact=True)
            dialog.locator("textarea#note").fill("浏览器回归演示-管理员认证-" + self.report["run_id"])
            with self.page.expect_response(lambda response: urlparse(response.url).path == "/api/demo/events" and response.request.method == "POST") as created:
                dialog.get_by_role("button", name="生成并入库", exact=True).click()
            require(created.value.status in (200, 201), "登录后模拟事件 UI 创建失败")
            event = created.value.json()
            require(event["is_demo"] and event["confidence"] is None, "不得产生真实检测结果")
            self.event_id = event["event_id"]
            self.report["event_id"] = self.event_id

        with self.case(8, "UI 处置请求注入伪造 reviewer，后端强制登录 username"):
            path = f"/api/events/{self.event_id}/decisions"
            def forged_reviewer(route):
                body = route.request.post_data_json
                body["reviewer_id"] = "forged_browser_reviewer"
                route.continue_(post_data=json.dumps(body))
            self.page.route("**" + path, forged_reviewer)
            try:
                self.navigate("/manual?event_id=" + self.event_id)
                expect(self.page.locator(".wf-summary .wf-id")).to_have_text(self.event_id)
                self.page.locator("#wf-decision-note").fill("浏览器回归演示：核对登录身份，不代表真实违规")
                with self.page.expect_response(lambda response: urlparse(response.url).path == path and response.request.method == "POST") as decided:
                    self.page.get_by_role("button", name="确认属实", exact=True).click()
                require(decided.value.status == 200, "认证处置请求失败")
            finally:
                self.page.unroute("**" + path, forged_reviewer)
            detail_response = self.context.request.get(self.base + "/api/events/" + self.event_id)
            require(detail_response.status == 200, "处置后详情读取失败")
            detail = detail_response.json()
            require(detail["status"] == "CONFIRMED" and detail["decisions"][-1]["reviewer_id"] == self.username,
                    "后端未将伪造 reviewer 替换为登录 username")
            require(all(entry["actor"] == self.username for entry in detail["audit"]), "事件操作 actor 不符")
            require(detail["evidences"] == [], "没有真实视频，媒体证据应为空")
            self.report["reviewer_override"] = {"sent": "forged_browser_reviewer", "stored": self.username}
            self.capture("authenticated-decision")

        with self.case(9, "登录后 UI 导出真实事实清单和管理员审计"):
            require(self.status()["user"] is not None, "进入证据页前会话已失效")
            self.navigate("/evidence?event_id=" + self.event_id)
            expect(self.page.locator(".wf-summary .wf-id")).to_have_text(self.event_id)
            with self.page.expect_download() as pending:
                self.page.get_by_role("button", name=re.compile(r"下载 JSON 清单$")).click()
            download = pending.value
            raw = Path(download.path()).read_text(encoding="utf-8")
            require(not any(secret in raw for secret in self.private if secret), "导出不得包含认证秘密")
            exported = json.loads(raw)
            require(exported["event"]["event_id"] == self.event_id and exported["event"]["evidences"] == [], "导出内容不符")
            require(exported["event"]["decisions"][-1]["reviewer_id"] == self.username, "导出处置身份不符")
            audit_response = self.context.request.get(self.base + "/api/auth/audit")
            require(audit_response.status == 200, "管理员审计读取失败")
            audit_raw = audit_response.text()
            require(not any(secret in audit_raw for secret in self.private if secret), "认证审计不得记录密码/会话秘密")
            audit = audit_response.json()
            require(isinstance(audit, list), "审计列表结构不符")
            require(any(entry["actor"] == self.username and entry["path"].endswith("/export") for entry in audit), "导出须按管理员身份审计")
            self.report["export_verified"] = True
            self.capture("authenticated-export")

        with self.case(10, "修改密码撤销当前与独立旧会话，其他标签自动退出"):
            old_context = self.context_new()
            old_page = self.page_new(old_context)
            self.navigate("/registry", old_page)
            self.login_screen(old_page)
            self.submit_auth(self.password, page=old_page)
            secondary = self.page_new(self.context)
            self.navigate("/registry", secondary)
            self.logged_in(secondary)
            self.password_change(self.password, self.new_password)
            self.login_screen(secondary)
            self.unauthorized()
            self.unauthorized(old_context)
            require(self.status(old_context)["user"] is None, "独立旧会话未失效")
            old_page.close()
            secondary.close()
            self.capture("password-changed")

        with self.case(11, "旧密码不能登录，新密码 UI 登录成功"):
            self.submit_auth(self.password, expected=401)
            self.submit_auth(self.new_password)
            require(self.status()["user"]["username"] == self.username, "新密码身份不符")

        with self.case(12, "恢复初始测试密码后再次登录，保留后续回归凭证"):
            self.restore_password_api()
            self.page.reload(wait_until="domcontentloaded")
            self.login_screen()
            self.submit_auth(self.password)

        with self.case(13, "跨标签退出立即清空业务页面与会话"):
            other = self.page_new(self.context)
            self.navigate("/registry", other)
            self.logged_in(other)
            self.logout()
            self.login_screen(other)
            expect(other.locator(".registry-page")).to_have_count(0)
            expect(other.locator(".app-header")).to_have_count(0)
            self.unauthorized()
            self.capture("cross-tab-logout", other)

        with self.case(14, "登录页拒绝外域 returnTo，无 JavaScript 异常"):
            self.navigate("/login?returnTo=https%3A%2F%2Fexample.invalid%2Fexternal")
            self.submit_auth(self.password)
            expect(self.page).to_have_url(self.base + "/overview")
            self.logout()
            require(not self.report["page_errors"], "存在浏览器 JS 异常，见脱敏结果")
            self.report["completed_logged_out"] = True


def self_check():
    """纯离线 red 检查：正式地址、错误回归配置、未初始化必须被拒绝。"""
    blocked = []
    for url in ("http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:8000", "http://example.com:13000", "http://localhost:13000/path"):
        try:
            isolated_base(url)
        except ValueError:
            blocked.append(url)
        else:
            raise AssertionError("危险环境未被拒绝")
    require(isolated_base("http://localhost:13000/") == "http://localhost:13000", "隔离地址校验不符")
    class StubResponse:
        status = 200
        def json(self): return {"initialized": False}
    class NoSetupRequest:
        def get(self, *args, **kwargs): return StubResponse()
        def post(self, *args, **kwargs): raise AssertionError("辅助模块不应调用 setup 或提前 login")
    class StubContext:
        request = NoSetupRequest()
    keys = ("TEST_BASE_URL", "ADMIN_TEST_PASSWORD")
    old = {key: os.environ.get(key) for key in keys}
    try:
        os.environ.pop("TEST_BASE_URL", None)
        require(authenticate(StubContext(), "http://localhost:3000") is None, "无测试环境不得登录")
        os.environ["TEST_BASE_URL"] = "http://localhost:13000"
        os.environ["ADMIN_TEST_PASSWORD"] = fresh_password()
        try:
            authenticate(StubContext(), "http://localhost:13000")
        except RuntimeError:
            pass
        else:
            raise AssertionError("未初始化环境未被拒绝")
    finally:
        for key, value in old.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value
    secret = fresh_password()
    require(secret not in json.dumps(sanitize({"error": "fill:" + secret, "csrf_token": "token"}, [secret])), "脱敏失效")
    report = {"status": "PREPARED", "live_execution": False, "network_access": False,
        "blocked_bases": blocked, "default_base": "http://localhost:13000",
        "no_automatic_setup_helper": "PASS", "redaction_check": "PASS",
        "awaiting": "收到隔离服务部署完成通知后运行"}
    (ROOT / "admin-auth-preparation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("TEST_BASE_URL", "http://localhost:13000"))
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--resume-setup-result", type=Path, help="仅续跑本脚本已通过 UI setup201 的失败结果，不重复初始化")
    args = parser.parse_args()
    if args.self_check:
        return self_check()
    base = isolated_base(args.base_url)
    username = os.environ.get("ADMIN_TEST_USERNAME", "test_admin")
    require(re.fullmatch(r"[A-Za-z0-9_]{3,32}", username), "测试用户名不符合契约")
    password = os.environ.get("ADMIN_TEST_PASSWORD") or fresh_password()
    new_password = fresh_password()
    require(10 <= len(password) <= 128 and re.search(r"[A-Za-z]", password) and re.search(r"[0-9]", password), "测试密码不符合契约（不输出内容）")
    run_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
    report = {"run_id": run_id, "base_url": base, "status": "RUNNING", "username": username,
        "password_source": "environment" if os.environ.get("ADMIN_TEST_PASSWORD") else "secrets_generated_not_persisted",
        "started_at": datetime.now(timezone.utc).isoformat(), "cases": [], "http_checks": [],
        "page_errors": [], "screenshots": [], "scope": "仅隔离 schema 认证和模拟业务，不访问正式库，不接入真实视频"}
    if args.resume_setup_result:
        previous = json.loads(args.resume_setup_result.read_text(encoding="utf-8"))
        require(previous.get("base_url") == base and previous.get("username") == username and
                previous.get("error") == "setup HTTP 201，预期 200" and previous["cases"][0]["status"] == "PASS",
                "仅允许从本测试的 setup 已成功201、断言错误结果续跑")
        report["resume_setup_result"] = str(args.resume_setup_result)
        report["first_setup_evidence"] = {"initial_uninitialized_case": "PASS", "ui_setup_response": 201,
            "screenshots": previous["screenshots"], "note": "首次设置成功；原运行仅错误地断言 HTTP 200"}
    result = ROOT / f"admin-auth-{run_id}-result.json"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        acceptance = AuthAcceptance(browser, base, report, username, password, new_password)
        try:
            acceptance.run()
            report["status"] = "PASS"
        except Exception as error:
            report.update(status="FAIL", error=sanitize(str(error), acceptance.private))
            acceptance.capture("failure-final")
            print("FAIL", sanitize(str(error), acceptance.private), flush=True)
        finally:
            if acceptance.current_password != password:
                try:
                    acceptance.restore_password_api()
                    report["cleanup_password_restored"] = True
                except Exception:
                    report["cleanup_error"] = "隔离管理员原始密码恢复失败，未输出凭证；需主线程检查测试库"
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            result.write_text(json.dumps(sanitize(report, acceptance.private), ensure_ascii=False, indent=2), encoding="utf-8")
            for context in acceptance.contexts:
                context.close()
            browser.close()
            print("RESULT", result, flush=True)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
