"""8.2 semantic task API: isolated PostgreSQL only."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import unittest
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker


class SemanticAnalysisApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        required = [root / "app/semantic_models.py", root / "app/routers/semantic_analyses.py"]
        if not all(path.exists() for path in required):
            raise AssertionError("8.2语义任务API尚未实现")
        from app import audio_models
        from app.auth import AdminAuthMiddleware
        from app.auth_models import AdminAccount, AdminAudit, AdminSession
        from app.db import get_session
        from app.models import Base, Event
        from app.supervision_models import SemanticEventLink, SupervisionIntakeFailure, RecordingCaptureTime
        from app.routers.semantic_analyses import router
        from app.semantic_models import SemanticAnalysis

        url = os.environ.get("SEMANTIC_TEST_DATABASE_URL")
        if not url:
            raise RuntimeError("SEMANTIC_TEST_DATABASE_URL必须显式指定PostgreSQL测试连接")
        cls.admin = create_engine(url)
        if cls.admin.dialect.name != "postgresql":
            raise RuntimeError("不得使用SQLite替代PostgreSQL验收")
        cls.schema = "test_semantic_v2_" + uuid4().hex
        with cls.admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        cls.engine = create_engine(url, connect_args={"options": f"-csearch_path={cls.schema} -clock_timeout=5000 -cstatement_timeout=15000"})
        tables = [audio_models.AudioRecording.__table__, audio_models.AudioSegment.__table__, SemanticAnalysis.__table__, Event.__table__, SemanticEventLink.__table__, SupervisionIntakeFailure.__table__, RecordingCaptureTime.__table__,
                  AdminAccount.__table__, AdminAudit.__table__, AdminSession.__table__]
        Base.metadata.create_all(cls.engine, tables=tables)
        cls.Session = sessionmaker(bind=cls.engine)
        cls.Recording = audio_models.AudioRecording
        cls.Analysis = SemanticAnalysis
        cls.AuthSession = AdminSession
        with cls.Session.begin() as db:
            db.add(AdminAccount(account_id=1, username="semantic_admin", display_name="Semantic admin", password_hash="not-a-real-password"))
        cls.app = FastAPI()
        cls.app.add_middleware(AdminAuthMiddleware, session_factory=cls.Session)
        cls.app.include_router(router, prefix="/api")

        def override_db():
            with cls.Session() as db:
                yield db
        cls.app.dependency_overrides[get_session] = override_db

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "engine"):
            cls.engine.dispose()
        if hasattr(cls, "schema"):
            with cls.admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
            cls.admin.dispose()

    def setUp(self):
        token, csrf = secrets.token_urlsafe(32), secrets.token_hex(32)
        with self.Session.begin() as db:
            db.add(self.AuthSession(token_hash=hashlib.sha256(token.encode("ascii")).hexdigest(), account_id=1,
                                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1), csrf_token=csrf))
        self.client = TestClient(self.app, headers={"X-Requested-With": "XMLHttpRequest", "X-CSRF-Token": csrf})
        self.client.cookies.set("eval_admin_session", token)
        self.recording_id = str(uuid4())
        with self.Session.begin() as db:
            db.add(self.Recording(recording_id=self.recording_id, title="测试录音", source_id="audio-source-1",
                                  source_type="upload", source_key=str(uuid4()), status="ready", duration_ms=60000,
                                  created_at=datetime.now(timezone.utc), segment_count=1, sha256="a" * 64,
                                  original_path="original.wav", pcm_path="pcm.wav"))

    def tearDown(self):
        self.client.close()
        with self.Session.begin() as db:
            db.query(self.Analysis).delete()
            db.query(self.Recording).delete()

    def request_body(self, **changes):
        value = {
            "contract_version": "2.0.0", "analysis_id": "client-must-not-control", "recording_id": self.recording_id,
            "project_id": "project-1", "room_id": "room-1", "session_id": "session-1", "stage_snapshot_id": "snapshot-1",
            "phase_id": "DETAIL", "phase_version": 1, "scene_id": "S06", "rule_version": "s06-v1",
            "prompt_version": "s06-prompt-v1", "target_segment_ids": ["seg-1"],
            "segments": [{"segment_id": "seg-1", "speaker": "A", "start_ms": 1000, "end_ms": 5000, "text": "按标准逐项核对。"}],
            "context": {"speaker_bindings": [], "recipient_authorized": None, "recipient_fact_id": None,
                        "confidentiality_policy_id": None, "information_visibility": "unknown",
                        "applicable_rules": [{"rule_id": "rule-1", "rule_version": "s06-v1", "text": "正常讨论与不当施压需区分。"}],
                        "exceptions": [], "missing_facts": []},
            "source_id": "audio-source-1", "binding_status": "bound", "media_run_id": "run-1", "asset_id": "asset-1",
            "occurred_at": "2026-10-15T02:00:00Z", "asr_model": "paraformer", "asr_version": "v1",
            "speaker_model": "campplus", "speaker_model_version": "v1", "model_name": "MiniMax-M3.1-Flash-Preview",
            "model_version": "cloud-preview", "window_start_ms": 1000, "window_end_ms": 5000, "quality_flags": []
        }
        value.update(changes)
        return {"input": value}

    def create(self, body=None):
        return self.client.post(f"/api/audio/recordings/{self.recording_id}/semantic-analyses", json=body or self.request_body())

    def test_non_ready_recording_is_not_queued(self):
        with self.Session.begin() as db:
            db.get(self.Recording, self.recording_id).status = "processing"
        response = self.create()
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("RECORDING_NOT_READY", response.json()["detail"]["code"])
        with self.Session() as db:
            self.assertEqual([], db.scalars(select(self.Analysis)).all())

    def test_ready_recording_creates_snapshot_and_same_task_is_idempotent(self):
        first = self.create()
        self.assertEqual(202, first.status_code, first.text)
        self.assertEqual("queued", first.json()["status"])
        self.assertNotEqual("client-must-not-control", first.json()["analysis_id"])
        second = self.create()
        self.assertEqual(200, second.status_code, second.text)
        self.assertTrue(second.json()["deduplicated"])
        self.assertEqual(first.json()["analysis_id"], second.json()["analysis_id"])
        listed = self.client.get(f"/api/audio/recordings/{self.recording_id}/semantic-analyses")
        self.assertEqual(200, listed.status_code)
        self.assertEqual(1, len(listed.json()["items"]))
        self.assertEqual("bound", listed.json()["items"][0]["request"]["binding_status"])

    def test_different_window_gets_different_task(self):
        first = self.create()
        second = self.create(self.request_body(
            window_start_ms=6000, window_end_ms=10000,
            segments=[{"segment_id": "seg-1", "speaker": "A", "start_ms": 6000, "end_ms": 10000, "text": "按标准逐项核对。"}],
        ))
        self.assertEqual(202, first.status_code)
        self.assertEqual(202, second.status_code)
        self.assertNotEqual(first.json()["analysis_id"], second.json()["analysis_id"])

    def test_failed_task_can_retry_but_ready_task_cannot(self):
        created = self.create().json()
        with self.Session.begin() as db:
            row = db.get(self.Analysis, created["analysis_id"])
            row.status = "failed"
            row.error = {"code": "MODEL_UNAVAILABLE", "retryable": True}
            row.claim_id = "old-claim"
        retried = self.client.post(f"/api/audio/semantic-analyses/{created['analysis_id']}/retry")
        self.assertEqual(202, retried.status_code, retried.text)
        self.assertEqual("queued", retried.json()["status"])
        with self.Session.begin() as db:
            row = db.get(self.Analysis, created["analysis_id"])
            row.status = "ready"
        rejected = self.client.post(f"/api/audio/semantic-analyses/{created['analysis_id']}/retry")
        self.assertEqual(409, rejected.status_code, rejected.text)
        self.assertEqual("ANALYSIS_NOT_FAILED", rejected.json()["detail"]["code"])

    def test_stale_claim_cannot_overwrite_new_claim(self):
        created = self.create().json()
        from app.semantic_models import claim_task, complete_task
        with self.Session.begin() as db:
            first_claim = claim_task(db, created["analysis_id"], "worker-a")
            db.flush()
        with self.Session.begin() as db:
            row = db.get(self.Analysis, created["analysis_id"])
            row.status = "queued"
            second_claim = claim_task(db, created["analysis_id"], "worker-b")
            db.flush()
        self.assertNotEqual(first_claim, second_claim)
        with self.Session.begin() as db:
            self.assertFalse(complete_task(db, created["analysis_id"], first_claim, {"scene_id": "S06"}))
            self.assertEqual("processing", db.get(self.Analysis, created["analysis_id"]).status)
            self.assertTrue(complete_task(db, created["analysis_id"], second_claim, {"scene_id": "S06"}))

    def test_auth_and_csrf_are_required(self):
        anonymous = TestClient(self.app, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(401, anonymous.post(f"/api/audio/recordings/{self.recording_id}/semantic-analyses", json=self.request_body()).status_code)
        anonymous.close()
        no_csrf = TestClient(self.app, headers={"X-Requested-With": "XMLHttpRequest"})
        no_csrf.cookies.set("eval_admin_session", self.client.cookies.get("eval_admin_session"))
        self.assertEqual(403, no_csrf.post(f"/api/audio/recordings/{self.recording_id}/semantic-analyses", json=self.request_body()).status_code)
        no_csrf.close()


def run_isolated_pg():
    name = "poc-semantic-test-" + uuid4().hex[:12]
    password = uuid4().hex
    old_url = os.environ.get("SEMANTIC_TEST_DATABASE_URL")
    started = False
    try:
        result = subprocess.run(["docker", "run", "-d", "--name", name, "--tmpfs", "/var/lib/postgresql/data",
                                 "-e", "POSTGRES_USER=semantic_test", "-e", "POSTGRES_PASSWORD=" + password,
                                 "-e", "POSTGRES_DB=semantic_test", "-p", "127.0.0.1::5432", "postgres:16-alpine"], capture_output=True)
        if result.returncode:
            raise RuntimeError("独立PG启动失败")
        started = True
        deadline = time.monotonic() + 60
        while subprocess.run(["docker", "exec", name, "pg_isready", "-U", "semantic_test", "-d", "semantic_test"], capture_output=True).returncode:
            if time.monotonic() >= deadline:
                raise RuntimeError("独立PG等待超时")
            time.sleep(0.5)
        port = subprocess.check_output(["docker", "port", name, "5432/tcp"]).decode().strip().split(":")[-1]
        os.environ["SEMANTIC_TEST_DATABASE_URL"] = f"postgresql+psycopg://semantic_test:{password}@127.0.0.1:{port}/semantic_test"
        result = unittest.main(argv=[sys.argv[0], "-v"], exit=False)
        return result.result.wasSuccessful()
    finally:
        if started:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=True)
        if old_url is None:
            os.environ.pop("SEMANTIC_TEST_DATABASE_URL", None)
        else:
            os.environ["SEMANTIC_TEST_DATABASE_URL"] = old_url


if __name__ == "__main__":
    sys.exit(0 if run_isolated_pg() else 1) if "--isolated-pg" in sys.argv else unittest.main(verbosity=2)
