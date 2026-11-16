"""执行卡 5.3：S10 音频采集心跳、静音标记与断流缺口（桩验证：构造音频块，逻辑时钟）。

运行（服务根目录）：PYTHONPATH=. python -m unittest discover -s tests -p test_audio_health_v2.py -v
"""
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from worker.audio_health import (
    AudioChunkContext,
    AudioGapBook,
    AudioHealthConfig,
    AudioHealthMonitor,
    make_gap_id,
)

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "contracts"))
from contract_validator import ContractValidator  # noqa: E402

BASE = datetime(2026, 10, 19, 1, 0, 0, tzinfo=timezone.utc)
STEP = 0.5
SCOPE = {"project_id": "p1", "room_id": "r1", "session_id": "s1"}
SNAPSHOT = {"stage_snapshot_id": "snap-1", "phase_id": "DETAILED", "phase_version": 1}


def at(t):
    return (BASE + timedelta(seconds=t)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def loud(seed=0):
    return (np.random.default_rng(seed).standard_normal(8000) * 0.3).astype(np.float32)


def silent():
    return np.zeros(8000, dtype=np.float32)


def ctx(t, run="run-1", bound=True, start_ms=None, snapshot=None):
    fields = dict(source_id="mic-1", occurred_at=at(t), media_run_id=run,
                  start_ms=int(round(t * 1000)) if start_ms is None else start_ms, asset_id=None)
    if bound:
        fields.update(SCOPE)
        fields.update(snapshot or SNAPSHOT)
    return AudioChunkContext(**fields)


def steps(t0, t1):
    n0, n1 = int(round(t0 / STEP)), int(round(t1 / STEP))
    return [round(i * STEP, 3) for i in range(n0, n1 + 1)]


def feed(monitor, t0, t1, samples_fn, **kw):
    out = []
    for t in steps(t0, t1):
        if samples_fn is None:
            out.extend(monitor.tick(ctx(t, **kw)))
        else:
            out.extend(monitor.on_chunk(ctx(t, **kw), samples_fn()))
    return out


def by_type(observations, obs_type):
    return [o for o in observations if o["type"] == obs_type]


class AudioHealthTest(unittest.TestCase):
    def test_ac8_silence_is_quality_flag_not_offline(self):
        m = AudioHealthMonitor()
        obs = feed(m, 0, 6.0, silent)
        self.assertEqual(obs, [])
        st = m.status()
        self.assertEqual(st["stream"], "online")
        self.assertTrue(st["silence"])
        self.assertLess(st["last_rms"], 0.01)
        self.assertEqual(st["open_gaps"], [])

    def test_ac8_heartbeat_timeout_opens_gap_and_recovery_closes_it(self):
        m = AudioHealthMonitor()
        obs = feed(m, 0, 2.0, loud)
        obs += feed(m, 2.5, 9.5, None)
        offline = by_type(obs, "audio_offline")
        self.assertEqual(len(obs), 1)
        self.assertEqual(len(offline), 1)
        o = offline[0]
        self.assertEqual(o["occurred_at"], at(5.0))
        self.assertEqual((o["start_ms"], o["end_ms"]), (2000, 5000))
        self.assertEqual(o["payload"]["gap_id"], "gap:mic-1:run-1:audio_offline:2000")
        self.assertEqual(o["payload"]["technical_reason"], "no_chunk")
        self.assertEqual(o["evidence_refs"][0]["evidence_type"], "gap")
        self.assertEqual(o["evidence_refs"][0]["status"], "missing")
        self.assertEqual(m.status()["stream"], "offline")
        obs += feed(m, 10.0, 14.0, loud)
        online = by_type(obs, "audio_online")
        self.assertEqual(len(online), 1)
        self.assertEqual(online[0]["occurred_at"], at(12.0))
        self.assertEqual(online[0]["payload"]["gap_id"], o["payload"]["gap_id"])
        self.assertEqual((online[0]["start_ms"], online[0]["end_ms"]), (10000, 12000))
        gap = m.registry.find(o["payload"]["gap_id"])
        self.assertFalse(gap.is_open)
        self.assertEqual(gap.close_end_ms - gap.open_start_ms, 8000)
        self.assertEqual(online[0]["payload"]["gap_duration_ms"], 8000)
        self.assertEqual(m.status()["stream"], "online")

    def test_waiting_without_chunks_reports_once(self):
        m = AudioHealthMonitor()
        obs = feed(m, 0, 10.0, None)
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["type"], "audio_offline")
        self.assertEqual(obs[0]["occurred_at"], at(3.0))
        self.assertEqual(obs[0]["payload"]["technical_reason"], "no_stream_at_start")
        self.assertEqual((obs[0]["start_ms"], obs[0]["end_ms"]), (0, 3000))
        self.assertEqual(m.status()["stream"], "waiting")

    def test_silence_flag_rides_on_recovery_observation(self):
        m = AudioHealthMonitor()
        obs = feed(m, 0, 2.0, loud)
        obs += feed(m, 2.5, 9.5, None)
        obs += feed(m, 10.0, 14.0, silent)
        online = by_type(obs, "audio_online")
        self.assertEqual(len(online), 1)
        self.assertIn("silence", online[0]["quality_flags"])
        self.assertNotIn("silence", by_type(obs, "audio_offline")[0]["quality_flags"])

    def test_reset_keeps_open_gap_and_recovery_references_it(self):
        m = AudioHealthMonitor()
        obs = feed(m, 0, 2.0, loud)
        obs += feed(m, 2.5, 9.5, None)
        m.reset("run-2")
        self.assertEqual(m.status()["open_gaps"], ["gap:mic-1:run-1:audio_offline:2000"])
        later = []
        for i, t in enumerate(steps(10.0, 14.0)):
            later.extend(m.on_chunk(ctx(t, run="run-2", start_ms=i * 500), loud()))
        online = by_type(later, "audio_online")
        self.assertEqual(len(online), 1)
        self.assertEqual(online[0]["payload"]["gap_media_run_id"], "run-1")
        self.assertEqual(online[0]["media_run_id"], "run-2")
        self.assertEqual((online[0]["start_ms"], online[0]["end_ms"]), (0, 2000))
        self.assertEqual(m.registry.find("gap:mic-1:run-1:audio_offline:2000").close_media_run_id, "run-2")

    def test_unbound_context_marks_binding_missing(self):
        m = AudioHealthMonitor()
        obs = feed(m, 0, 2.0, loud, bound=False)
        obs += feed(m, 2.5, 6.0, None, bound=False)
        o = by_type(obs, "audio_offline")[0]
        self.assertEqual(o["binding_status"], "unbound")
        self.assertIsNone(o["session_id"])
        self.assertIsNone(o["phase_version"])
        self.assertIn("binding_missing", o["quality_flags"])

    def test_int16_samples_are_normalised(self):
        m = AudioHealthMonitor()
        m.on_chunk(ctx(0), (np.ones(8000) * 9830).astype(np.int16))  # 0.3 满幅
        self.assertAlmostEqual(m.status()["last_rms"], 0.3, places=2)
        self.assertFalse(m.status()["silence"])

    def test_ac9_all_observations_pass_contract_validator(self):
        out = []
        m = AudioHealthMonitor()
        out += feed(m, 0, 2.0, loud)
        out += feed(m, 2.5, 9.5, None)
        out += feed(m, 10.0, 14.0, silent)
        u = AudioHealthMonitor()
        out += feed(u, 0, 10.0, None, bound=False)
        self.assertGreaterEqual(len(out), 3)
        validator = ContractValidator()
        for o in out:
            self.assertEqual(validator.validate_object("observation", o), [], o["type"])
            self.assertEqual(o["model_name"], "audio_health")
            for ref in o["evidence_refs"]:
                self.assertEqual(validator.validate_object("evidence-ref", ref), [])


class ReviewFindingsTest(unittest.TestCase):
    """2026-10-19 独立复核发现的边界问题（变更包 1.01 验证第 7 节）。"""

    def test_r1_chunks_then_timeout_before_online_confirmed(self):
        m = AudioHealthMonitor()
        obs = feed(m, 0, 1.0, loud)
        obs += feed(m, 1.5, 9.5, None)
        self.assertEqual(len(obs), 1)
        self.assertEqual((obs[0]["type"], obs[0]["occurred_at"]), ("audio_offline", at(4.0)))
        self.assertEqual((obs[0]["start_ms"], obs[0]["payload"]["technical_reason"]), (1000, "no_chunk"))
        self.assertEqual(m.status()["stream"], "offline")

    def test_r3_late_chunk_opens_gap_retroactively(self):
        m = AudioHealthMonitor()
        obs = feed(m, 0, 2.0, loud)
        obs += m.on_chunk(ctx(20.0), loud())
        offline = by_type(obs, "audio_offline")
        self.assertEqual(len(offline), 1)
        self.assertEqual((offline[0]["occurred_at"], offline[0]["start_ms"], offline[0]["end_ms"]), (at(20.0), 2000, 20000))
        obs += feed(m, 20.5, 23.0, loud)
        online = by_type(obs, "audio_online")
        self.assertEqual(len(online), 1)
        self.assertEqual(online[0]["occurred_at"], at(22.0))
        self.assertEqual(online[0]["payload"]["gap_duration_ms"], 18000)

    def test_r6_open_and_close_share_identical_gap_evidence(self):
        m = AudioHealthMonitor()
        obs = feed(m, 0, 2.0, loud)
        obs += feed(m, 2.5, 9.5, None)
        obs += feed(m, 10.0, 13.0, loud)
        self.assertEqual(by_type(obs, "audio_offline")[0]["evidence_refs"], by_type(obs, "audio_online")[0]["evidence_refs"])

    def test_r7_media_run_change_without_reset(self):
        m = AudioHealthMonitor()
        feed(m, 0, 2.0, loud)
        feed(m, 2.5, 9.5, None)
        later = []
        for i, t in enumerate(steps(10.0, 14.0)):
            later.extend(m.on_chunk(ctx(t, run="run-2", start_ms=i * 500), loud()))
        online = by_type(later, "audio_online")
        self.assertEqual((online[0]["start_ms"], online[0]["end_ms"], online[0]["payload"]["gap_media_run_id"]), (0, 2000, "run-1"))

    def test_r10_empty_chunk_is_not_a_heartbeat(self):
        m = AudioHealthMonitor()
        obs = feed(m, 0, 2.0, loud)
        obs += feed(m, 2.5, 9.5, lambda: np.zeros(0, dtype=np.float32))
        offline = by_type(obs, "audio_offline")
        self.assertEqual(len(offline), 1)
        self.assertEqual((offline[0]["occurred_at"], offline[0]["start_ms"]), (at(5.0), 2000))

    def test_r10_nan_and_unsigned_samples(self):
        m = AudioHealthMonitor()
        m.on_chunk(ctx(0), np.array([np.nan, np.inf, 0.0], dtype=np.float32))
        self.assertEqual(m.status()["last_rms"], 0.0)
        json.dumps(m.status())                      # payload / status 必须是合法 JSON
        u = AudioHealthMonitor()
        u.on_chunk(ctx(0), np.full(8000, 128, dtype=np.uint8))   # 无符号 PCM 的静音中点
        self.assertTrue(u.status()["silence"])
        stereo = AudioHealthMonitor()
        stereo.on_chunk(ctx(0), np.stack([loud(), loud()], axis=1))   # (采样数, 声道数)
        self.assertAlmostEqual(stereo.status()["last_rms"], 0.3, places=1)

    def test_r11_source_change_is_rejected(self):
        m = AudioHealthMonitor()
        m.on_chunk(ctx(0), loud())
        with self.assertRaises(ValueError):
            m.on_chunk(AudioChunkContext(source_id="mic-2", occurred_at=at(0.5), media_run_id="run-1", start_ms=500), loud())

    def test_r4_restored_open_gap_primes_offline_and_closes(self):
        old = AudioHealthMonitor()
        feed(old, 0, 2.0, loud)
        feed(old, 2.5, 9.5, None)
        book = AudioGapBook()
        book.restore(json.loads(json.dumps(old.registry.export())))
        m = AudioHealthMonitor(registry=book)
        m.tick(ctx(20.0))
        self.assertEqual(m.status()["stream"], "offline")
        obs = feed(m, 20.5, 23.0, loud)
        self.assertEqual(len(by_type(obs, "audio_online")), 1)
        self.assertEqual(book.open_gaps("mic-1"), [])


class AudioGapBookTest(unittest.TestCase):
    def test_gap_id_rule_matches_coverage_gap(self):
        self.assertEqual(make_gap_id("mic-1", "run-1", "audio_offline", 2000), "gap:mic-1:run-1:audio_offline:2000")

    def test_open_close_export_restore(self):
        book = AudioGapBook()
        g = book.open(source_id="mic-1", kind="audio_offline", media_run_id="run-1", start_ms=2000,
                      occurred_at=at(5), scope=SCOPE, technical_reason="no_chunk")
        self.assertIs(book.open(source_id="mic-1", kind="audio_offline", media_run_id="run-1", start_ms=9000,
                                occurred_at=at(9), scope=SCOPE), g)
        book.close(g.gap_id, media_run_id="run-1", end_ms=10000, occurred_at=at(12))
        self.assertIsNone(book.open_gap("mic-1", "audio_offline"))
        other = AudioGapBook()
        other.restore(json.loads(json.dumps(book.export())))
        self.assertEqual(other.find(g.gap_id).to_dict(), g.to_dict())
        with self.assertRaises(KeyError):
            book.close("gap:nobody", media_run_id="run-1", end_ms=0, occurred_at=at(1))


class ConfigTest(unittest.TestCase):
    def test_defaults_and_env(self):
        c = AudioHealthConfig()
        self.assertEqual((c.heartbeat_seconds, c.recovery_seconds, c.silence_rms_threshold), (3.0, 2.0, 0.01))
        e = AudioHealthConfig.from_env({"AUDIO_HEARTBEAT_SECONDS": "5", "AUDIO_SILENCE_RMS": "0.02"})
        self.assertEqual((e.heartbeat_seconds, e.silence_rms_threshold, e.recovery_seconds), (5.0, 0.02, 2.0))


if __name__ == "__main__":
    unittest.main()
