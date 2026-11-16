"""执行卡 9.4：离线质检任务接口与报告。

同一份测试双后端：设 OFFLINE_QA_TEST_DATABASE_URL 走真实 PostgreSQL 随机 schema（另一台机器），否则 SQLite 临时文件
（本机桩验证：B 模型的 JSONB 换成 JSON、PostgreSQL 专用 CHECK 去掉，只在测试进程内）。
运行（services/api 下）：PYTHONPATH=. python -m unittest discover -s tests -p test_offline_qa_v2.py -v
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
from sqlalchemy import JSON, CheckConstraint, MetaData, create_engine, event, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from app.asset_models import StageAsset
from app.auth import AdminAuthMiddleware
from app.auth_models import AdminAccount, AdminAudit, AdminSession
from app.business_models import BusinessParticipant, BusinessPerson, BusinessProject, BusinessRoom, BusinessSeat
from app.db import get_session
from app.stage_media_binding_models import StageMediaBinding as MediaBinding
from app.stage_media_bindings import build_timeline
from app.models import Base, Decision, EvalSession, Event
from app.offline_visual_models import OfflineQaJob
from app.phase_models import PhaseBinding, PhaseSnapshot
from app.report_builder import build_report, render_html
from app.routers.offline_visual_qa import router
from app.zone_config_models import SourceZoneConfig

T0 = datetime(2026, 10, 19, 2, 0, 0, tzinfo=timezone.utc)
SOURCE = "camera-offline-test"


def utc(seconds):
    return (T0 + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OfflineQaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = [BusinessProject.__table__, BusinessRoom.__table__, BusinessPerson.__table__, BusinessParticipant.__table__,
                      BusinessSeat.__table__, EvalSession.__table__, PhaseBinding.__table__, PhaseSnapshot.__table__,
                      AdminAccount.__table__, AdminAudit.__table__, AdminSession.__table__, StageAsset.__table__, MediaBinding.__table__,
                      SourceZoneConfig.__table__, Event.__table__, Decision.__table__, OfflineQaJob.__table__]
        url = os.environ.get("OFFLINE_QA_TEST_DATABASE_URL")
        cls.tmpdb = None
        cls.engine = None
        try:
            if url:
                cls.backend = "postgresql"
                cls.admin = create_engine(url)
                cls.schema = "test_offline_qa_v2_" + uuid4().hex
                with cls.admin.begin() as connection:
                    connection.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
                cls.engine = create_engine(url, connect_args={"options": f"-csearch_path={cls.schema} -cstatement_timeout=15000"})
                Base.metadata.create_all(cls.engine, tables=cls.tables)
            else:
                cls.backend = "sqlite"
                cls.tmpdb = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
                cls.engine = create_engine(f"sqlite:///{Path(cls.tmpdb.name) / 'offline_qa.db'}")

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
                db.add(AdminAccount(account_id=1, username="offline_admin", display_name="测试管理员", password_hash="not-a-real-password"))
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
        token, self.csrf = secrets.token_urlsafe(32), secrets.token_hex(32)
        self.binding_id, self.audio_binding_id = str(uuid4()), str(uuid4())
        with self.Session.begin() as db:
            db.add(BusinessProject(project_id=self.project, code=self.project, name="测试项目", owner="测试", status="running"))
            db.flush()
            db.add(BusinessRoom(room_id=self.room, project_id=self.project, code=self.room, name="测试评标室", location="测试", enabled=True))
            db.add(EvalSession(session_id=self.session_id, name="离线测试场次", status="closed", current_phase="offline"))
            db.flush()
            db.add(PhaseBinding(session_id=self.session_id, project_id=self.project, room_id=self.room, source_id=SOURCE, active=True))
            db.flush()
            snaps = []
            for version, (phase, at, scenes) in enumerate([("PRELIM", T0 - timedelta(minutes=5), ["S03"]),
                                                           ("DETAIL", T0 + timedelta(seconds=20), ["S03", "S05"])], start=1):
                snap = PhaseSnapshot(session_id=self.session_id, project_id=self.project, room_id=self.room, source_id=SOURCE, phase_id=phase,
                                     phase_version=version, effective_at=at, enabled_scenes=scenes, always_on_scenes=["S10"], assets=[],
                                     rule_versions={"S03": "s03-v1", "S05": "s05-v1", "S10": "s10-v1"}, exceptions=[], sync_status="applied",
                                     actor_id="offline_admin", reason="测试")
                db.add(snap)
                db.flush()
                snaps.append({"stage_snapshot_id": snap.stage_snapshot_id, "phase_id": phase, "phase_version": version, "effective_at": at})
            self.snapshot_ids = [s["stage_snapshot_id"] for s in snaps]
            db.add(StageAsset(session_id=self.session_id, asset_id="video-01", asset_type="video", relative_path="authorized/room-a.mp4",
                              sha256="c" * 64, duration_ms=60000, authorization_source="auth-1", visibility_notes="全景", size_bytes=10,
                              probe={"status": "unavailable"}, registered_by="offline_admin"))
            db.add(StageAsset(session_id=self.session_id, asset_id="talk-01", asset_type="audio", relative_path="authorized/talk.wav",
                              sha256="d" * 64, duration_ms=60000, authorization_source="auth-1", visibility_notes="录音", size_bytes=10,
                              probe={"status": "unavailable"}, registered_by="offline_admin"))
            for binding_id, subject, asset in ((self.binding_id, "run-1", "video-01"), (self.audio_binding_id, "run-audio", "talk-01")):
                db.add(MediaBinding(binding_id=binding_id, session_id=self.session_id, project_id=self.project, room_id=self.room, source_id=SOURCE,
                                    kind="media_run", subject_id=f"{subject}-{self.session_id}", asset_id=asset, started_at=T0, duration_ms=60000,
                                    timeline=build_timeline(snaps, T0, 60000), request_id=f"bind-{binding_id}", request_digest="e" * 64,
                                    created_by="offline_admin"))
            db.add(SourceZoneConfig(config_id=str(uuid4()), session_id=self.session_id, project_id=self.project, room_id=self.room, source_id=SOURCE,
                                    config_version=4, zones=[{"zone_id": "seat-a01", "name": "A01", "kind": "seat", "seat_id": "seat-1",
                                                              "polygon": [[0.1, 0.2], [0.4, 0.2], [0.4, 0.7], [0.1, 0.7]]}], entry_lines=[]))
            db.add(AdminSession(token_hash=hashlib.sha256(token.encode("ascii")).hexdigest(), account_id=1,
                                expires_at=datetime.now(timezone.utc) + timedelta(hours=1), csrf_token=self.csrf))
        self.client = TestClient(self.app, headers={"X-Requested-With": "XMLHttpRequest", "X-CSRF-Token": self.csrf})
        self.client.cookies.set("eval_admin_session", token)

    # ---- 夹具 ----
    def create(self, request_id=None, **body):
        payload = {"request_id": request_id or f"req-{uuid4().hex[:12]}", "binding_id": self.binding_id, **body}
        return self.client.post("/api/offline-qa/jobs", json=payload)

    def row(self, job_id):
        with self.Session() as db:
            return db.scalar(select(OfflineQaJob).where(OfflineQaJob.job_id == job_id))

    def set_row(self, job_id, **values):
        with self.Session.begin() as db:
            row = db.scalar(select(OfflineQaJob).where(OfflineQaJob.job_id == job_id))
            for key, value in values.items():
                setattr(row, key, value)

    def event_count(self):
        with self.Session() as db:
            return db.scalar(select(func.count()).select_from(Event)), db.scalar(select(func.count()).select_from(Decision))

    # ---- AC-1 / AC-2 ----
    def test_ac1_create_freezes_timeline_snapshots_zone_and_asset(self):
        response = self.create("req-ac1")
        self.assertEqual(response.status_code, 201, response.text)
        job = response.json()
        self.assertEqual((job["status"], job["source_id"], job["asset_id"], job["versions_pinned"]), ("queued", SOURCE, "video-01", False))
        frozen = self.row(job["job_id"]).frozen
        self.assertEqual([s["snapshot"]["stage_snapshot_id"] for s in frozen["segments"]], self.snapshot_ids)
        self.assertEqual([(s["from_ms"], s["to_ms"]) for s in frozen["segments"]], [(0, 20000), (20000, 60000)])
        self.assertEqual(frozen["segments"][1]["snapshot"]["enabled_scenes"], ["S03", "S05"])
        self.assertEqual(frozen["segments"][1]["snapshot"]["rule_versions"]["S03"], "s03-v1")
        self.assertEqual(frozen["zone_config"]["config_version"], 4)
        self.assertEqual(frozen["asset"], {"asset_id": "video-01", "relative_path": "authorized/room-a.mp4", "sha256": "c" * 64, "duration_ms": 60000})
        self.assertIsNone(frozen["components"])
        self.assertEqual((frozen["sample_fps"], frozen["media_started_at"]), (2.0, utc(0)))
        self.assertNotIn("frozen", job, "列表 / 详情不回传冻结清单与结果")

    def test_ac2_idempotency_validation_and_auth(self):
        first = self.create("req-same")
        again = self.create("req-same")
        self.assertEqual((again.status_code, again.json()["job_id"]), (200, first.json()["job_id"]))
        conflict = self.create("req-same", sample_fps=5)
        self.assertEqual((conflict.status_code, conflict.json()["detail"]["code"]), (409, "REQUEST_CONFLICT"))
        missing = self.client.post("/api/offline-qa/jobs", json={"request_id": "req-x", "binding_id": str(uuid4())})
        self.assertEqual((missing.status_code, missing.json()["detail"]["code"]), (404, "BINDING_NOT_FOUND"))
        audio = self.client.post("/api/offline-qa/jobs", json={"request_id": "req-audio", "binding_id": self.audio_binding_id})
        self.assertEqual((audio.status_code, audio.json()["detail"]["code"]), (422, "ASSET_REQUIRED"))
        self.assertEqual(self.create("req-bad", sample_fps=0).status_code, 422)
        self.assertEqual(self.create("req-extra", unknown=1).status_code, 422)
        anonymous = TestClient(self.app, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(anonymous.post("/api/offline-qa/jobs", json={"request_id": "r", "binding_id": self.binding_id}).status_code, 401)
        self.assertEqual(anonymous.get("/api/offline-qa/jobs").status_code, 401)
        listed = self.client.get("/api/offline-qa/jobs", params={"session_id": self.session_id}).json()["jobs"]
        self.assertEqual([j["job_id"] for j in listed], [first.json()["job_id"]])

    # ---- AC-3 ----
    def test_ac3_rerun_copies_frozen_manifest_not_current_snapshots(self):
        original = self.create("req-orig").json()["job_id"]
        components = {"absence_rules": {"sha256": "1" * 64, "model_version": None}}
        frozen = dict(self.row(original).frozen, components=components)
        self.set_row(original, frozen=frozen, status="succeeded")
        with self.Session.begin() as db:            # 场次后来追加了新快照：重跑不能读到它
            db.add(PhaseSnapshot(session_id=self.session_id, project_id=self.project, room_id=self.room, source_id=SOURCE, phase_id="REPORT",
                                 phase_version=3, effective_at=T0 + timedelta(seconds=30), enabled_scenes=["S03"], always_on_scenes=["S10"],
                                 assets=[], rule_versions={"S03": "s03-v2", "S10": "s10-v1"}, exceptions=[], sync_status="applied",
                                 actor_id="offline_admin", reason="测试"))
        rerun = self.create("req-rerun", rerun_of=original)
        self.assertEqual(rerun.status_code, 201, rerun.text)
        copied = self.row(rerun.json()["job_id"]).frozen
        self.assertEqual(copied["components"], components)
        self.assertEqual(copied["rerun_of"], original)
        self.assertEqual({k: v for k, v in copied.items() if k != "rerun_of"}, {k: v for k, v in frozen.items() if k != "rerun_of"})
        self.assertTrue(rerun.json()["versions_pinned"])
        other = self.client.post("/api/offline-qa/jobs", json={"request_id": "req-other", "binding_id": self.audio_binding_id, "rerun_of": original})
        self.assertEqual(other.status_code, 422)
        self.assertEqual(self.create("req-ghost", rerun_of=str(uuid4())).status_code, 404)

    # ---- AC-4 / AC-5 ----
    def test_ac4_cancel_queued_running_terminal(self):
        queued = self.create().json()["job_id"]
        response = self.client.post(f"/api/offline-qa/jobs/{queued}/cancel")
        self.assertEqual((response.status_code, response.json()["status"]), (200, "cancelled"))
        self.assertEqual(self.client.post(f"/api/offline-qa/jobs/{queued}/cancel").status_code, 409)
        running = self.create().json()["job_id"]
        self.set_row(running, status="running", claim_id=str(uuid4()), claimed_by="w1", claim_at=datetime.now(timezone.utc), attempts=1)
        body = self.client.post(f"/api/offline-qa/jobs/{running}/cancel").json()
        self.assertEqual((body["status"], body["cancel_requested"]), ("running", True), "运行中的由 worker 停下后置 cancelled")
        self.assertEqual(self.client.post(f"/api/offline-qa/jobs/{uuid4()}/cancel").status_code, 404)

    def test_ac5_retry_keeps_frozen_and_respects_retryable(self):
        job_id = self.create().json()["job_id"]
        frozen_before = self.row(job_id).frozen
        self.set_row(job_id, status="failed", attempts=3, error={"code": "MODEL_UNAVAILABLE", "retryable": True}, result={"asset_check": {}})
        retried = self.client.post(f"/api/offline-qa/jobs/{job_id}/retry")
        self.assertEqual(retried.status_code, 200, retried.text)
        row = self.row(job_id)
        self.assertEqual((row.status, row.error, row.result, row.attempts), ("queued", None, None, 0))
        self.assertEqual(row.frozen, frozen_before)
        self.assertEqual(self.client.post(f"/api/offline-qa/jobs/{job_id}/retry").status_code, 409, "queued 不能重试")
        self.set_row(job_id, status="failed", error={"code": "VERSION_MISMATCH", "retryable": False})
        blocked = self.client.post(f"/api/offline-qa/jobs/{job_id}/retry")
        self.assertEqual((blocked.status_code, blocked.json()["detail"]["code"]), (409, "JOB_NOT_RETRYABLE"))
        self.set_row(job_id, status="cancelled", error=None)
        self.assertEqual(self.client.post(f"/api/offline-qa/jobs/{job_id}/retry").json()["status"], "queued")

    # ---- AC-11 / AC-12 ----
    def _succeed(self, job_id, *, reason="工位 A01 空置 12000 ms"):
        clues = [
            {"clue_id": "clue:S03:a", "scene_id": "S03", "rule_id": "s03-seat-absence", "rule_version": "s03-v1", "conclusion": "suspected",
             "risk_level": "medium", "occurred_at": utc(12), "start_ms": 2000, "end_ms": 12000, "track_id": None, "seat_id": "seat-1",
             "person_id": None, "reason": reason, "missing_facts": [],
             "evidence_refs": [{"evidence_id": "e1", "status": "pending"}]},
            {"clue_id": "clue:S03:b", "scene_id": "S03", "rule_id": "s03-seat-absence", "rule_version": "s03-v1", "conclusion": "suspected",
             "risk_level": "medium", "occurred_at": utc(50), "start_ms": 40000, "end_ms": 50000, "track_id": None, "seat_id": "seat-1",
             "person_id": None, "reason": "离线独有", "missing_facts": [], "evidence_refs": []},
        ]
        result = {"covered_scenes": ["S10", "S03"], "clues": clues, "failures": [], "media_run_id": f"offline:{job_id}:1",
                  "stats": {"frames_processed": 120, "media_ms_processed": 60000},
                  "asset_check": {"status": "match", "expected_sha256": "c" * 64, "actual_sha256": "c" * 64}}
        frozen = dict(self.row(job_id).frozen, components={"absence_rules": {"sha256": "1" * 64}})
        self.set_row(job_id, status="succeeded", result=result, frozen=frozen, finished_at=datetime.now(timezone.utc))

    def test_ac12_report_json_and_printable_html(self):
        job_id = self.create().json()["job_id"]
        naive = lambda seconds: (T0 + timedelta(seconds=seconds)).replace(tzinfo=None)   # noqa: E731  events.occurred_at 是无时区 UTC
        with self.Session.begin() as db:
            db.add(Event(event_id="ev-1", session_id=self.session_id, camera_id=SOURCE, skill_code="s03-seat-absence", skill_version="s03-v1",
                         occurred_at=naive(15), status="CONFIRMED", risk_level="medium", reason="实时离席"))
            db.add(Event(event_id="ev-2", session_id=self.session_id, camera_id=SOURCE, skill_code="s01-device-deposit", skill_version="s01-v1",
                         occurred_at=naive(30), status="DISMISSED", risk_level="medium", reason="实时设备"))
            db.add(Event(event_id="ev-other", session_id=self.session_id, camera_id="camera-other", skill_code="s03-seat-absence",
                         skill_version="s03-v1", occurred_at=naive(15), status="PENDING", risk_level="medium", reason="别的源"))
            db.add(Decision(event_id="ev-1", action="escalate", reviewer_id="rev-1", note="先升级", decided_at=naive(100)))
            db.add(Decision(event_id="ev-1", action="confirm", reviewer_id="rev-2", note="确认离席", decided_at=naive(200)))
            db.add(Decision(event_id="ev-2", action="false_positive", reviewer_id="rev-1", note="水杯", decided_at=naive(150)))
        pending = self.client.get(f"/api/offline-qa/jobs/{job_id}/report").json()
        self.assertFalse(pending["conclusive"])
        self.assertEqual((pending["clues"], pending["realtime_comparison"]), ([], []))
        self.assertIn("queued", pending["limitations"][0])
        before = self.event_count()
        self._succeed(job_id, reason="<script>alert(1)</script> 空置")
        report = self.client.get(f"/api/offline-qa/jobs/{job_id}/report").json()
        self.assertTrue(report["conclusive"])
        self.assertEqual([s["phase_id"] for s in report["timeline"]], ["PRELIM", "DETAIL"])
        self.assertEqual(report["timeline"][1]["from_at"], utc(20))
        self.assertEqual(report["coverage"], {"enabled_scenes": ["S03", "S05", "S10"], "covered_scenes": ["S03", "S10"], "not_covered_scenes": ["S05"],
                                              "failed_scenes": [], "scene_failures": {}})
        self.assertIn("S05", report["limitations"][0])
        self.assertEqual(report["clue_summary"]["S03"], {"suspected": 2, "insufficient": 0, "normal": 0})
        rows = {r["event_id"]: r for r in report["realtime_comparison"]}
        self.assertEqual(set(rows), {"ev-1", "ev-2"}, "只对照本源本时段的实时事件")
        self.assertEqual((rows["ev-1"]["offline_reproduced"], rows["ev-1"]["decision"]["action"], rows["ev-1"]["decision"]["reviewer_id"]),
                         (True, "confirm", "rev-2"), "取最新的人工决定")
        self.assertEqual((rows["ev-2"]["offline_reproduced"], rows["ev-2"]["decision"]["action"]), (False, "false_positive"))
        self.assertEqual(report["offline_only_clue_ids"], ["clue:S03:b"])
        self.assertEqual(report["evidence_check"]["asset"]["status"], "match")
        self.assertEqual(report["evidence_check"]["evidence_ref_status"], {"pending": 1})
        self.assertEqual(report["versions"]["zone_config_version"], 4)
        page = self.client.get(f"/api/offline-qa/jobs/{job_id}/report.html")
        self.assertEqual(page.status_code, 200)
        self.assertIn("text/html", page.headers["content-type"])
        self.assertIn("@media print", page.text)
        self.assertNotIn("<script>alert(1)</script>", page.text)
        self.assertIn("&lt;script&gt;", page.text)
        for heading in ("阶段时间轴", "实时事件与人工决定对照", "证据校验", "限制", "未覆盖（不代表正常）"):
            self.assertIn(heading, page.text)
        self.assertEqual(self.event_count(), before, "报告与建任务都不写实时事件 / 决定")

    # ---- 独立复核回归 ----
    def test_review_m1_rerun_requires_pinned_original_and_same_fps(self):
        original = self.create("req-unpinned").json()["job_id"]
        unpinned = self.create("req-rerun-1", rerun_of=original)
        self.assertEqual((unpinned.status_code, unpinned.json()["detail"]["code"]), (409, "RERUN_NOT_PINNED"))
        self.set_row(original, frozen=dict(self.row(original).frozen, components={"absence_rules": {"sha256": "1" * 64}}))
        other_fps = self.create("req-rerun-2", rerun_of=original, sample_fps=5)
        self.assertEqual((other_fps.status_code, other_fps.json()["detail"]["code"]), (422, "RERUN_MISMATCH"))
        same_fps = self.create("req-rerun-3", rerun_of=original, sample_fps=2.0)
        self.assertEqual(same_fps.status_code, 201, same_fps.text)

    def test_review_m3_cancel_running_with_expired_lease(self):
        job_id = self.create().json()["job_id"]
        self.set_row(job_id, status="running", claim_id=str(uuid4()), claimed_by="dead-worker", attempts=1,
                     claim_at=datetime.now(timezone.utc) - timedelta(hours=1))
        body = self.client.post(f"/api/offline-qa/jobs/{job_id}/cancel").json()
        self.assertEqual((body["status"], body["cancel_requested"]), ("cancelled", False), "worker 已不在：直接取消，不卡在 running")
        row = self.row(job_id)
        self.assertEqual((row.claim_id, row.claimed_by), (None, None))
        self.assertEqual(self.client.post(f"/api/offline-qa/jobs/{job_id}/retry").json()["status"], "queued")

    def test_review_m4_l1_l6_h2_system_close_window_versions_and_scene_failures(self):
        job_id = self.create().json()["job_id"]
        naive = lambda seconds: (T0 + timedelta(seconds=seconds)).replace(tzinfo=None)   # noqa: E731
        with self.Session.begin() as db:
            db.add(Event(event_id="ev-a", session_id=self.session_id, camera_id=SOURCE, skill_code="s03-seat-absence", skill_version="s03-v0",
                         occurred_at=naive(15), status="CLOSED", risk_level="medium", reason="实时离席"))
            db.add(Event(event_id="ev-tail", session_id=self.session_id, camera_id=SOURCE, skill_code="s03-seat-absence", skill_version="s03-v1",
                         occurred_at=naive(65), status="PENDING", risk_level="medium", reason="素材结尾后 5 秒"))
            db.add(Decision(event_id="ev-a", action="auto_close", reviewer_id="system", note="设备恢复", decided_at=naive(90)))
        self._succeed(job_id)
        self.set_row(job_id, result=dict(self.row(job_id).result, scene_failures={"S03": 4}, covered_scenes=["S10"]))
        report = self.client.get(f"/api/offline-qa/jobs/{job_id}/report").json()
        rows = {r["event_id"]: r for r in report["realtime_comparison"]}
        self.assertIn("ev-tail", rows, "查询窗口两端放宽对照容差")
        self.assertIsNone(rows["ev-a"]["decision"], "系统自动关闭不是人工决定")
        self.assertEqual(rows["ev-a"]["system_close"]["action"], "auto_close")
        self.assertEqual((rows["ev-a"]["offline_rule_versions"], rows["ev-a"]["rule_version_same"]), (["s03-v1"], False))
        self.assertEqual((report["coverage"]["failed_scenes"], report["coverage"]["scene_failures"]), (["S03"], {"S03": 4}))
        self.assertNotIn("S03", report["coverage"]["not_covered_scenes"])
        self.assertIn("计数为 0 不代表干净", report["limitations"][0])
        self.assertIn("处理失败（结果不完整）", self.client.get(f"/api/offline-qa/jobs/{job_id}/report.html").text)

    def test_report_builder_failed_job_has_no_conclusion(self):
        job = {"job_id": "j", "status": "failed", "error": {"code": "MODEL_UNAVAILABLE"}, "frozen": {"segments": []},
               "result": {"clues": [{"clue_id": "x", "conclusion": "normal"}]}}
        report = build_report(job)
        self.assertEqual((report["conclusive"], report["clues"], report["clue_summary"]), (False, [], {}))
        self.assertIn("任务未成功", render_html(report))
        self.assertIn("failed", report["limitations"][0])


if __name__ == "__main__":
    unittest.main()
