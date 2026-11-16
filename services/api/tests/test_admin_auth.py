"""单管理员认证：独立 PostgreSQL schema 和临时 ASGI 应用。"""
import hashlib
import json
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker


class AdminAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import auth
        from app.auth_models import AdminAccount, AdminAudit, AdminSession
        from app.models import Base

        cls.auth = auth
        cls.Account, cls.Audit, cls.AuthSession = AdminAccount, AdminAudit, AdminSession
        cls.Base = Base
        url = os.environ.get("WORKFLOW_TEST_DATABASE_URL")
        if not url:
            raise unittest.SkipTest("请显式配置 WORKFLOW_TEST_DATABASE_URL，使用专用测试数据库")
        cls.admin_engine = create_engine(url)
        cls.schema = "test_auth_" + uuid.uuid4().hex
        with cls.admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        try:
            cls.engine = create_engine(url, connect_args={"options": f"-csearch_path={cls.schema}"})
            Base.metadata.create_all(cls.engine)
            cls.Session = sessionmaker(bind=cls.engine)
        except Exception:
            if hasattr(cls, "engine"):
                cls.engine.dispose()
            with cls.admin_engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
            cls.admin_engine.dispose()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        with cls.admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
        cls.admin_engine.dispose()

    def setUp(self):
        with self.engine.begin() as connection:
            names = ",".join(f'"{table.name}"' for table in self.Base.metadata.sorted_tables)
            connection.execute(text("TRUNCATE " + names + " CASCADE"))
        self.session_is_valid = lambda raw: self.auth.session_is_valid(raw, session_factory=self.Session)
        self.env_patch = patch.dict(os.environ, {"AUTH_COOKIE_SECURE": "false"})
        self.env_patch.start()
        self.app = FastAPI()
        self.app.include_router(self.auth.router, prefix="/api")

        @self.app.get("/api/private")
        async def private(request: Request):
            return {"user": request.state.admin}

        @self.app.post("/api/private")
        async def write(request: Request):
            return {"user": request.state.admin, "ok": True}

        @self.app.get("/api/events/{event_id}/export")
        async def export(event_id: str):
            return {"event_id": event_id}

        @self.app.post("/api/rejected", status_code=409)
        async def rejected():
            return {"detail": "conflict"}

        @self.app.get("/api/stream")
        async def stream():
            async def events():
                yield b"data: first\n\n"
                yield b"data: second\n\n"
            return StreamingResponse(events(), media_type="text/event-stream")

        self.app.add_middleware(self.auth.AdminAuthMiddleware, session_factory=self.Session)
        self.client = TestClient(self.app)
        self.username = "admin_" + uuid.uuid4().hex[:12]
        self.password = "PrivatePassword123!"
        self.new_password = "ChangedPassword456!"
        self.xhr = {"X-Requested-With": "XMLHttpRequest"}

    def tearDown(self):
        self.client.close()
        self.env_patch.stop()

    def setup_admin(self, client=None, **changes):
        client = client or self.client
        response = client.post("/api/auth/setup", headers=self.xhr, json={"username": self.username, "display_name": "管理员", "password": self.password, **changes})
        self.assertEqual(response.status_code, 201, response.text)
        self.csrf = response.json()["csrf_token"]
        return response

    def login(self, client=None, **changes):
        client = client or self.client
        return client.post("/api/auth/login", headers=self.xhr, json={"username": self.username, "password": self.password, **changes})

    def headers(self, **changes):
        return {**self.xhr, "X-CSRF-Token": self.csrf, **changes}

    def test_no_default_admin_and_all_api_and_docs_require_auth(self):
        response = self.client.get("/api/auth/status")
        self.assertEqual(response.json(), {"initialized": False, "user": None, "csrf_token": None})
        self.assertEqual(response.headers["cache-control"], "no-store")
        for path in ("/api/private", "/api/auth/audit", "/api/stream", "/api/missing", "/api", "/docs", "/redoc", "/openapi.json"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.options("/api/private", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"}).status_code, 401)
        self.assertEqual(self.login().status_code, 409)

    def test_setup_has_salted_scrypt_digest_only_session_and_eight_hour_cookie(self):
        response = self.setup_admin()
        self.assertEqual(response.json()["user"], {"username": self.username, "display_name": "管理员", "role": "admin"})
        cookie = response.headers["set-cookie"]
        for expected in ("eval_admin_session=", "HttpOnly", "Max-Age=28800", "Path=/", "SameSite=strict"):
            self.assertIn(expected, cookie)
        self.assertNotIn("Secure", cookie)
        raw = self.client.cookies.get("eval_admin_session")
        self.assertTrue(self.session_is_valid(raw))
        with self.Session() as db:
            account = db.query(self.Account).one()
            session = db.query(self.AuthSession).one()
            self.assertIn("scrypt", account.password_hash)
            self.assertNotIn(self.password, account.password_hash)
            values = [getattr(session, c.name) for c in session.__table__.columns]
            self.assertIn(hashlib.sha256(raw.encode()).hexdigest(), values)
            self.assertNotIn(raw, values)
            remaining = (session.expires_at - datetime.now(timezone.utc)).total_seconds()
            self.assertGreater(remaining, 28700)
            self.assertLessEqual(remaining, 28800)
        self.assertEqual(self.client.get("/api/private").json()["user"]["username"], self.username)
        self.assertEqual(self.client.get("/api/auth/status").json()["csrf_token"], self.csrf)
        self.assertEqual(self.client.post("/api/auth/setup", headers=self.xhr, json={"username": "other", "display_name": "其他", "password": self.password}).status_code, 409)

    def test_setup_serializes_and_database_rejects_second_account(self):
        def create(index):
            with TestClient(self.app, client=(f"198.51.100.{index + 1}", 50000)) as client:
                return client.post("/api/auth/setup", headers=self.xhr, json={"username": self.username + str(index), "display_name": "管理员", "password": self.password}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(create, (0, 1))), [201, 409])
        with self.Session() as db:
            self.assertEqual(db.query(self.Account).count(), 1)
            db.add(self.Account(account_id=2, username="another", display_name="其他", password_hash="not-a-password"))
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()

    def test_input_validation_and_secure_cookie_setting(self):
        for changes in ({"username": "ab"}, {"username": "中文admin"}, {"username": "a" * 33}, {"display_name": " \t "}, {"display_name": "x" * 65}, {"password": "onlyletterslong"}, {"password": "12345678901"}, {"password": "Short123"}, {"password": "a1" * 65}):
            with self.subTest(changes=list(changes)):
                response = self.client.post("/api/auth/setup", headers=self.xhr, json={"username": self.username, "display_name": "管理员", "password": self.password, **changes})
                self.assertEqual(response.status_code, 422, response.text)
        with patch.dict(os.environ, {"AUTH_COOKIE_SECURE": "true"}), TestClient(self.app, base_url="https://testserver") as secure:
            response = self.setup_admin(secure)
            self.assertIn("; Secure", response.headers["set-cookie"])
            self.assertEqual(secure.get("/api/private").status_code, 200)

    def test_setup_and_login_require_xhr_and_same_origin(self):
        payload = {"username": self.username, "display_name": "管理员", "password": self.password}
        self.assertEqual(self.client.post("/api/auth/setup", json=payload).status_code, 403)
        for origin in ("http://evil.example", "https://testserver", "http://testserver:9999", "null"):
            self.assertEqual(self.client.post("/api/auth/setup", headers={**self.xhr, "Origin": origin}, json=payload).status_code, 403)
        response = self.client.post("/api/auth/setup", headers={**self.xhr, "Origin": "http://testserver"}, json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(self.client.post("/api/auth/login", json=payload).status_code, 403)
        self.assertEqual(self.client.post("/api/auth/login", headers={**self.xhr, "Origin": "http://evil.example"}, json=payload).status_code, 403)
        self.assertEqual(self.login().status_code, 200)

    def test_csrf_is_required_only_for_authenticated_writes(self):
        self.setup_admin()
        self.assertEqual(self.client.get("/api/private").status_code, 200)
        self.assertEqual(self.client.post("/api/private", headers=self.xhr).status_code, 403)
        self.assertEqual(self.client.post("/api/private", headers={"X-CSRF-Token": self.csrf}).status_code, 403)
        self.assertEqual(self.client.post("/api/private", headers=self.headers(**{"X-CSRF-Token": "incorrect"})).status_code, 403)
        self.assertEqual(self.client.post("/api/private", headers=self.headers(Origin="http://evil.example")).status_code, 403)
        self.assertEqual(self.client.post("/api/private", headers=self.headers(Origin="http://testserver")).status_code, 200)
        self.assertEqual(self.client.options("/api/private").status_code, 405)

    def test_login_wrong_password_expired_and_forged_cookie(self):
        self.setup_admin()
        self.client.cookies.clear()
        self.assertEqual(self.login(password="WrongPassword123").status_code, 401)
        response = self.login()
        self.assertEqual(response.status_code, 200, response.text)
        raw = self.client.cookies.get("eval_admin_session")
        with self.Session() as db:
            db.query(self.AuthSession).update({"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
            db.commit()
        self.assertFalse(self.session_is_valid(raw))
        self.assertEqual(self.client.get("/api/private").status_code, 401)
        self.assertEqual(self.client.get("/api/auth/status").json(), {"initialized": True, "user": None, "csrf_token": None})
        self.client.cookies.set("eval_admin_session", "forged")
        self.assertEqual(self.client.get("/api/private").status_code, 401)
        self.assertFalse(self.session_is_valid(None))

    def test_logout_revokes_current_session_and_clears_cookie(self):
        self.setup_admin()
        raw = self.client.cookies.get("eval_admin_session")
        response = self.client.post("/api/auth/logout", headers=self.headers())
        self.assertEqual(response.json(), {"ok": True})
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        self.assertFalse(self.session_is_valid(raw))
        self.assertEqual(self.client.get("/api/private").status_code, 401)

    def test_password_change_revokes_all_sessions_and_changes_salt(self):
        self.setup_admin()
        raw = self.client.cookies.get("eval_admin_session")
        with self.Session() as db:
            old_hash = db.query(self.Account).one().password_hash
        with TestClient(self.app, client=("198.51.100.80", 50000)) as other:
            self.assertEqual(self.login(other).status_code, 200)
            other_raw = other.cookies.get("eval_admin_session")
            bad = self.client.post("/api/auth/password", headers=self.headers(), json={"current_password": "WrongPassword123", "new_password": self.new_password})
            self.assertEqual(bad.status_code, 400)
            self.assertTrue(self.session_is_valid(raw))
            self.assertEqual(self.client.get("/api/private").status_code, 200)
            response = self.client.post("/api/auth/password", headers=self.headers(), json={"current_password": self.password, "new_password": self.new_password})
            self.assertEqual(response.json(), {"ok": True})
            self.assertFalse(self.session_is_valid(raw))
            self.assertFalse(self.session_is_valid(other_raw))
            self.assertEqual(other.get("/api/private").status_code, 401)
            with self.Session() as db:
                self.assertNotEqual(db.query(self.Account).one().password_hash, old_hash)
                self.assertEqual(db.query(self.AuthSession).count(), 0)
            self.assertEqual(self.login(other).status_code, 401)
            self.assertEqual(self.login(other, password=self.new_password).status_code, 200)

    def test_login_failures_are_throttled_by_username_and_client_ip(self):
        self.setup_admin()
        ip = "203.0.113." + str(int(uuid.uuid4().hex[:2], 16))
        with TestClient(self.app, client=(ip, 50000)) as attacker:
            statuses = [self.login(attacker, password="Incorrect12345").status_code for _ in range(12)]
            self.assertIn(401, statuses)
            self.assertIn(429, statuses)
            self.assertEqual(self.login(attacker, password=self.password).status_code, 429)
            self.assertEqual(self.login(attacker, username="other_" + uuid.uuid4().hex[:8], password="Incorrect12345").status_code, 429)
        with TestClient(self.app, client=("198.51.100.99", 50000)) as another_ip:
            self.assertEqual(self.login(another_ip).status_code, 429)

    def test_successful_writes_exports_and_identity_changes_are_audited_without_secrets(self):
        self.setup_admin()
        raw = self.client.cookies.get("eval_admin_session")
        self.assertEqual(self.client.post("/api/private?password=QuerySecret123", headers=self.headers(), json={"password": "BodySecret123", "actor": "forged"}).status_code, 200)
        self.assertEqual(self.client.get("/api/private").status_code, 200)
        self.assertEqual(self.client.post("/api/rejected", headers=self.headers()).status_code, 409)
        self.assertEqual(self.client.get("/api/events/123/export?token=QuerySecret123").status_code, 200)
        response = self.client.get("/api/auth/audit")
        self.assertEqual(response.status_code, 200, response.text)
        audits = response.json()
        self.assertIsInstance(audits, list)
        for audit in audits:
            self.assertEqual(audit["actor"], self.username)
            self.assertTrue({"actor", "action", "path", "method", "at"}.issubset(audit))
        self.assertTrue(any(a["path"] == "/api/private" and a["method"] == "POST" for a in audits))
        self.assertTrue(any(a["path"] == "/api/events/123/export" for a in audits))
        self.assertFalse(any(a["path"] == "/api/private" and a["method"] == "GET" for a in audits))
        self.assertFalse(any(a["path"] == "/api/rejected" for a in audits))
        serialized = json.dumps(audits, ensure_ascii=False)
        for secret in (self.password, self.csrf, raw, "QuerySecret123", "BodySecret123", "forged"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_audit_returns_only_latest_hundred_and_streaming_is_preserved(self):
        self.setup_admin()
        with self.Session() as db:
            for index in range(105):
                db.add(self.Audit(actor=self.username, action="test", path=f"/api/item/{index}", method="POST"))
            db.commit()
        rows = self.client.get("/api/auth/audit").json()
        self.assertEqual(len(rows), 100)
        self.assertEqual(rows[0]["path"], "/api/item/104")
        self.assertEqual(rows[-1]["path"], "/api/item/5")
        response = self.client.get("/api/stream")
        self.assertEqual(response.text, "data: first\n\ndata: second\n\n")
        self.assertIn("text/event-stream", response.headers["content-type"])

    def test_validation_responses_never_echo_password_or_body_secrets(self):
        setup_secret = "SECRET_SETUP_PASSWORD"
        response = self.client.post("/api/auth/setup", headers=self.xhr, json={"username": "ab", "display_name": "管理员", "password": setup_secret})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(setup_secret, response.text)
        weak_secret = "LettersOnlySecret"
        response = self.client.post("/api/auth/setup", headers=self.xhr, json={"username": self.username, "display_name": "管理员", "password": weak_secret})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(weak_secret, response.text)
        self.setup_admin()
        for path, payload, secrets in (
            ("/api/auth/login", {"username": self.username, "password": "a1" * 100}, ["a1" * 100]),
            ("/api/auth/login", {"password": "LoginSecret123"}, ["LoginSecret123"]),
            ("/api/auth/password", {"current_password": self.password, "new_password": "NewLettersSecret"}, [self.password, "NewLettersSecret"]),
        ):
            with self.subTest(path=path):
                response = self.client.post(path, headers=self.headers(), json=payload)
                self.assertEqual(response.status_code, 422, response.text)
                for secret in secrets:
                    self.assertNotIn(secret, response.text)
                self.assertTrue(all("input" not in item and "ctx" not in item for item in response.json()["detail"]))

    def test_generic_audit_failure_does_not_replace_committed_business_response(self):
        self.setup_admin()
        with patch.object(self.auth, "_save_business_audit", side_effect=RuntimeError("SensitiveDatabaseError")):
            with self.assertLogs("app.auth", level="ERROR") as logs:
                response = self.client.post("/api/private?password=QuerySecret123", headers=self.headers(), json={"password": "BodySecret123"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"])
        self.assertIn("审计", " ".join(logs.output))
        for secret in ("SensitiveDatabaseError", "QuerySecret123", "BodySecret123", self.password):
            self.assertNotIn(secret, " ".join(logs.output))


if __name__ == "__main__":
    unittest.main()
