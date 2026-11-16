"""执行卡 9.4：离线质检 worker。桩验证：假帧源（随机噪声帧）+ 假检测器 + SQLite；规则是实时同一份模块（5.1 / 5.2 / 5.3 / 2.3）。
设 OFFLINE_TEST_DATABASE_URL 走真实 PostgreSQL（另一台机器）。
运行（services/offline-worker 下）：PYTHONPATH=. python -m unittest discover -s tests -p test_offline_worker_v2.py -v
"""
import copy
import hashlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from sqlalchemy import create_engine, select, update

from offline.jobs import SqlAlchemyJobRepository, job_table
from offline.pipeline import ModelUnavailable, OfflinePipeline, component_manifest, ensure_rule_paths
from offline.runner import OfflineRunner

T0 = datetime(2026, 10, 19, 2, 0, 0, tzinfo=timezone.utc)
SCOPE = {"project_id": "project-a", "room_id": "room-a", "session_id": "session-a"}
SOURCE = "camera-a"
SEAT_BOX_PX = [24, 40, 56, 100]          # 160x120 帧里落在工位多边形内的人框
ZONES = {"config_version": 3, "entry_lines": [],
         "zones": [{"zone_id": "seat-a01", "name": "A01", "kind": "seat", "seat_id": "seat-1",
                    "polygon": [[0.10, 0.25], [0.40, 0.25], [0.40, 0.90], [0.10, 0.90]]}]}


def snapshot(snapshot_id="snap-1", phase="DETAILED", version=1, scenes=("S03",)):
    return {"contract_version": "2.0.0", **SCOPE, "source_id": SOURCE, "stage_snapshot_id": snapshot_id, "phase_id": phase,
            "phase_version": version, "effective_at": "2026-10-19T01:00:00Z", "enabled_scenes": list(scenes), "always_on_scenes": ["S10"],
            "assets": [], "rule_versions": {"S03": "s03-v1", "S10": "s10-v1"}, "exceptions": []}


class FakeSource:
    """seconds 秒素材，每 1000/fps 毫秒一帧随机噪声（健康监测看到的是正常画面）。"""

    def __init__(self, seconds=30, *, on_frame=None):
        self.seconds, self.on_frame = seconds, on_frame
        self.rng = np.random.default_rng(7)

    def frames(self, sample_fps):
        step = int(1000 / sample_fps)
        for ms in range(0, self.seconds * 1000 + 1, step):
            if self.on_frame:
                self.on_frame(ms)
            yield ms, self.rng.integers(0, 255, size=(120, 160, 3), dtype=np.uint8)


class FakeDetector:
    """前 leave_ms 毫秒工位上有人，之后离席。"""

    def __init__(self, leave_ms=5000, *, broken=False):
        self.leave_ms, self.broken, self.ms = leave_ms, broken, 0

    def identity(self):
        return {"name": "fake-person", "weights_sha256": "f" * 64, "conf": 0.25, "imgsz": 160}

    def detect(self, frame):
        if self.broken:
            raise RuntimeError("CUDA error")
        present = self.ms < self.leave_ms
        self.ms += 500
        return [{"class": "person", "conf": 0.9, "bbox": list(SEAT_BOX_PX)}] if present else []


class OfflineWorkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_rule_paths()
        url = os.environ.get("OFFLINE_TEST_DATABASE_URL")
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.engine = create_engine(url) if url else create_engine(f"sqlite:///{Path(cls.tmp.name) / 'offline.db'}")
        cls.table = job_table()
        cls.table.metadata.create_all(cls.engine)
        cls.assets = Path(cls.tmp.name) / "assets"
        (cls.assets / "authorized").mkdir(parents=True)
        cls.video = cls.assets / "authorized" / "room-a.mp4"
        cls.video.write_bytes(b"fake video bytes for digest only")
        cls.sha = hashlib.sha256(cls.video.read_bytes()).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        cls.tmp.cleanup()

    def setUp(self):
        with self.engine.begin() as connection:
            connection.execute(self.table.delete())
        self.repo = SqlAlchemyJobRepository(self.engine, lease_seconds=60)

    # ---- 夹具 ----
    def frozen(self, **overrides):
        base = {"media_started_at": "2026-10-19T02:00:00.000Z", "duration_ms": 30000,
                "asset": {"asset_id": "video-01", "relative_path": "authorized/room-a.mp4", "sha256": self.sha, "duration_ms": 30000},
                "segments": [{"from_ms": 0, "to_ms": 30000, "binding_status": "bound", "snapshot": snapshot()}],
                "zone_config": copy.deepcopy(ZONES), "components": None, "sample_fps": 2.0, "rerun_of": None}
        base.update(overrides)
        return base

    def add_job(self, job_id="job-1", *, status="queued", created=0, **frozen_overrides):
        now = T0 + timedelta(seconds=created)
        with self.engine.begin() as connection:
            connection.execute(self.table.insert().values(
                job_id=job_id, request_id=f"req-{job_id}", request_digest="d" * 64, **SCOPE, source_id=SOURCE, binding_id="binding-1",
                asset_id="video-01", status=status, cancel_requested=False, frozen=self.frozen(**frozen_overrides), progress_ms=0, attempts=0,
                max_attempts=3, claim_id=None, claimed_by=None, claim_at=None, error=None, result=None, created_by="admin",
                created_at=now, updated_at=now, finished_at=None, contract_version="2.0.0"))
        return job_id

    def row(self, job_id="job-1"):
        with self.engine.begin() as connection:
            return dict(connection.execute(select(self.table).where(self.table.c.job_id == job_id)).mappings().one())

    def runner(self, *, detector=None, source=None, factory=None):
        detector = detector or FakeDetector()
        return OfflineRunner(self.repo, worker_id="offline-test", assets_root=self.assets,
                             detector_factory=factory or (lambda: detector), source_factory=lambda path: source or FakeSource(),
                             pipeline=OfflinePipeline(progress_every_ms=2000))

    # ---- AC-6 认领与租约 ----
    def test_ac6_claim_lease_stale_claim_and_exhaustion(self):
        self.add_job("job-1")
        first = self.repo.claim_next("w1")
        self.assertEqual((first["job_id"], first["status"], first["attempts"]), ("job-1", "running", 1))
        self.assertIsNone(self.repo.claim_next("w2"), "租约内不能被别人认领")
        with self.engine.begin() as connection:     # 模拟 worker 崩溃：租约过期
            connection.execute(update(self.table).values(claim_at=datetime.now(timezone.utc) - timedelta(minutes=5)))
        second = self.repo.claim_next("w2")
        self.assertEqual((second["attempts"], second["claimed_by"]), (2, "w2"))
        self.assertFalse(self.repo.complete("job-1", first["claim_id"], {"clues": []}, 0), "旧认领不能写结果")
        self.assertEqual(self.repo.heartbeat("job-1", first["claim_id"], 1000), "lost")
        self.assertEqual(self.repo.heartbeat("job-1", second["claim_id"], 1000), "ok")
        with self.engine.begin() as connection:
            connection.execute(update(self.table).values(attempts=3, claim_at=datetime.now(timezone.utc) - timedelta(minutes=5)))
        self.assertIsNone(self.repo.claim_next("w3"))
        row = self.row()
        self.assertEqual((row["status"], row["error"]["code"], row["claim_id"]), ("failed", "ATTEMPTS_EXHAUSTED", None))

    # ---- AC-7 / AC-11 成功运行 ----
    def test_ac7_ac11_run_reuses_realtime_modules_and_stays_offline(self):
        self.add_job()
        self.assertTrue(self.runner().run_once())
        row = self.row()
        self.assertEqual(row["status"], "succeeded", row["error"])
        result = row["result"]
        self.assertEqual(result["asset_check"]["status"], "match")
        self.assertEqual(result["covered_scenes"], ["S10", "S03"])
        self.assertEqual(result["stats"]["frames_processed"], 60)
        self.assertEqual(result["stats"]["media_ms_processed"], 29500, "段是左闭右开：素材末尾那一刻不处理")
        self.assertEqual(result["scene_failures"], {})
        s03 = [c for c in result["clues"] if c["scene_id"] == "S03"]
        self.assertIn("suspected", {c["conclusion"] for c in s03}, "离席超过 10 秒，S03 规则出 suspected")
        for clue in result["clues"]:
            self.assertTrue(clue["media_run_id"].startswith("offline:job-1:"), "离线媒体轮次与实时不冲突")
            self.assertTrue(clue["clue_id"].startswith("clue:"))
        self.assertIsNotNone(row["frozen"]["components"], "第一次运行写入版本清单")
        self.assertEqual(row["progress_ms"], 29500)
        here = Path(__file__).resolve().parents[1] / "offline"
        source = "".join(p.read_text(encoding="utf-8") for p in here.glob("*.py"))
        for forbidden in ("event_adapter", "persist_clue", "xadd", "Event(", "risk_clues"):
            self.assertNotIn(forbidden, source, f"离线不得写实时链路：{forbidden}")

    # ---- AC-8 与实时路径同结果 ----
    def test_ac8_same_clues_as_realtime_path(self):
        self.add_job()
        self.runner().run_once()
        offline = self.row()["result"]["clues"]
        from service.stage_dispatcher import dispatch
        from worker.absence_rules import AbsenceRules
        from worker.stage_health import FrameContext, StageHealthMonitor
        from worker.zone_observer import ZoneObserver, normalize_detections
        health, observer, handlers, detector = StageHealthMonitor(), ZoneObserver(copy.deepcopy(ZONES)), {"S03": AbsenceRules()}, FakeDetector()
        realtime = []
        for ms, frame in FakeSource().frames(2.0):     # 实时入口的写法：逐帧构造上下文、调同一组模块
            ctx = FrameContext(source_id=SOURCE, occurred_at=(T0 + timedelta(milliseconds=ms)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                               media_run_id="run-realtime", start_ms=ms, frame=frame, asset_id="video-01", **SCOPE,
                               stage_snapshot_id="snap-1", phase_id="DETAILED", phase_version=1)
            dets = normalize_detections(detector.detect(frame), 160, 120)
            observations = health.update(ctx, dets)
            status = health.status()
            flags = (["camera_offline"] if status["stream"] != "online" else []) + (["camera_degraded"] if status["quality"] == "degraded" else [])
            observations += observer.update(ctx, dets, quality_flags=flags)
            for obs in observations:
                realtime += dispatch(obs, snapshot(), handlers)

        def shape(clues):
            return sorted((c["scene_id"], c["rule_id"], c["conclusion"], c["risk_level"], c["start_ms"], c["end_ms"], c["seat_id"]) for c in clues)
        self.assertEqual(shape(offline), shape(realtime))
        self.assertTrue(offline)

    # ---- AC-9 版本冻结 ----
    def test_ac9_version_pinned_then_mismatch_fails_without_running(self):
        self.add_job()
        self.runner().run_once()
        pinned = self.row()["frozen"]["components"]
        self.assertEqual(pinned, component_manifest(FakeDetector(), 2.0))
        changed = copy.deepcopy(pinned)
        changed["absence_rules"]["sha256"] = "0" * 64
        self.add_job("job-2", created=1, components=changed)
        frames = []
        self.runner(source=FakeSource(on_frame=frames.append)).run_once()
        row = self.row("job-2")
        self.assertEqual((row["status"], row["error"]["code"], row["error"]["retryable"]), ("failed", "VERSION_MISMATCH", False))
        self.assertIn("absence_rules", row["error"]["message"])
        self.assertEqual(frames, [], "版本不符不运行")
        other_detector = FakeDetector()
        other_detector.identity = lambda: {"name": "fake-person", "weights_sha256": "e" * 64, "conf": 0.25, "imgsz": 160}
        self.add_job("job-3", created=2, components=pinned)
        self.runner(detector=other_detector).run_once()
        self.assertEqual(self.row("job-3")["error"]["code"], "VERSION_MISMATCH", "换了权重也算版本变化")

    # ---- AC-10 模型不可用 ----
    def test_ac10_model_unavailable_is_failed_without_normal(self):
        self.add_job()
        def missing():
            raise ModelUnavailable("检测权重文件不存在")
        self.runner(factory=missing).run_once()
        row = self.row()
        self.assertEqual((row["status"], row["error"]["code"], row["error"]["retryable"]), ("failed", "MODEL_UNAVAILABLE", True))
        self.assertNotIn("clues", row["result"] or {})
        self.add_job("job-2", created=1)
        self.runner(detector=FakeDetector(broken=True)).run_once()
        row = self.row("job-2")
        self.assertEqual(row["error"]["code"], "MODEL_UNAVAILABLE", "推理抛错不能当成「画面里没人」")
        self.assertNotIn("clues", row["result"] or {})

    def test_asset_checks_and_no_frames(self):
        self.add_job("job-1", asset={"asset_id": "video-01", "relative_path": "authorized/room-a.mp4", "sha256": "a" * 64, "duration_ms": 30000})
        self.runner().run_once()
        row = self.row("job-1")
        self.assertEqual((row["error"]["code"], row["error"]["retryable"], row["result"]["asset_check"]["status"]), ("ASSET_SHA256_MISMATCH", False, "mismatch"))
        self.add_job("job-2", created=1, asset={"asset_id": "video-01", "relative_path": "authorized/missing.mp4", "sha256": self.sha, "duration_ms": 1})
        self.runner().run_once()
        self.assertEqual(self.row("job-2")["error"]["code"], "ASSET_NOT_FOUND")
        self.add_job("job-3", created=2, asset={"asset_id": "video-01", "relative_path": "../outside.mp4", "sha256": self.sha, "duration_ms": 1})
        self.runner().run_once()
        self.assertEqual(self.row("job-3")["error"]["code"], "ASSET_NOT_FOUND", "素材根外的路径不读")
        self.add_job("job-4", created=3)
        self.runner(source=FakeSource(seconds=-1)).run_once()
        self.assertEqual((self.row("job-4")["error"]["code"], self.row("job-4")["error"]["retryable"]), ("NO_FRAMES", False))

    # ---- AC-4 运行中取消 ----
    def test_ac4_cancel_while_running(self):
        self.add_job()

        def request_cancel(ms):
            if ms == 3000:
                with self.engine.begin() as connection:
                    connection.execute(update(self.table).values(cancel_requested=True))
        self.runner(source=FakeSource(on_frame=request_cancel)).run_once()
        row = self.row()
        self.assertEqual((row["status"], row["claim_id"], row["result"]), ("cancelled", None, None))
        self.assertLess(row["progress_ms"], 30000)

    # ---- 独立复核回归 ----
    def test_review_h1_truncated_media_is_failed_not_clean(self):
        self.add_job()
        self.runner(source=FakeSource(seconds=5)).run_once()
        row = self.row()
        self.assertEqual((row["status"], row["error"]["code"], row["error"]["retryable"]), ("failed", "INCOMPLETE_MEDIA", False))
        self.assertEqual(row["result"]["stats"]["media_ms_processed"], 5000)
        self.assertNotIn("clues", row["result"], "没读完的素材不出线索结果")

    def test_review_h2_scene_with_handler_failures_is_not_covered(self):
        broken = snapshot()
        broken["rule_versions"] = {"S10": "s10-v1"}             # 启用了 S03 却没给规则版本：S03 每条观察都处理失败
        self.add_job(segments=[{"from_ms": 0, "to_ms": 30000, "binding_status": "bound", "snapshot": broken}])
        self.runner().run_once()
        result = self.row()["result"]
        self.assertEqual(self.row()["status"], "succeeded")
        self.assertNotIn("S03", result["covered_scenes"])
        self.assertGreater(result["scene_failures"]["S03"], 0)

    def test_review_h3_frozen_facts_reach_rules(self):
        leave = {"fact_id": "leave-1", "fact_version": 1, "kind": "leave_approved", "scope": {"seat_id": "seat-1"},
                 "valid_from": "2026-10-19T01:00:00Z", "valid_until": "2026-10-19T03:00:00Z"}
        approved = snapshot()
        approved["exceptions"] = [{"fact_id": "leave-1", "fact_version": 1}]
        segments = [{"from_ms": 0, "to_ms": 30000, "binding_status": "bound", "snapshot": approved}]
        self.add_job("job-1", segments=segments, facts={"authorizations": [leave], "seat_identities": {}, "identities": {}, "track_seats": {}})
        self.add_job("job-2", created=1, segments=segments, facts=None)
        self.runner().run_once()
        self.runner().run_once()
        with_facts = {c["conclusion"] for c in self.row("job-1")["result"]["clues"] if c["scene_id"] == "S03"}
        without = {c["conclusion"] for c in self.row("job-2")["result"]["clues"] if c["scene_id"] == "S03"}
        self.assertIn("normal", with_facts, "冻结的离岗许可传到规则：合法离岗判 normal")
        self.assertNotIn("suspected", with_facts)
        self.assertIn("insufficient", without, "例外引用了但内容没冻结：按事实缺失出 insufficient")

    def test_review_m2_unexpected_loader_error_fails_job_without_crashing(self):
        self.add_job()
        def explode():
            raise RuntimeError("CUDA driver mismatch")
        self.assertTrue(self.runner(factory=explode).run_once(), "worker 不抛出、不退出")
        row = self.row()
        self.assertEqual((row["status"], row["error"]["code"], row["error"]["retryable"], row["claim_id"]), ("failed", "OFFLINE_WORKER_ERROR", True, None))

    def test_review_m3_cancel_requested_just_before_completion_wins(self):
        self.add_job()

        def late_cancel(ms):
            if ms == 29500:                                      # 最后一帧之后不再有心跳
                with self.engine.begin() as connection:
                    connection.execute(update(self.table).values(cancel_requested=True))
        self.runner(source=FakeSource(on_frame=late_cancel)).run_once()
        row = self.row()
        self.assertEqual((row["status"], row["result"]), ("cancelled", None), "取消优先，不写成功结果")

    def test_unbound_segment_and_no_zone_config(self):
        self.add_job(segments=[{"from_ms": 0, "to_ms": 30000, "binding_status": "unbound", "snapshot": None}], zone_config=None)
        self.runner().run_once()
        row = self.row()
        self.assertEqual(row["status"], "succeeded", row["error"])
        self.assertEqual(row["result"]["covered_scenes"], ["S10"], "没有区域配置时 S03 不算覆盖")
        self.assertTrue(all(c["binding_status"] == "unbound" for c in row["result"]["clues"]))


if __name__ == "__main__":
    unittest.main()
