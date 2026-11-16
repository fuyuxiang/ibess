"""执行卡 7.4：S09 音频侧候选。桩验证（构造句段、合成采样块）。
运行（services/audio-worker 下）：PYTHONPATH=. python -m unittest discover -s tests -p test_order_rules_v2.py -v
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from worker.audio_health import AudioChunkContext
from worker.order_rules import AudioOrderMonitor, LoudnessDetector, OrderConfig, ThreatLexicon, dbfs

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "contracts"))
from contract_validator import ContractValidator  # noqa: E402

T0 = datetime(2026, 10, 19, 2, 0, 0, tzinfo=timezone.utc)


def ctx(ms, bound=True, run="run-1"):
    fields = dict(source_id="mic-a", occurred_at=(T0 + timedelta(milliseconds=ms)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                  media_run_id=run, start_ms=ms, asset_id="rec-1")
    if bound:
        fields.update(project_id="project-a", room_id="room-a", session_id="session-a", stage_snapshot_id="snap-1", phase_id="DETAILED", phase_version=1)
    return AudioChunkContext(**fields)


def segment(sid, text, speaker="A", start=1000, end=4000):
    return {"segment_id": sid, "speaker": speaker, "start_ms": start, "end_ms": end, "text": text}


def tone(level_dbfs, n=1600):
    amplitude = 10 ** (level_dbfs / 20.0) * np.sqrt(2)
    t = np.arange(n) / 16000.0
    return (amplitude * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


class OrderRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = ContractValidator()

    def test_ac1_threat_phrase_quotes_are_verbatim_substrings(self):
        monitor = AudioOrderMonitor()
        obs = monitor.on_segments(ctx(1000), [segment("s1", "你再这样我就打你，你等着"), segment("s2", "这个标准我们再讨论一下"), segment("s3", "打你", speaker="B")])
        self.assertEqual([o["payload"]["segment_ids"] for o in obs], [["s1"], ["s3"]])
        self.assertEqual(obs[0]["payload"]["quotes"], ["打你"], "「你等着」误召多，不在默认词表")
        self.assertEqual(obs[0]["payload"]["segment_text"], "你再这样我就打你，你等着")
        for o in obs:
            for quote in o["payload"]["quotes"]:
                self.assertIn(quote, next(s["text"] for s in (segment("s1", "你再这样我就打你，你等着"), segment("s3", "打你")) if s["segment_id"] == o["payload"]["segment_ids"][0]))
        self.assertEqual(obs[0]["payload"]["speaker"], "A")
        self.assertNotIn("person_id", obs[0]["payload"])
        self.assertEqual(monitor.on_segments(ctx(2000), [segment("s1", "你再这样我就打你")]), [], "同一句段只评一次")
        self.assertEqual(ThreatLexicon(("打你",)).match("今天打印一下"), [])
        with self.assertRaises(ValueError):
            OrderConfig(lexicon=("", "x"))

    def test_review_h3_raised_voice_with_pauses_is_not_repeated_bursts(self):
        monitor = AudioOrderMonitor(OrderConfig(rise_db=12, burst_min_ms=400, baseline_window_ms=5000))
        out, ms = [], 0
        for _ in range(30):
            out += monitor.on_chunk(ctx(ms), tone(-30), chunk_ms=100); ms += 100
        for _ in range(30):                                   # 提高音量讲 54 秒：1.5 秒大声、0.3 秒停顿交替
            for _ in range(15):
                out += monitor.on_chunk(ctx(ms), tone(-12), chunk_ms=100); ms += 100
            for _ in range(3):
                out += monitor.on_chunk(ctx(ms), tone(-28), chunk_ms=100); ms += 100
        bursts = [o for o in out if o["payload"]["kind"] == "loud_burst"]
        self.assertLessEqual(len(bursts), 2, "基线几轮内跟上，之后不再每句都算突发")
        self.assertGreater(monitor.loudness.baseline(), -20)

    def test_ac2_loud_burst_relative_to_baseline_excludes_sustained_and_spikes(self):
        config = OrderConfig(rise_db=12, burst_min_ms=400, baseline_window_ms=5000)
        monitor = AudioOrderMonitor(config)
        out = []
        ms = 0
        for _ in range(30):                                   # 3 秒正常说话 -30 dBFS
            out += monitor.on_chunk(ctx(ms), tone(-30), chunk_ms=100)
            ms += 100
        for _ in range(6):                                    # 600 ms 突升到 -10 dBFS
            out += monitor.on_chunk(ctx(ms), tone(-10), chunk_ms=100)
            ms += 100
        for _ in range(10):
            out += monitor.on_chunk(ctx(ms), tone(-30), chunk_ms=100)
            ms += 100
        bursts = [o for o in out if o["payload"]["kind"] == "loud_burst"]
        self.assertEqual(len(bursts), 1)
        payload = bursts[0]["payload"]
        self.assertEqual((payload["duration_ms"], bursts[0]["start_ms"], bursts[0]["end_ms"]), (600, 3000, 3600))
        self.assertGreaterEqual(payload["rise_db"], 12)
        self.assertEqual(self.validator.validate_object("observation", bursts[0]), [])
        # 短尖峰 200 ms：不算
        spike = AudioOrderMonitor(config)
        out, ms = [], 0
        for _ in range(30):
            out += spike.on_chunk(ctx(ms), tone(-30), chunk_ms=100); ms += 100
        for _ in range(2):
            out += spike.on_chunk(ctx(ms), tone(-10), chunk_ms=100); ms += 100
        for _ in range(10):
            out += spike.on_chunk(ctx(ms), tone(-30), chunk_ms=100); ms += 100
        self.assertEqual(out, [], "200 ms 尖峰是背景噪声，不算")
        # 持续偏高 8 秒：超过最大突发时长，基线跟上，不算
        loud = AudioOrderMonitor(config)
        out, ms = [], 0
        for _ in range(30):
            out += loud.on_chunk(ctx(ms), tone(-30), chunk_ms=100); ms += 100
        for _ in range(80):
            out += loud.on_chunk(ctx(ms), tone(-10), chunk_ms=100); ms += 100
        self.assertEqual(out, [], "提高音量持续讲话不是突发")
        self.assertAlmostEqual(loud.loudness.baseline(), -10.0, delta=0.5, msg="基线已跟上")
        # 没有基线（开头就大声）：不算
        cold = AudioOrderMonitor(config)
        self.assertEqual(cold.on_chunk(ctx(0), tone(-5), chunk_ms=100), [])

    def test_ac3_contract_unbound_and_run_reset(self):
        monitor = AudioOrderMonitor()
        obs = monitor.on_segments(ctx(1000, bound=False), [segment("s1", "滚出去")])
        self.assertEqual(obs[0]["binding_status"], "unbound")
        self.assertIn("binding_missing", obs[0]["quality_flags"])
        self.assertEqual(obs[0]["evidence_refs"][0]["evidence_type"], "audio")
        self.assertEqual((obs[0]["start_ms"], obs[0]["end_ms"]), (1000, 4000))
        self.assertEqual(self.validator.validate_object("observation", obs[0]), [])
        again = monitor.on_segments(ctx(1000, run="run-2"), [segment("s1", "滚出去")])
        self.assertEqual(len(again), 1, "新轮次句段重新评")
        self.assertEqual(again[0]["media_run_id"], "run-2")
        detector = LoudnessDetector(OrderConfig())
        detector.feed(0, 100, -30)
        with self.assertRaises(ValueError):
            detector.feed(50, 150, -30)
        self.assertEqual(dbfs(0.0), -120.0)
        self.assertAlmostEqual(dbfs(1.0), 0.0)


if __name__ == "__main__":
    unittest.main()
