"""执行卡 5.3：S10 视频设备健康与见证缺口（桩验证：构造帧序列，逻辑时钟，不读视频文件）。

运行（服务根目录）：PYTHONPATH=. python -m unittest discover -s tests -p test_stage_health_v2.py -v
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from worker.coverage_gap import Gap, GapRegistry, make_gap_id
from worker.stage_health import (
    FrameContext,
    StageHealthConfig,
    StageHealthMonitor,
    adapt_legacy_message,
)

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "contracts"))
from contract_validator import ContractValidator  # noqa: E402  只在测试里用 1.2 的校验器

BASE = datetime(2026, 10, 19, 1, 0, 0, tzinfo=timezone.utc)
STEP = 0.2  # 5 fps
SCOPE = {"project_id": "p1", "room_id": "r1", "session_id": "s1"}
SNAPSHOT = {"stage_snapshot_id": "snap-1", "phase_id": "DETAILED", "phase_version": 1}


def at(t):
    return (BASE + timedelta(seconds=t)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def epoch(t):
    return (BASE + timedelta(seconds=t)).timestamp()


def noise(seed=0):
    return np.random.default_rng(seed).integers(0, 256, (48, 64, 3), dtype=np.uint8)


def black():
    return np.zeros((48, 64, 3), dtype=np.uint8)


def gradient():
    row = np.linspace(0, 255, 64).astype(np.uint8)
    return np.repeat(np.tile(row, (48, 1))[:, :, None], 3, axis=2)


def shifted(frame, dx):
    return np.roll(frame, dx, axis=1)


def ctx(t, frame, run="run-1", bound=True, start_ms=None, snapshot=None, reference=None, roi=None):
    fields = dict(
        source_id="cam-1",
        occurred_at=at(t),
        media_run_id=run,
        start_ms=int(round(t * 1000)) if start_ms is None else start_ms,
        frame=frame,
        asset_id=None,
        reference_frame=reference,
        roi=roi,
    )
    if bound:
        fields.update(SCOPE)
        fields.update(snapshot or SNAPSHOT)
    return FrameContext(**fields)


def steps(t0, t1):
    n0, n1 = int(round(t0 / STEP)), int(round(t1 / STEP))
    return [round(i * STEP, 3) for i in range(n0, n1 + 1)]


def feed(monitor, t0, t1, frame_fn, **kw):
    out = []
    for t in steps(t0, t1):
        frame = frame_fn() if callable(frame_fn) else frame_fn
        out.extend(monitor.update(ctx(t, frame, **kw)))
    return out


def by_type(observations, obs_type):
    return [o for o in observations if o["type"] == obs_type]


class GapRegistryTest(unittest.TestCase):
    def test_open_is_idempotent_per_source_and_kind(self):
        reg = GapRegistry()
        gap = reg.open(source_id="cam-1", kind="offline", media_run_id="run-1", start_ms=2000,
                       occurred_at=at(5), scope=SCOPE, technical_reason="no_frame")
        again = reg.open(source_id="cam-1", kind="offline", media_run_id="run-1", start_ms=7000,
                         occurred_at=at(10), scope=SCOPE)
        other = reg.open(source_id="cam-1", kind="quality", media_run_id="run-1", start_ms=3000,
                         occurred_at=at(5), scope=SCOPE)
        self.assertIs(gap, again)
        self.assertEqual(gap.gap_id, make_gap_id("cam-1", "run-1", "offline", 2000))
        self.assertEqual(gap.gap_id, "gap:cam-1:run-1:offline:2000")
        self.assertNotEqual(gap.gap_id, other.gap_id)
        self.assertEqual(len(reg.open_gaps("cam-1")), 2)
        self.assertTrue(gap.is_open)

    def test_close_sets_end_and_is_idempotent(self):
        reg = GapRegistry()
        gap = reg.open(source_id="cam-1", kind="offline", media_run_id="run-1", start_ms=2000,
                       occurred_at=at(5), scope=SCOPE)
        closed = reg.close(gap.gap_id, media_run_id="run-2", end_ms=400, occurred_at=at(9))
        self.assertFalse(closed.is_open)
        self.assertEqual((closed.close_media_run_id, closed.close_end_ms, closed.closed_at), ("run-2", 400, at(9)))
        self.assertIsNone(reg.open_gap("cam-1", "offline"))
        self.assertIs(reg.close(gap.gap_id, media_run_id="run-3", end_ms=1, occurred_at=at(20)), closed)
        self.assertEqual(closed.close_media_run_id, "run-2")
        with self.assertRaises(KeyError):
            reg.close("gap:nobody", media_run_id="run-1", end_ms=0, occurred_at=at(1))

    def test_export_restore_roundtrip(self):
        reg = GapRegistry()
        reg.open(source_id="cam-1", kind="offline", media_run_id="run-1", start_ms=2000, occurred_at=at(5), scope=SCOPE)
        q = reg.open(source_id="cam-1", kind="quality", media_run_id="run-1", start_ms=3000, occurred_at=at(6), scope=SCOPE)
        reg.close(q.gap_id, media_run_id="run-1", end_ms=9000, occurred_at=at(9))
        state = json.loads(json.dumps(reg.export()))
        other = GapRegistry()
        other.restore(state)
        self.assertEqual([g.to_dict() for g in other.all()], [g.to_dict() for g in reg.all()])
        self.assertEqual(other.open_gap("cam-1", "offline").open_start_ms, 2000)
        self.assertIsNone(other.open_gap("cam-1", "quality"))
        self.assertIsInstance(other.find(q.gap_id), Gap)


class StageHealthOfflineTest(unittest.TestCase):
    def test_ac1_offline_after_three_seconds_opens_gap_once(self):
        m = StageHealthMonitor()
        obs = feed(m, 0, 2.0, noise)
        obs += feed(m, 2.2, 8.0, None)
        offline = by_type(obs, "video_offline")
        self.assertEqual(len(obs), 1)
        self.assertEqual(len(offline), 1)
        o = offline[0]
        self.assertEqual(o["occurred_at"], at(5.0))
        self.assertEqual((o["start_ms"], o["end_ms"]), (2000, 5000))
        self.assertEqual(o["payload"]["gap_id"], "gap:cam-1:run-1:offline:2000")
        self.assertEqual(o["payload"]["gap_media_run_id"], "run-1")
        self.assertEqual(o["payload"]["technical_reason"], "no_frame")
        gap = m.registry.open_gap("cam-1", "offline")
        self.assertEqual(gap.open_start_ms, 2000)
        self.assertEqual(o["evidence_refs"][0]["evidence_type"], "gap")
        self.assertEqual(o["evidence_refs"][0]["status"], "missing")
        self.assertIsNone(o["evidence_refs"][0]["sha256"])
        self.assertEqual(m.status()["stream"], "offline")

    def test_ac2_recovery_closes_same_gap_fifteen_second_fault(self):
        m = StageHealthMonitor()
        obs = feed(m, 0, 10.0, noise)
        obs += feed(m, 10.2, 24.8, None)
        obs += feed(m, 25.0, 30.0, noise)
        offline, online = by_type(obs, "video_offline"), by_type(obs, "video_online")
        self.assertEqual((len(offline), len(online)), (1, 1))
        self.assertEqual(offline[0]["occurred_at"], at(13.0))
        self.assertEqual(online[0]["occurred_at"], at(27.0))
        self.assertEqual(online[0]["payload"]["gap_id"], offline[0]["payload"]["gap_id"])
        self.assertEqual((online[0]["start_ms"], online[0]["end_ms"]), (25000, 27000))
        gap = m.registry.find(offline[0]["payload"]["gap_id"])
        self.assertFalse(gap.is_open)
        duration = gap.close_end_ms - gap.open_start_ms
        self.assertLessEqual(abs(duration - 15000), int(STEP * 1000))
        self.assertEqual(online[0]["payload"]["gap_duration_ms"], duration)
        self.assertEqual(m.status()["stream"], "online")
        self.assertEqual(m.status()["open_gaps"], [])

    def test_ac4_waiting_without_frames_reports_once_and_status(self):
        m = StageHealthMonitor()
        obs = feed(m, 0, 10.0, None)
        self.assertEqual(len(obs), 1)
        o = obs[0]
        self.assertEqual(o["type"], "video_offline")
        self.assertEqual(o["occurred_at"], at(3.0))
        self.assertEqual((o["start_ms"], o["end_ms"]), (0, 3000))
        self.assertEqual(o["payload"]["technical_reason"], "no_stream_at_start")
        self.assertEqual(o["payload"]["gap_id"], "gap:cam-1:run-1:offline:0")
        st = m.status()
        self.assertEqual(st["stream"], "waiting")
        self.assertAlmostEqual(st["waiting_seconds"], 10.0, places=2)
        self.assertEqual(st["open_gaps"], ["gap:cam-1:run-1:offline:0"])

    def test_ac5_gap_survives_reset_and_snapshot_change(self):
        m = StageHealthMonitor()
        obs = feed(m, 0, 2.0, noise)
        obs += feed(m, 2.2, 8.0, None)
        self.assertEqual(len(by_type(obs, "video_offline")), 1)
        m.reset("run-2")
        self.assertEqual(m.status()["open_gaps"], ["gap:cam-1:run-1:offline:2000"])
        snap2 = {"stage_snapshot_id": "snap-2", "phase_id": "SCORING", "phase_version": 2}
        later = []
        for i, t in enumerate(steps(8.2, 12.0)):
            later.extend(m.update(ctx(t, noise(), run="run-2", start_ms=i * 200, snapshot=snap2)))
        online = by_type(later, "video_online")
        self.assertEqual(len(online), 1)
        o = online[0]
        self.assertEqual(o["payload"]["gap_id"], "gap:cam-1:run-1:offline:2000")
        self.assertEqual(o["payload"]["gap_media_run_id"], "run-1")
        self.assertEqual((o["media_run_id"], o["stage_snapshot_id"], o["phase_id"]), ("run-2", "snap-2", "SCORING"))
        self.assertEqual((o["start_ms"], o["end_ms"]), (0, 2000))
        gap = m.registry.find("gap:cam-1:run-1:offline:2000")
        self.assertEqual((gap.is_open, gap.close_media_run_id, gap.close_end_ms), (False, "run-2", 0))
        self.assertEqual(o["evidence_refs"][0]["media_run_id"], "run-1")

    def test_ac6_unbound_context_keeps_gap_without_guessing_session(self):
        m = StageHealthMonitor()
        obs = feed(m, 0, 2.0, noise, bound=False)
        obs += feed(m, 2.2, 6.0, None, bound=False)
        o = by_type(obs, "video_offline")[0]
        self.assertEqual(o["binding_status"], "unbound")
        self.assertIsNone(o["session_id"])
        self.assertIsNone(o["stage_snapshot_id"])
        self.assertIsNone(o["phase_id"])
        self.assertIsNone(o["phase_version"])
        self.assertIn("binding_missing", o["quality_flags"])
        self.assertEqual(len(m.registry.open_gaps("cam-1")), 1)

    def test_frame_context_has_no_defaults_for_scope(self):
        with self.assertRaises(ValueError):
            FrameContext(source_id="", occurred_at=at(0), media_run_id="run-1", start_ms=0)
        with self.assertRaises(ValueError):
            FrameContext(source_id="cam-1", occurred_at=at(0), media_run_id="", start_ms=0)
        with self.assertRaises(ValueError):
            FrameContext(source_id="cam-1", occurred_at="2026-10-19 01:00:00", media_run_id="run-1", start_ms=0)
        with self.assertRaises(ValueError):
            FrameContext(source_id="cam-1", occurred_at=at(0), media_run_id="run-1", start_ms=-1)


class StageHealthQualityTest(unittest.TestCase):
    def test_ac3_black_confirmed_after_two_seconds_and_flicker_ignored(self):
        m = StageHealthMonitor()
        obs = feed(m, 0, 2.0, noise)
        obs += feed(m, 2.2, 3.0, black)      # 闪黑 1 秒
        obs += feed(m, 3.2, 4.0, noise)
        self.assertEqual(obs, [])
        obs += feed(m, 4.2, 8.0, black)      # 持续黑屏
        blacked = by_type(obs, "camera_blacked")
        self.assertEqual(len(obs), 1)
        self.assertEqual(len(blacked), 1)
        b = blacked[0]
        self.assertEqual(b["occurred_at"], at(6.2))
        self.assertEqual((b["start_ms"], b["end_ms"]), (4200, 6200))
        self.assertEqual(b["payload"]["technical_reason"], "blacked")
        self.assertLess(b["payload"]["variance"], 10)
        self.assertEqual(b["payload"]["gap_id"], "gap:cam-1:run-1:quality:4200")
        obs += feed(m, 8.2, 12.0, noise)
        normal = by_type(obs, "camera_normal")
        self.assertEqual(len(normal), 1)
        self.assertEqual(normal[0]["occurred_at"], at(10.2))
        self.assertEqual(normal[0]["payload"]["gap_id"], b["payload"]["gap_id"])
        gap = m.registry.find(b["payload"]["gap_id"])
        self.assertEqual((gap.is_open, gap.close_end_ms), (False, 8200))
        self.assertEqual(m.status()["quality"], "good")

    def test_blur_and_exposure_are_low_quality_reasons(self):
        m = StageHealthMonitor()
        obs = feed(m, 0, 1.0, noise)
        obs += feed(m, 1.2, 4.0, gradient)
        b = by_type(obs, "camera_blacked")
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0]["payload"]["technical_reason"], "blurred")
        self.assertGreater(b[0]["payload"]["variance"], 10)
        m2 = StageHealthMonitor()
        bright = np.full((48, 64, 3), 250, dtype=np.uint8)
        obs2 = feed(m2, 0, 1.0, noise)
        obs2 += feed(m2, 1.2, 4.0, bright)
        self.assertEqual(by_type(obs2, "camera_blacked")[0]["payload"]["technical_reason"], "bright")

    def test_ac7_offset_unconfigured_then_shift_detected(self):
        ref = noise(1)
        m = StageHealthMonitor()
        obs = feed(m, 0, 2.0, ref)
        obs += feed(m, 2.2, 6.0, shifted(ref, 30))
        self.assertEqual(obs, [])
        self.assertFalse(m.status()["offset"]["configured"])
        self.assertEqual(m.status()["offset"]["state"], "unconfigured")

        only_ref = StageHealthMonitor()          # 有参考图没 ROI 同样是「偏移待配置」（执行卡:247）
        obs1 = feed(only_ref, 0, 2.0, ref, reference=ref)
        obs1 += feed(only_ref, 2.2, 6.0, shifted(ref, 30), reference=ref)
        self.assertEqual(obs1, [])
        self.assertFalse(only_ref.status()["offset"]["configured"])
        self.assertEqual(only_ref.status()["offset"]["state"], "unconfigured")

        m2 = StageHealthMonitor()
        obs2 = feed(m2, 0, 2.0, ref, reference=ref, roi=(0.0, 0.0, 1.0, 1.0))
        self.assertTrue(m2.status()["offset"]["configured"])
        self.assertEqual(m2.status()["offset"]["state"], "aligned")
        obs2 += feed(m2, 2.2, 6.0, shifted(ref, 30), reference=ref, roi=(0.0, 0.0, 1.0, 1.0))
        b = by_type(obs2, "camera_blacked")
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0]["occurred_at"], at(4.2))
        self.assertEqual(b[0]["payload"]["technical_reason"], "shifted")
        self.assertGreater(b[0]["payload"]["shift_px"], 20)
        self.assertEqual(m2.status()["offset"]["state"], "shifted")


class ContractTest(unittest.TestCase):
    def collect(self):
        out = []
        m = StageHealthMonitor()
        out += feed(m, 0, 10.0, noise)
        out += feed(m, 10.2, 24.8, None)
        out += feed(m, 25.0, 30.0, noise)
        out += feed(m, 30.2, 34.0, black)
        out += feed(m, 34.2, 38.0, noise)
        u = StageHealthMonitor()
        out += feed(u, 0, 2.0, noise, bound=False)
        out += feed(u, 2.2, 6.0, None, bound=False)
        out += feed(u, 6.2, 10.0, noise, bound=False)
        ref = noise(1)
        s = StageHealthMonitor()
        out += feed(s, 0, 2.0, ref, reference=ref, roi=(0.0, 0.0, 1.0, 1.0))
        out += feed(s, 2.2, 6.0, shifted(ref, 30), reference=ref, roi=(0.0, 0.0, 1.0, 1.0))
        r = StageHealthMonitor()                   # 跨轮次恢复与阻塞读帧也要过校验器（换轮次名，避免与上面同源同轮次的确定性 id 撞车）
        out += feed(r, 0, 2.0, noise, run="run-r1")
        out += feed(r, 2.2, 8.0, None, run="run-r1")
        for i, t in enumerate(steps(8.2, 12.0)):
            out.extend(r.update(ctx(t, noise(), run="run-r2", start_ms=i * 200)))
        out += feed(r, 30.0, 33.0, noise, run="run-r2")
        return out

    def test_ac9_all_observations_pass_contract_validator(self):
        observations = self.collect()
        self.assertGreaterEqual(len(observations), 7)
        validator = ContractValidator()
        for o in observations:
            self.assertEqual(validator.validate_object("observation", o), [], o["type"])
            self.assertEqual(o["contract_version"], "2.0.0")
            self.assertEqual(o["model_name"], "stage_health")
            for ref in o["evidence_refs"]:
                self.assertEqual(validator.validate_object("evidence-ref", ref), [])
        ids = [o["observation_id"] for o in observations]
        self.assertEqual(len(ids), len(set(ids)))


class LegacyAdapterTest(unittest.TestCase):
    def legacy(self, obs_type, t, payload=None, session="session-001"):
        return {"type": obs_type, "source_id": "cam-1", "session_id": session,
                "occurred_at": epoch(t), "payload": json.dumps(payload or {}, ensure_ascii=False)}

    def test_ac10_adapt_offline_then_online_with_gap(self):
        reg = GapRegistry()
        o = adapt_legacy_message(self.legacy("video_offline", 5.0, {"reason": "no_frame"}),
                                 media_run_id="run-1", start_ms=5000, registry=reg)
        self.assertEqual(o["type"], "video_offline")
        self.assertEqual(o["payload"]["gap_id"], "gap:cam-1:run-1:offline:5000")
        self.assertEqual(o["payload"]["technical_reason"], "no_frame")
        self.assertEqual(o["binding_status"], "unbound")
        self.assertIsNone(o["session_id"])
        self.assertEqual(o["occurred_at"], at(5.0))
        o2 = adapt_legacy_message(self.legacy("video_online", 9.0), media_run_id="run-1", start_ms=9000, registry=reg)
        self.assertEqual(o2["type"], "video_online")
        self.assertEqual(o2["payload"]["gap_id"], o["payload"]["gap_id"])
        self.assertFalse(reg.find(o["payload"]["gap_id"]).is_open)
        b = adapt_legacy_message(self.legacy("camera_blacked", 12.0, {"variance": 3.5}),
                                 media_run_id="run-1", start_ms=12000, registry=reg)
        self.assertEqual(b["payload"]["variance"], 3.5)
        self.assertEqual(b["payload"]["gap_id"], "gap:cam-1:run-1:quality:12000")
        validator = ContractValidator()
        for x in (o, o2, b):
            self.assertEqual(validator.validate_object("observation", x), [])

    def test_ac10_scope_from_caller_makes_it_bound(self):
        scope = dict(SCOPE, **SNAPSHOT)
        o = adapt_legacy_message(self.legacy("video_offline", 5.0), media_run_id="run-1", start_ms=5000,
                                 registry=GapRegistry(), scope=scope)
        self.assertEqual(o["binding_status"], "bound")
        self.assertEqual(o["session_id"], "s1")
        self.assertEqual(ContractValidator().validate_object("observation", o), [])

    def test_ac10_cannot_adapt_cases(self):
        self.assertEqual(adapt_legacy_message(self.legacy("video_offline", 5.0), media_run_id=None, start_ms=5000,
                                              registry=GapRegistry()), {"cannot_adapt": "missing_media_run_id"})
        self.assertEqual(adapt_legacy_message(self.legacy("video_online", 9.0), media_run_id="run-1", start_ms=9000,
                                              registry=GapRegistry()), {"cannot_adapt": "no_open_gap"})
        self.assertEqual(adapt_legacy_message(self.legacy("person_detected", 1.0), media_run_id="run-1", start_ms=1000,
                                              registry=GapRegistry()), {"cannot_adapt": "unsupported_type"})
        self.assertEqual(adapt_legacy_message({"type": "video_offline", "occurred_at": epoch(1.0)}, media_run_id="run-1",
                                              start_ms=1000, registry=GapRegistry()), {"cannot_adapt": "missing_source_id"})

    @unittest.skipUnless(os.getenv("STAGE_HEALTH_WIRED") == "1", "10.1 接线前门控：旧判定仍在 main.py 里")
    def test_legacy_inline_health_removed(self):
        src = (Path(__file__).resolve().parents[1] / "worker" / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("offline_reported", src)
        self.assertNotIn("black_started", src)


class ReviewFindingsTest(unittest.TestCase):
    """2026-10-19 独立复核发现的边界问题，逐条锁住（编号对应变更包 1.01 验证第 7 节）。"""

    def test_r1_frames_then_offline_before_online_confirmed(self):
        m = StageHealthMonitor()
        obs = feed(m, 0, 1.0, noise)              # 有帧但不足 2 秒恢复确认
        obs += feed(m, 1.2, 10.0, None)
        self.assertEqual(len(obs), 1)
        o = obs[0]
        self.assertEqual((o["type"], o["occurred_at"]), ("video_offline", at(4.0)))
        self.assertEqual((o["start_ms"], o["end_ms"]), (1000, 4000))
        self.assertEqual(o["payload"]["technical_reason"], "no_frame")
        self.assertEqual(o["payload"]["gap_id"], "gap:cam-1:run-1:offline:1000")
        self.assertEqual(m.status()["stream"], "offline")
        self.assertEqual(m.status()["waiting_seconds"], 0.0)

    def test_r3_blocking_read_opens_gap_retroactively(self):
        m = StageHealthMonitor()
        obs = feed(m, 0, 2.0, noise)
        obs += m.update(ctx(20.0, noise()))        # 中间入口被 cap.read 卡住 18 秒，没机会报无帧
        offline = by_type(obs, "video_offline")
        self.assertEqual(len(offline), 1)
        self.assertEqual(offline[0]["occurred_at"], at(20.0))
        self.assertEqual((offline[0]["start_ms"], offline[0]["end_ms"]), (2000, 20000))
        obs += feed(m, 20.2, 23.0, noise)
        online = by_type(obs, "video_online")
        self.assertEqual(len(online), 1)
        self.assertEqual(online[0]["occurred_at"], at(22.0))
        self.assertEqual(online[0]["payload"]["gap_id"], offline[0]["payload"]["gap_id"])
        self.assertEqual(online[0]["payload"]["gap_duration_ms"], 18000)
        self.assertEqual(m.status()["open_gaps"], [])

    def test_r4_restored_quality_gap_can_be_closed(self):
        old = StageHealthMonitor()
        feed(old, 0, 1.0, noise)
        feed(old, 1.2, 4.0, black)                 # 打开一条 quality 缺口
        state = json.loads(json.dumps(old.registry.export()))
        reg = GapRegistry()
        reg.restore(state)
        self.assertEqual(len(reg.open_gaps("cam-1")), 1)
        m = StageHealthMonitor(registry=reg)      # 重启后的新实例
        obs = feed(m, 10.0, 11.0, noise)
        self.assertEqual(m.status()["quality"], "degraded")
        obs += feed(m, 11.2, 14.0, noise)
        normal = by_type(obs, "camera_normal")
        self.assertEqual(len(normal), 1)
        self.assertEqual(normal[0]["occurred_at"], at(12.0))
        self.assertEqual(normal[0]["payload"]["gap_id"], reg.all()[0].gap_id)
        self.assertEqual(reg.open_gaps("cam-1"), [])
        self.assertEqual(m.status()["quality"], "good")

    def test_r4_restored_offline_gap_primes_stream_state(self):
        reg = GapRegistry()
        reg.open(source_id="cam-1", kind="offline", media_run_id="run-0", start_ms=5000, occurred_at=at(0), scope=SCOPE)
        m = StageHealthMonitor(registry=reg)
        m.update(ctx(0, None))
        self.assertEqual(m.status()["stream"], "offline")
        obs = feed(m, 0.2, 3.0, noise)
        self.assertEqual(by_type(obs, "video_online")[0]["payload"]["gap_media_run_id"], "run-0")

    def test_r5_quality_timers_reset_when_stream_drops(self):
        m = StageHealthMonitor()
        obs = feed(m, 0, 2.0, noise)
        obs += feed(m, 2.2, 3.0, black)           # 0.8 秒黑屏，未确认
        obs += feed(m, 3.2, 13.0, None)           # 断流
        obs += feed(m, 13.2, 16.0, black)         # 恢复后仍黑
        blacked = by_type(obs, "camera_blacked")
        self.assertEqual(len(blacked), 1)
        self.assertEqual(blacked[0]["occurred_at"], at(15.2))
        self.assertEqual(blacked[0]["start_ms"], 13200)
        self.assertEqual(by_type(obs, "video_online")[0]["occurred_at"], at(15.2))
        m2 = StageHealthMonitor()
        feed(m2, 0, 1.0, noise)
        obs2 = feed(m2, 1.2, 4.0, black)          # 已确认低质量
        obs2 += feed(m2, 4.2, 5.0, noise)         # 0.8 秒恢复，未确认
        obs2 += feed(m2, 5.2, 15.0, None)         # 断流
        obs2 += feed(m2, 15.2, 18.0, noise)
        normal = by_type(obs2, "camera_normal")
        self.assertEqual(len(normal), 1)
        self.assertEqual(normal[0]["occurred_at"], at(17.2))
        self.assertEqual(normal[0]["payload"]["gap_end_ms"], 15200)

    def test_r6_open_and_close_share_identical_gap_evidence(self):
        m = StageHealthMonitor()
        obs = feed(m, 0, 2.0, noise)
        obs += feed(m, 2.2, 8.0, None)
        obs += feed(m, 8.2, 11.0, noise)
        offline, online = by_type(obs, "video_offline")[0], by_type(obs, "video_online")[0]
        self.assertEqual(offline["evidence_refs"], online["evidence_refs"])
        self.assertIsNone(offline["evidence_refs"][0]["start_ms"])
        self.assertIsNone(online["evidence_refs"][0]["end_ms"])

    def test_r7_media_run_change_without_reset_is_treated_as_reset(self):
        m = StageHealthMonitor()
        obs = feed(m, 0, 2.0, noise)
        obs += feed(m, 2.2, 8.0, None)
        later = []
        for i, t in enumerate(steps(8.2, 12.0)):  # 入口忘了调 reset，直接换了轮次
            later.extend(m.update(ctx(t, noise(), run="run-2", start_ms=i * 200)))
        online = by_type(later, "video_online")
        self.assertEqual(len(online), 1)
        self.assertEqual((online[0]["start_ms"], online[0]["end_ms"]), (0, 2000))
        gap = m.registry.find(online[0]["payload"]["gap_id"])
        self.assertEqual((gap.gap_media_run_id, gap.close_media_run_id, gap.close_end_ms), ("run-1", "run-2", 0))

    def test_r8_tiny_roi_is_unmeasurable_not_a_crash(self):
        ref = noise(1)
        m = StageHealthMonitor()
        obs = feed(m, 0, 3.0, ref, reference=ref, roi=(0.996, 0.0, 1.0, 1.0))
        self.assertEqual(obs, [])
        self.assertEqual(m.status()["offset"]["state"], "unmeasurable")

    def test_r9_unusable_frames_count_as_no_frame(self):
        m = StageHealthMonitor()
        obs = feed(m, 0, 2.0, noise)
        obs += feed(m, 2.2, 6.0, lambda: np.zeros((0, 0, 3), dtype=np.uint8))
        self.assertEqual(len(by_type(obs, "video_offline")), 1)
        self.assertEqual(by_type(obs, "video_offline")[0]["occurred_at"], at(5.0))
        self.assertIsNotNone(m.status()["frame_error"])
        gray_ok = StageHealthMonitor()
        gray_ok.update(ctx(0, noise()[:, :, 0]))            # 单通道
        gray_ok.update(ctx(0.2, np.dstack([noise(), np.full((48, 64), 255, np.uint8)])))   # BGRA
        gray_ok.update(ctx(0.4, (noise().astype(np.float32) / 255.0)))                      # 0～1 浮点
        self.assertIsNone(gray_ok.status()["frame_error"])
        self.assertGreater(gray_ok.status()["last_metrics"]["variance"], 10)

    def test_r11_source_change_is_rejected(self):
        m = StageHealthMonitor()
        m.update(ctx(0, noise()))
        other = FrameContext(source_id="cam-2", occurred_at=at(0.2), media_run_id="run-1", start_ms=200, frame=noise())
        with self.assertRaises(ValueError):
            m.update(other)

    def test_r13_non_monotonic_start_ms_is_flagged(self):
        m = StageHealthMonitor()
        feed(m, 0, 2.0, noise)
        feed(m, 2.2, 8.0, None)
        obs = []
        obs += m.update(ctx(8.2, noise(), start_ms=9000))
        obs += m.update(ctx(8.4, noise(), start_ms=8400))     # PTS 倒退
        obs += feed(m, 8.6, 11.0, noise)
        online = by_type(obs, "video_online")[0]
        self.assertIn("start_ms_not_monotonic", online["quality_flags"]) if online["occurred_at"] == at(8.4) \
            else self.assertEqual(online["occurred_at"], at(10.2))


class ConfigTest(unittest.TestCase):
    def test_defaults_follow_execution_card(self):
        c = StageHealthConfig()
        self.assertEqual((c.offline_seconds, c.degraded_seconds, c.recovery_seconds, c.black_var_threshold), (3.0, 2.0, 2.0, 10.0))

    def test_from_env_keeps_legacy_variable_names(self):
        env = {"OFFLINE_SECONDS": "4", "BLACK_SECONDS": "1.5", "BLACK_VAR_THRESHOLD": "12", "SHIFT_PX": "8"}
        c = StageHealthConfig.from_env(env)
        self.assertEqual((c.offline_seconds, c.degraded_seconds, c.black_var_threshold, c.shift_px), (4.0, 1.5, 12.0, 8.0))
        self.assertEqual(c.recovery_seconds, 2.0)


if __name__ == "__main__":
    unittest.main()
