"""隔离测试登录辅助；导入无副作用，不初始化账号、不保存凭证。

旧测试可调用 authenticate(context, base)，随后 context.request 的写操作
自动携带 X-Requested-With / X-CSRF-Token。只有显式 TEST_BASE_URL 才登录。
"""
from __future__ import annotations

import os
from urllib.parse import urlparse


def isolated_base(base: str) -> str:
    value = base.rstrip("/")
    parsed = urlparse(value)
    if (parsed.scheme not in ("http", "https") or parsed.hostname not in
            ("localhost", "127.0.0.1", "::1") or parsed.port in (None, 3000, 8000)
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("仅允许显式端口的本机隔离环境；禁止正式 3000 / 8000 环境")
    return value


def authenticate(context, base: str):
    """未配置 TEST_BASE_URL 时返回 None；初始化未完成时拒绝，不调用 setup。

    返回不含会话/CSRF 的 user 字典。调用方原有 extra_headers 可通过
    context.new_context 配置后，在此函数调用之后重新设置并保留认证头。
    """
    configured = os.environ.get("TEST_BASE_URL")
    if not configured:
        return None
    target = isolated_base(configured)
    if isolated_base(base) != target:
        raise ValueError("base 与 TEST_BASE_URL 不一致，拒绝登录")
    username = os.environ.get("ADMIN_TEST_USERNAME", "test_admin")
    password = os.environ.get("ADMIN_TEST_PASSWORD")
    if not password:
        raise ValueError("隔离浏览器登录需要 ADMIN_TEST_PASSWORD；不会自动创建管理员")
    response = context.request.get(target + "/api/auth/status", max_redirects=0)
    if response.status != 200:
        raise RuntimeError(f"认证状态检查失败 HTTP {response.status}")
    status = response.json()
    if not status.get("initialized"):
        raise RuntimeError("隔离管理员尚未初始化；辅助模块不会调用 setup")
    headers = {"X-Requested-With": "XMLHttpRequest", "Origin": target}
    response = context.request.post(target + "/api/auth/login",
        data={"username": username, "password": password}, headers=headers, max_redirects=0)
    if response.status != 200:
        raise RuntimeError(f"隔离管理员登录失败 HTTP {response.status}（凭证不输出）")
    session = response.json()
    user = session.get("user")
    csrf = session.get("csrf_token")
    if not user or user.get("username") != username or user.get("role") != "admin" or not csrf:
        raise RuntimeError("登录返回身份或 CSRF 不符合契约")
    # BrowserContext extraHTTPHeaders 同时用于 context.request；浏览器同源 Cookie 已共享。
    context.set_extra_http_headers({**headers, "X-CSRF-Token": csrf})
    return user
