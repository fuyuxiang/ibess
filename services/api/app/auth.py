"""管理员与岗位账号认证、会话和角色权限；由主应用显式集成。"""
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

from app import db
from app.auth_models import AdminAccount, AdminAudit, AdminSession
from app.auth_models import TeamAccount, TeamSession

log = logging.getLogger(__name__)


class AuthRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def sanitized_handler(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # 仅输出字段位置、类型和描述；input/ctx 可能携带密码或整份正文。
                errors = [{key: item[key] for key in ("type", "loc", "msg") if key in item} for item in exc.errors()]
                return JSONResponse({"detail": errors}, status_code=422, headers={"Cache-Control": "no-store"})

        return sanitized_handler


router = APIRouter(route_class=AuthRoute)
COOKIE_NAME = "eval_admin_session"
SESSION_SECONDS = 8 * 60 * 60
_AUTH_LOCK = 81234172
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_PUBLIC = {("GET", "/api/auth/status"), ("POST", "/api/auth/setup"), ("POST", "/api/auth/login")}
_PUBLIC.update({('GET', '/api/participant-verification/context'), ('POST', '/api/participant-verification/submit')})
_LOGIN_WINDOW = 300
_LOGIN_LIMIT = 5
_throttle_lock = threading.Lock()
_failures = {}


def _now():
    return datetime.now(timezone.utc)


def _lock_account(session):
    # setup、login、logout、password 共用锁，防止改密后旧登录事务再插入会话。
    session.execute(text(f"SELECT pg_advisory_xact_lock({_AUTH_LOCK})"))


def _user(account):
    return {"username": account.username, "display_name": account.display_name, "role": getattr(account,'role','admin')}


def _digest(raw):
    if not isinstance(raw, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", raw):
        return None
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def _identity(raw, session_factory=None):
    digest = _digest(raw)
    if digest is None:
        return None
    with (session_factory or db.SessionLocal)() as session:
        record = session.get(AdminSession, digest)
        team = record is None
        if team:
            record = session.get(TeamSession,digest)
        if record is None or record.expires_at <= _now():
            return None
        account = session.get(TeamAccount if team else AdminAccount, record.account_id)
        if account is None or not getattr(account,'active',True):
            return None
        return {"user": _user(account), "csrf_token": record.csrf_token, "token_hash": digest}


def session_is_valid(rawtoken, session_factory=None):
    """SSE 每轮调用；只校验摘要、过期时间和账号存在性，不续期。"""
    return _identity(rawtoken, session_factory) is not None


def _session_factory(request):
    return request.scope.get("state", {}).get("_auth_session_factory") or db.SessionLocal


def _hash_password(password):
    salt = secrets.token_bytes(32)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=64, maxmem=64 * 1024 * 1024)
    return f"scrypt$16384$8$1${salt.hex()}${derived.hex()}"


def _verify_password(password, encoded):
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if (algorithm, n, r, p) != ("scrypt", "16384", "8", "1"):
            return False
        expected_bytes = bytes.fromhex(expected)
        salt_bytes = bytes.fromhex(salt)
        if len(expected_bytes) != 64 or len(salt_bytes) != 32:
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt_bytes, n=16384, r=8, p=1, dklen=64, maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(actual, expected_bytes)
    except (ValueError, TypeError):
        return False


def _password(value):
    if not any(char.isalpha() for char in value) or not any(char.isdigit() for char in value):
        raise ValueError("密码须为10–128字符，且同时包含字母和数字")
    return value


class SetupIn(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_]+$")
    display_name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=10, max_length=128)

    @field_validator("display_name")
    @classmethod
    def display_nonempty(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("展示名不能为空")
        return value

    @field_validator("password")
    @classmethod
    def password_valid(cls, value):
        return _password(value)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class PasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=10, max_length=128)

    @field_validator("new_password")
    @classmethod
    def password_valid(cls, value):
        return _password(value)


def _cookie_secure():
    return os.environ.get("AUTH_COOKIE_SECURE", "false").strip().lower() in {"1", "true", "yes", "on"}


def _set_cookie(response, raw):
    response.set_cookie(COOKIE_NAME, raw, max_age=SESSION_SECONDS, httponly=True, secure=_cookie_secure(), samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"


def _clear_cookie(response):
    response.delete_cookie(COOKIE_NAME, httponly=True, secure=_cookie_secure(), samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"


def _new_session(session, account):
    raw = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    # 已到期会话不再保留；有效会话可用于多个浏览器。
    model=TeamSession if isinstance(account,TeamAccount) else AdminSession
    session.query(model).filter(model.expires_at <= _now()).delete(synchronize_session=False)
    session.add(model(token_hash=_digest(raw), account_id=account.account_id, expires_at=_now() + timedelta(seconds=SESSION_SECONDS), csrf_token=csrf))
    return raw, csrf


def _audit(session, username, action, path, method):
    session.add(AdminAudit(actor=username, action=action, path=path[:2048], method=method[:16]))


def _throttle_keys(username, request):
    ip = request.client.host if request.client else "unknown"
    return (("username", username), ("ip", ip))


def _check_throttle(keys):
    now = time.monotonic()
    with _throttle_lock:
        for key in list(_failures):
            entries = _failures[key]
            while entries and entries[0] <= now - _LOGIN_WINDOW:
                entries.popleft()
            if not entries:
                del _failures[key]
        if any(len(_failures.get(key, ())) >= _LOGIN_LIMIT for key in keys):
            raise HTTPException(429, "登录尝试过于频繁，请稍后再试", headers={"Retry-After": str(_LOGIN_WINDOW)})


def _login_failed(keys):
    with _throttle_lock:
        for key in keys:
            _failures.setdefault(key, deque()).append(time.monotonic())


def _active_session(session, request):
    digest = _digest(request.cookies.get(COOKIE_NAME))
    record = session.get(AdminSession, digest) if digest else None
    if record is None and digest:
        record=session.get(TeamSession,digest)
    if record is None or record.expires_at <= _now():
        raise HTTPException(401, "登录已失效，请重新登录")
    return record


@router.get("/auth/status")
def status(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    with _session_factory(request)() as session:
        initialized = session.get(AdminAccount, 1) is not None
    identity = _identity(request.cookies.get(COOKIE_NAME), _session_factory(request))
    return {"initialized": initialized, "user": identity["user"] if identity else None, "csrf_token": identity["csrf_token"] if identity else None}


@router.post("/auth/setup", status_code=201)
def setup(body: SetupIn, request: Request, response: Response):
    with _session_factory(request)() as session:
        _lock_account(session)
        if session.get(AdminAccount, 1) is not None:
            raise HTTPException(409, "管理员已初始化，请登录")
        account = AdminAccount(account_id=1, username=body.username, display_name=body.display_name, password_hash=_hash_password(body.password))
        session.add(account)
        session.flush()
        raw, csrf = _new_session(session, account)
        user = _user(account)
        _audit(session, account.username, "auth_setup", "/api/auth/setup", "POST")
        session.commit()
    _set_cookie(response, raw)
    return {"user": user, "csrf_token": csrf}


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response):
    keys = _throttle_keys(body.username, request)
    with _session_factory(request)() as session:
        _lock_account(session)
        _check_throttle(keys)
        account = session.get(AdminAccount, 1)
        if account is None:
            raise HTTPException(409, "请先初始化管理员")
        if not hmac.compare_digest(body.username.encode('utf-8'),account.username.encode('utf-8')):
            account=session.query(TeamAccount).filter_by(username=body.username).first()
        password_matches = _verify_password(body.password, account.password_hash if account else session.get(AdminAccount,1).password_hash)
        if account is None or not getattr(account,'active',True) or not password_matches:
            _login_failed(keys)
            raise HTTPException(401, "用户名或密码错误")
        raw, csrf = _new_session(session, account)
        user = _user(account)
        _audit(session, account.username, "auth_login", "/api/auth/login", "POST")
        session.commit()
    _set_cookie(response, raw)
    return {"user": user, "csrf_token": csrf}


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    with _session_factory(request)() as session:
        _lock_account(session)
        record = _active_session(session, request)
        account = session.get(TeamAccount if isinstance(record,TeamSession) else AdminAccount, record.account_id)
        _audit(session, account.username, "auth_logout", "/api/auth/logout", "POST")
        session.delete(record)
        session.commit()
    _clear_cookie(response)
    return {"ok": True}


@router.post("/auth/password")
def change_password(body: PasswordIn, request: Request, response: Response):
    with _session_factory(request)() as session:
        _lock_account(session)
        record = _active_session(session, request)
        account = session.get(TeamAccount if isinstance(record,TeamSession) else AdminAccount, record.account_id)
        if not _verify_password(body.current_password, account.password_hash):
            raise HTTPException(400, "当前密码错误")
        account.password_hash = _hash_password(body.new_password)
        model=TeamSession if isinstance(record,TeamSession) else AdminSession
        session.query(model).filter(model.account_id == account.account_id).delete(synchronize_session=False)
        _audit(session, account.username, "auth_password_change", "/api/auth/password", "POST")
        session.commit()
    _clear_cookie(response)
    return {"ok": True}


@router.get("/auth/audit")
def audit(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    # 中间件已认证；此处再核验，避免身份撤销后继续读取敏感留痕。
    with _session_factory(request)() as session:
        _active_session(session, request)
        records = session.query(AdminAudit).order_by(AdminAudit.id.desc()).limit(100).all()
        return [{"actor": row.actor, "action": row.action, "path": row.path, "method": row.method, "at": row.at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")} for row in records]


def _origin(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment:
            return None
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


def _write_headers_allowed(connection):
    if connection.headers.get("x-requested-with") != "XMLHttpRequest":
        return False
    supplied = connection.headers.get("origin")
    if supplied is None:
        return True
    expected = _origin(f"{connection.scope['scheme']}://{connection.headers.get('host', '')}")
    return expected is not None and _origin(supplied) == expected


def _save_business_audit(username, path, method, session_factory=None):
    with (session_factory or db.SessionLocal)() as session:
        _audit(session, username, "business_export" if method in {"GET", "HEAD"} else "business_write", path, method)
        session.commit()


def role_path_allowed(role,path,method):
    if role=='admin':return True
    if path in ('/api/auth/logout','/api/auth/password','/api/auth/status'):return True
    if method in _SAFE_METHODS:
        export=path.endswith(('/export','/evidence-package','/download'))
        if export and role!='supervisor':return False
        return path.startswith(('/api/supervision/','/api/events','/api/evidence/','/api/recordings/',
            '/api/audio/recordings','/api/registry','/api/media-bindings/','/api/authz/snapshots/')) or bool(re.fullmatch(r'/api/sessions/[^/]+/phase-config',path))
    if role=='viewer':return False
    if re.fullmatch(r'/api/supervision/workflows/[^/]+/(checks|start|phase|finish|archive|journal|permit)',path):return role=='supervisor'
    if path=='/api/media-bindings' and method=='POST':return True
    if re.fullmatch(r'/api/audio/semantic-analyses/[^/]+/(retry|intake-retry)',path) and method=='POST':return True
    if path.startswith('/api/audio/recordings'):
        return path=='/api/audio/recordings' or path.endswith(('/semantic-analyses','/retry','/capture-time','/intake-retry'))
    if re.fullmatch(r'/api/supervision/events/(manual|[^/]+/(actions|receive|video-window|audio-links|preservation-retry|annotations|remove-link))',path):return True
    return role=='supervisor' and bool(re.fullmatch(r'/api/supervision/sessions/[^/]+/reports',path))


class TeamIn(SetupIn):
    role: str = Field(pattern=r'^(supervisor|operator|viewer)$')


class TeamPatch(BaseModel):
    active: bool | None = None
    role: str | None = Field(default=None,pattern=r'^(supervisor|operator|viewer)$')


@router.get('/auth/team')
def list_team(request:Request):
    with _session_factory(request)() as session:
        return [{**_user(a),'account_id':a.account_id,'active':a.active,'created_at':a.created_at.isoformat()} for a in session.query(TeamAccount).order_by(TeamAccount.created_at)]


@router.post('/auth/team',status_code=201)
def create_team(body:TeamIn,request:Request):
    from uuid import uuid4
    with _session_factory(request)() as session:
        _lock_account(session)
        if session.query(TeamAccount).filter_by(username=body.username).first() or session.query(AdminAccount).filter_by(username=body.username).first():
            raise HTTPException(409,'账号名称已使用')
        row=TeamAccount(account_id=str(uuid4()),username=body.username,display_name=body.display_name,role=body.role,
            password_hash=_hash_password(body.password),created_by=request.state.admin['username'])
        session.add(row);session.flush()
        _audit(session,request.state.admin['username'],'team_create','/api/auth/team','POST')
        result={**_user(row),'account_id':row.account_id,'active':True}
        session.commit()
        return result


@router.patch('/auth/team/{account_id}')
def update_team(account_id:str,body:TeamPatch,request:Request):
    with _session_factory(request)() as session:
        _lock_account(session)
        row=session.get(TeamAccount,account_id)
        if not row:raise HTTPException(404,'账号不存在')
        if body.role is not None:row.role=body.role
        if body.active is not None:row.active=body.active
        session.query(TeamSession).filter_by(account_id=account_id).delete(synchronize_session=False)
        _audit(session,request.state.admin['username'],'team_update','/api/auth/team/'+account_id,'PATCH')
        result={**_user(row),'account_id':row.account_id,'active':row.active}
        session.commit()
        return result


class AdminAuthMiddleware:
    """不缓存请求/响应正文，保留 StreamingResponse 的逐条 ASGI 消息。"""
    def __init__(self, app, session_factory=None):
        self.app = app
        self.session_factory = session_factory

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        protected = path == "/api" or path.startswith("/api/") or path in {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
        if not protected:
            await self.app(scope, receive, send)
            return
        connection = HTTPConnection(scope)
        factory = self.session_factory or db.SessionLocal
        scope.setdefault("state", {})["_auth_session_factory"] = factory
        method = scope.get("method", "GET").upper()
        public = scope["type"] == "http" and (method, path) in _PUBLIC

        async def reject(code, detail):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4401 if code == 401 else 4403})
            else:
                await JSONResponse({"detail": detail}, status_code=code, headers={"Cache-Control": "no-store"})(scope, receive, send)

        identity = None
        if not public:
            identity = await run_in_threadpool(_identity, connection.cookies.get(COOKIE_NAME), factory)
            if identity is None:
                await reject(401, "请先登录管理员账号")
                return
            scope.setdefault("state", {})["admin"] = identity["user"]
            if not role_path_allowed(identity['user']['role'],path,method):
                await reject(403,'当前岗位无权执行此操作')
                return
        if method not in _SAFE_METHODS:
            if not _write_headers_allowed(connection):
                await reject(403, "请求来源或 X-Requested-With 校验失败")
                return
            if not public and not hmac.compare_digest(connection.headers.get("x-csrf-token", "").encode("utf-8"), identity["csrf_token"].encode("utf-8")):
                await reject(403, "CSRF 校验失败，请刷新登录状态")
                return
        elif scope["type"] == "websocket" and connection.headers.get("origin") is not None:
            expected = _origin(f"{'https' if scope['scheme'] == 'wss' else 'http'}://{connection.headers.get('host', '')}")
            if _origin(connection.headers["origin"]) != expected:
                await reject(403, "请求来源校验失败")
                return

        audit_business = identity is not None and not path.startswith("/api/auth/") and (method not in _SAFE_METHODS or (method == "GET" and path.rstrip("/").endswith("/export")))

        async def authenticated_send(message):
            if audit_business and message["type"] == "http.response.start" and 200 <= message["status"] < 300:
                try:
                    await run_in_threadpool(_save_business_audit, identity["user"]["username"], path, method, factory)
                except Exception as exc:
                    # 核心业务留痕由其自身事务保证；此通用审计为独立短事务。
                    # 已提交业务不能因补充留痕失败改报500，引发不必要的重试。
                    # 不输出异常文本/SQL/请求路径（可能包含调用方提供的秘密）。
                    log.error("业务响应已成功，通用管理员审计写入失败；method=%s error_type=%s", method, type(exc).__name__)
            if message["type"] == "http.response.start" and path.startswith("/api/auth/"):
                message = {**message, "headers": [(key, value) for key, value in message.get("headers", []) if key.lower() != b"cache-control"] + [(b"cache-control", b"no-store")]}
            await send(message)

        await self.app(scope, receive, authenticated_send)
