"""执行卡 4.3：素材绑定表、时间轴、接口。

同一份测试双后端：设 MEDIA_BINDINGS_TEST_DATABASE_URL 走真实 PostgreSQL 随机 schema（另一台机器），
否则 SQLite 临时文件（本机桩验证：B 模型的 JSONB 换成 JSON、PostgreSQL 专用 CHECK 去掉，只在测试进程内）。
运行（services/api 下）：PYTHONPATH=. python -m unittest discover -s tests -p test_media_bindings_v2.py -v
"""
import hashlib
import os
import secrets
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import JSON, CheckConstraint, MetaData, create_engine, event, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from app.asset_models import StageAsset
from app.audio_models import AudioRecording
from app.auth import AdminAuthMiddleware
from app.auth_models import AdminAccount, AdminAudit, AdminSession
from app.business_models import BusinessParticipant, BusinessPerson, BusinessProject, BusinessRoom, BusinessSeat
from app.db import get_session
from app.stage_media_binding_models import StageMediaBinding as MediaBinding
from app.stage_media_bindings import MediaBindingError, build_timeline, locate, segment_at
from app.models import Base, EvalSession
from app.phase_config import get_snapshot
from app.phase_models import PhaseBinding, PhaseSnapshot
from app.routers.stage_media_bindings import router

T0 = datetime(2026, 10, 19, 2, 0, 0, tzinfo=timezone.utc)


def utc(seconds):
    return (T0 + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class MediaBindingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = [BusinessProject.__table__, BusinessRoom.__table__, BusinessPerson.__table__, BusinessParticipant.__table__,
                      BusinessSeat.__table__, EvalSession.__table__, PhaseBinding.__table__, PhaseSnapshot.__table__,
                      AdminAccount.__table__, AdminAudit.__table__, AdminSession.__table__, StageAsset.__table__,
                      AudioRecording.__table__, MediaBinding.__table__]
        url = os.environ.get("MEDIA_BINDINGS_TEST_DATABASE_URL")
        cls.tmpdb = None
        cls.engine = None
        try:
            if url:
                cls.backend = "postgresql"
                cls.admin = create_engine(url)
                cls.schema = "test_bindings_v2_" + uuid4().hex
                with cls.admin.begin() as connection:
                    connection.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
                cls.engine = create_engine(url, connect_args={"options": f"-csearch_path={cls.schema} -cstatement_timeout=15000"})
                Base.metadata.create_all(cls.engine, tables=cls.tables)
            else:
                cls.backend = "sqlite"
                cls.tmpdb = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
                cls.engine = create_engine(f"sqlite:///{Path(cls.tmpdb.name) / 'bindings.db'}")

                @event.listens_for(cls.engine, "connect")
                def sqlite_compat(dbapi_connection, _record):
                    dbapi_connection.create_function("btrim", 1, lambda value: value.strip() if value is not None else None)
                    dbapi_connection.execute("PRAGMA foreign_keys=ON")
                safe = MetaData()
                for table in cls.tables:
                    table.to_metadata(safe)
                for table in safe.tables.values():
                    for column in table.columns:
                        if isinstance(column.type, JSONB):
                            column.type = JSON()
                    for constraint in list(table.constraints):
                        if isinstance(constraint, CheckConstraint) and ("~" in str(constraint.sqltext) or "::" in str(constraint.sqltext)):
                            table.constraints.discard(constraint)
                safe.create_all(cls.engine)
                import app.auth as auth_module
                cls._now_patch = mock.patch.object(auth_module, "_now", lambda: datetime.now(timezone.utc).replace(tzinfo=None))
                cls._now_patch.start()
            cls.Session = sessionmaker(bind=cls.engine)
            with cls.Session.begin() as db:
                db.add(AdminAccount(account_id=1, username="binding_admin", display_name="测试管理员", password_hash="not-a-real-password"))
            cls.app = FastAPI()
            cls.app.add_middleware(AdminAuthMiddleware, session_factory=cls.Session)
            cls.app.include_router(router, prefix="/api")

            def override_db():
                with cls.Session() as db:
                    yield db
            cls.app.dependency_overrides[get_session] = override_db
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        patch = getattr(cls, "_now_patch", None)
        if patch is not None:
            patch.stop()
            cls._now_patch = None
        if cls.engine is not None:
            cls.engine.dispose()
        if getattr(cls, "backend", None) == "postgresql" and hasattr(cls, "schema"):
            with cls.admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
            cls.admin.dispose()
        elif cls.tmpdb is not None:
            cls.tmpdb.cleanup()

    def setUp(self):
        self.session_id, self.project, self.room = (str(uuid4()) for _ in range(3))
        self.source = "mic-binding-test"
        self.recording_id = str(uuid4())
        token, self.csrf = secrets.token_urlsafe(32), secrets.token_hex(32)
        with self.Session.begin() as db:
            db.add(BusinessProject(project_id=self.project, code=self.project, name="测试项目", owner="测试", status="running"))
            db.flush()
            db.add(BusinessRoom(room_id=self.room, project_id=self.project, code=self.room, name="测试评标室", location="测试", enabled=True))
            db.add(EvalSession(session_id=self.session_id, name="绑定测试场次", status="running", current_phase="review"))
            db.flush()
            db.add(PhaseBinding(session_id=self.session_id, project_id=self.project, room_id=self.room, source_id=self.source, active=True))
            db.add(AdminSession(token_hash=hashlib.sha256(token.encode("ascii")).hexdigest(), account_id=1,
                                expires_at=datetime.now(timezone.utc) + timedelta(hours=1), csrf_token=self.csrf))
            db.add(AudioRecording(recording_id=self.recording_id, title="talk.wav", source_id="upload", source_type="upload", status="ready",
                                  duration_ms=90000, created_at=datetime.now(timezone.utc), sha256="a" * 64, original_path="x.wav", segment_count=0))
            db.add(StageAsset(session_id=self.session_id, asset_id="score-01", asset_type="scoring", relative_path="authorized/score.json",
                              sha256="b" * 64, duration_ms=None, authorization_source="auth-1", visibility_notes="评分", size_bytes=10,
                              probe={"status": "not_applicable"}, registered_by="binding_admin"))
            db.add(StageAsset(session_id=self.session_id, asset_id="talk-01", asset_type="audio", relative_path="authorized/talk.wav",
                              sha256="c" * 64, duration_ms=90000, authorization_source="auth-1", visibility_notes="录音", size_bytes=10,
                              probe={"status": "unavailable"}, registered_by="binding_admin"))
        self.client = TestClient(self.app, headers={"X-Requested-With": "XMLHttpRequest", "X-CSRF-Token": self.csrf})
        self.client.cookies.set("eval_admin_session", token)
        self.url = f"/api/sessions/{self.session_id}/media-bindings"

    def tearDown(self):
        self.client.close()

    # ---- helpers ----
    def add_snapshots(self, *specs):
        """specs: (phase_id, version, effective_seconds)"""
        with self.Session.begin() as db:
            for phase_id, version, seconds in specs:
                db.add(PhaseSnapshot(stage_snapshot_id=f"snap-{version}-{self.session_id[:8]}", session_id=self.session_id, project_id=self.project,
                                     room_id=self.room, source_id=self.source, phase_id=phase_id, phase_version=version,
                                     effective_at=T0 + timedelta(seconds=seconds), enabled_scenes=["S06"], always_on_scenes=["S10"],
                                     assets=[], rule_versions={"S06": "s06-v1", "S10": "s10-v1"}, exceptions=[], sync_status="pending",
                                     actor_id="tester", reason="测试"))

    def body(self, **overrides):
        base = {"request_id": "req-" + uuid4().hex[:12], "kind": "recording", "subject_id": self.recording_id, "started_at": utc(0)}
        base.update(overrides)
        return base

    def post(self, body):
        return self.client.post(self.url, json=body)

    def code(self, response):
        detail = response.json()["detail"]
        return detail["code"] if isinstance(detail, dict) else "VALIDATION"

    # ---- AC-1 ----
    def test_ac1_bind_recording_returns_timeline_and_scope(self):
        self.add_snapshots(("DETAIL", 1, -60))
        response = self.post(self.body())
        self.assertEqual(201, response.status_code, response.text)
        value = response.json()
        self.assertEqual((value["project_id"], value["room_id"], value["source_id"]), (self.project, self.room, self.source))
        self.assertEqual((value["kind"], value["subject_id"], value["duration_ms"]), ("recording", self.recording_id, 90000))
        self.assertEqual(value["started_at"], utc(0))
        self.assertEqual(value["timeline"], [{"from_ms": 0, "to_ms": 90000, "stage_snapshot_id": f"snap-1-{self.session_id[:8]}",
                                              "phase_id": "DETAIL", "phase_version": 1, "binding_status": "bound"}])
        self.assertEqual(value["created_by"], "binding_admin")
        listing = self.client.get(self.url)
        self.assertEqual([b["binding_id"] for b in listing.json()["bindings"]], [value["binding_id"]])

    # ---- AC-2 ----
    def test_ac2_idempotency_and_conflicts(self):
        self.add_snapshots(("DETAIL", 1, -60))
        body = self.body()
        first = self.post(body)
        self.assertEqual(201, first.status_code)
        again = self.post(body)
        self.assertEqual(200, again.status_code)
        self.assertEqual(first.json()["binding_id"], again.json()["binding_id"])
        changed = self.post(dict(body, started_at=utc(5)))
        self.assertEqual((409, "REQUEST_CONFLICT"), (changed.status_code, self.code(changed)))
        same_subject = self.post(self.body())                        # 新 request_id、同内容
        self.assertEqual(200, same_subject.status_code)
        self.assertEqual(first.json()["binding_id"], same_subject.json()["binding_id"])
        other_content = self.post(self.body(started_at=utc(5)))
        self.assertEqual((409, "BINDING_CONFLICT"), (other_content.status_code, self.code(other_content)))
        with self.Session() as db:
            self.assertEqual(1, len(db.scalars(select(MediaBinding).where(MediaBinding.session_id == self.session_id)).all()))

    # ---- AC-3 ----
    def test_ac3_timeline_splits_on_phase_boundaries_and_marks_unbound_head(self):
        self.add_snapshots(("PRELIM", 1, 10), ("DETAIL", 2, 40))      # 起点 0 秒，首快照 10 秒生效，第二份 40 秒
        value = self.post(self.body()).json()
        self.assertEqual([(s["from_ms"], s["to_ms"], s["phase_id"], s["binding_status"]) for s in value["timeline"]],
                         [(0, 10000, None, "unbound"), (10000, 40000, "PRELIM", "bound"), (40000, 90000, "DETAIL", "bound")])
        self.assertEqual(value["timeline"][2]["phase_version"], 2)
        pure = build_timeline([], T0, 5000)
        self.assertEqual(pure, [{"from_ms": 0, "to_ms": 5000, "stage_snapshot_id": None, "phase_id": None, "phase_version": None, "binding_status": "unbound"}])
        boundary = build_timeline([{"stage_snapshot_id": "s", "phase_id": "DETAIL", "phase_version": 1, "effective_at": T0}], T0, 5000)
        self.assertEqual([(s["from_ms"], s["to_ms"], s["binding_status"]) for s in boundary], [(0, 5000, "bound")])
        open_ended = build_timeline([{"stage_snapshot_id": "s", "phase_id": "DETAIL", "phase_version": 1, "effective_at": T0 + timedelta(seconds=3)}], T0, None)
        self.assertEqual([(s["from_ms"], s["to_ms"]) for s in open_ended], [(0, 3000), (3000, None)])

    # ---- AC-10 ----
    def test_ac10_timeline_agrees_with_phase_config_get_snapshot(self):
        self.add_snapshots(("PRELIM", 1, 10), ("DETAIL", 2, 40), ("REPORT", 3, 70))
        value = self.post(self.body()).json()
        with self.Session() as db:
            for segment in value["timeline"]:
                for ms in (segment["from_ms"], (segment["from_ms"] + segment["to_ms"]) // 2, segment["to_ms"] - 1):
                    expected = get_snapshot(self.session_id, T0 + timedelta(milliseconds=ms), db=db)
                    self.assertEqual(expected["stage_snapshot_id"] if expected else None, segment["stage_snapshot_id"], ms)

    # ---- AC-4 ----
    def test_ac4_locate_both_directions(self):
        self.add_snapshots(("PRELIM", 1, 10), ("DETAIL", 2, 40))
        binding_id = self.post(self.body()).json()["binding_id"]
        forward = self.client.get(f"/api/media-bindings/{binding_id}/locate", params={"media_ms": 45000}).json()
        self.assertEqual((forward["occurred_at"], forward["segment"]["phase_id"], forward["binding_status"]), (utc(45), "DETAIL", "bound"))
        backward = self.client.get(f"/api/media-bindings/{binding_id}/locate", params={"at": utc(5)}).json()
        self.assertEqual((backward["media_ms"], backward["binding_status"], backward["segment"]["phase_id"]), (5000, "unbound", None))
        edge = self.client.get(f"/api/media-bindings/{binding_id}/locate", params={"media_ms": 40000}).json()
        self.assertEqual(edge["segment"]["phase_id"], "DETAIL", "边界毫秒归后一段")
        out = self.client.get(f"/api/media-bindings/{binding_id}/locate", params={"media_ms": 90001})
        self.assertEqual((422, "OUT_OF_RANGE"), (out.status_code, self.code(out)))
        before = self.client.get(f"/api/media-bindings/{binding_id}/locate", params={"at": utc(-1)})
        self.assertEqual((422, "OUT_OF_RANGE"), (before.status_code, self.code(before)))
        both = self.client.get(f"/api/media-bindings/{binding_id}/locate", params={"media_ms": 1, "at": utc(1)})
        self.assertEqual((422, "INVALID_QUERY"), (both.status_code, self.code(both)))
        self.assertEqual(404, self.client.get("/api/media-bindings/nope/locate", params={"media_ms": 1}).status_code)
        self.assertIsNone(segment_at([], 5))

    # ---- AC-5 ----
    def test_ac5_unbound_recording_context_lists_missing(self):
        context = self.client.get(f"/api/media-bindings/recordings/{self.recording_id}")
        self.assertEqual(200, context.status_code, context.text)
        value = context.json()
        self.assertEqual((value["binding_status"], value["binding"]), ("unbound", None))
        self.assertIn("场次绑定（项目 / 评标室 / 场次）", value["missing"])
        self.assertEqual(value["recording_status"], "ready")
        self.assertEqual(404, self.client.get(f"/api/media-bindings/recordings/{uuid4()}").status_code)
        # 绑定但场次还没有快照 → 仍 unbound 并说缺阶段快照
        self.post(self.body())
        value = self.client.get(f"/api/media-bindings/recordings/{self.recording_id}").json()
        self.assertEqual(value["binding_status"], "unbound")
        self.assertTrue(any("阶段快照" in m for m in value["missing"]))
        self.assertEqual(value["binding"]["timeline"][0]["binding_status"], "unbound")

    def test_ac5_bound_recording_context(self):
        self.add_snapshots(("DETAIL", 1, -60))
        self.post(self.body())
        value = self.client.get(f"/api/media-bindings/recordings/{self.recording_id}").json()
        self.assertEqual((value["binding_status"], value["missing"]), ("bound", []))
        self.assertEqual(value["binding"]["timeline"][0]["stage_snapshot_id"], f"snap-1-{self.session_id[:8]}")

    # ---- AC-6 ----
    def test_ac6_error_codes(self):
        self.add_snapshots(("DETAIL", 1, -60))
        unknown_session = self.client.post(f"/api/sessions/{uuid4()}/media-bindings", json=self.body())
        self.assertEqual((404, "SESSION_NOT_FOUND"), (unknown_session.status_code, self.code(unknown_session)))
        unknown_recording = self.post(self.body(subject_id=str(uuid4())))
        self.assertEqual((404, "RECORDING_NOT_FOUND"), (unknown_recording.status_code, self.code(unknown_recording)))
        unknown_asset = self.post(self.body(kind="scoring", subject_id="score-99", asset_id="score-99"))
        self.assertEqual((404, "ASSET_NOT_FOUND"), (unknown_asset.status_code, self.code(unknown_asset)))
        mismatch = self.post(self.body(kind="scoring", subject_id="talk-01", asset_id="talk-01"))
        self.assertEqual((422, "ASSET_TYPE_MISMATCH"), (mismatch.status_code, self.code(mismatch)))
        no_asset = self.post(self.body(kind="scoring", subject_id="score-01"))
        self.assertEqual((422, "INVALID_BINDING"), (no_asset.status_code, self.code(no_asset)))
        scoring = self.post(self.body(kind="scoring", subject_id="score-01", asset_id="score-01"))
        self.assertEqual(201, scoring.status_code, scoring.text)
        self.assertEqual(scoring.json()["timeline"][0]["to_ms"], None, "评分素材没有时长：开放段")
        audio_with_asset = self.post(self.body(asset_id="talk-01", duration_ms=None))
        self.assertEqual(201, audio_with_asset.status_code, audio_with_asset.text)
        bad_time = self.post(self.body(started_at="2026-10-19 02:00:00"))
        self.assertEqual(422, bad_time.status_code)
        bad_kind = self.post(self.body(kind="video"))
        self.assertEqual(422, bad_kind.status_code)
        extra = self.post(self.body(object_key="x"))
        self.assertEqual(422, extra.status_code)
        with self.Session.begin() as db:
            db.scalar(select(EvalSession).where(EvalSession.session_id == self.session_id)).status = "closed"
        closed = self.post(self.body(subject_id=str(uuid4())))
        self.assertEqual((409, "SESSION_CLOSED"), (closed.status_code, self.code(closed)))

    def test_ac6_binding_required_when_no_phase_binding(self):
        other = str(uuid4())
        with self.Session.begin() as db:
            db.add(EvalSession(session_id=other, name="未绑定场次", status="running", current_phase="review"))
        response = self.client.post(f"/api/sessions/{other}/media-bindings", json=self.body())
        self.assertEqual((409, "BINDING_REQUIRED"), (response.status_code, self.code(response)))

    # ---- AC-7 ----
    def test_ac7_auth_and_csrf(self):
        anonymous = TestClient(self.app)
        self.assertEqual(401, anonymous.post(self.url, json=self.body()).status_code)
        self.assertEqual(401, anonymous.get(self.url).status_code)
        no_csrf = TestClient(self.app)
        no_csrf.cookies.set("eval_admin_session", self.client.cookies.get("eval_admin_session"))
        self.assertEqual(403, no_csrf.post(self.url, json=self.body()).status_code)

    def test_model_constraints(self):
        from sqlalchemy.exc import IntegrityError
        with self.assertRaises(IntegrityError):
            with self.Session.begin() as db:
                db.add(MediaBinding(session_id=self.session_id, project_id=self.project, room_id=self.room, source_id=self.source, kind="scoring",
                                    subject_id="x", asset_id=None, started_at=T0, timeline=[], request_id="r1", request_digest="d", created_by="t"))
        with self.assertRaises(IntegrityError):
            with self.Session.begin() as db:
                db.add(MediaBinding(session_id=self.session_id, project_id=self.project, room_id=self.room, source_id=self.source, kind="other",
                                    subject_id="x", started_at=T0, timeline=[], request_id="r2", request_digest="d", created_by="t"))

    # ---- 2026-10-19 独立复核回归 ----
    def test_r7_microsecond_effective_at_agrees_with_get_snapshot(self):
        self.add_snapshots(("PRELIM", 1, 10.0005), ("DETAIL", 2, 40.9999))
        value = self.post(self.body()).json()
        self.assertEqual([(s["from_ms"], s["to_ms"]) for s in value["timeline"]], [(0, 10001), (10001, 41000), (41000, 90000)])
        with self.Session() as db:
            for segment in value["timeline"]:
                for ms in (segment["from_ms"], segment["to_ms"] - 1):
                    expected = get_snapshot(self.session_id, T0 + timedelta(milliseconds=ms), db=db)
                    self.assertEqual(expected["stage_snapshot_id"] if expected else None, segment["stage_snapshot_id"], ms)
        binding_id = value["binding_id"]
        edge = self.client.get(f"/api/media-bindings/{binding_id}/locate", params={"at": utc(10.0004)}).json()
        self.assertEqual((edge["media_ms"], edge["binding_status"]), (10000, "unbound"))

    def test_r_equivalent_started_at_strings_share_digest_and_duration_must_be_positive(self):
        self.add_snapshots(("DETAIL", 1, -60))
        first = self.post(self.body(started_at="2026-10-19T02:00:00Z"))
        again = self.post(self.body(started_at="2026-10-19T02:00:00.000Z"))
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        zero = self.post(self.body(subject_id=str(uuid4()), duration_ms=0))
        self.assertEqual(422, zero.status_code)

    def test_locate_service_rejects_bad_args(self):
        row = MediaBinding(binding_id="b", session_id=self.session_id, project_id=self.project, room_id=self.room, source_id=self.source,
                           kind="recording", subject_id="r", started_at=T0, duration_ms=1000, timeline=[], request_id="x", request_digest="d", created_by="t")
        with self.assertRaises(MediaBindingError):
            locate(row)
        with self.assertRaises(MediaBindingError):
            locate(row, media_ms=True)
        self.assertEqual(locate(row, media_ms=1000)["occurred_at"], utc(1))


if __name__ == "__main__":
    unittest.main()
