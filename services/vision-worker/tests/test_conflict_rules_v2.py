"""执行卡 7.4：S09 秩序冲突规则。桩验证。
运行（services/vision-worker 下）：PYTHONPATH=. python -m unittest discover -s tests -p test_conflict_rules_v2.py -v
"""
import sys
import unittest
from pathlib import Path

from worker.conflict_rules import ConflictConfig, ConflictRules, INPUT_TYPES, MotionDetector, RULE_ID, evaluate

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cross_zone_rules_v2 import SCOPE, SOURCE, RUN, count, obs, utc  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "contracts"))
from contract_validator import ContractValidator  # noqa: E402


SYNC = {SOURCE: "room-a-av", "mic-a": "room-a-av"}       # 同室摄像头与麦克风是同步来源


def context(rule_version="s09-v1", sync_groups=SYNC):
    snap = {**SCOPE, "source_id": SOURCE, "stage_snapshot_id": "snap-1", "phase_id": "DETAILED", "phase_version": 1,
            "rule_versions": {"S09": rule_version, "S10": "s10-v1"} if rule_version else {"S10": "s10-v1"}, "exceptions": []}
    return {"snapshot": snap, "scene_id": "S09", "rule_version": rule_version, "exceptions": [], "sync_groups": dict(sync_groups or {})}


def audio(ms, kind="threat_phrase", quotes=("打你",), speaker="A", bound=True, run=RUN):
    scope = SCOPE if bound else {"project_id": None, "room_id": None, "session_id": None}
    obs_id = f"mic-a:{run}:audio_order_candidate:{kind}:{ms}"
    base = {"contract_version": "2.0.0", **scope, "source_id": "mic-a", "observation_id": obs_id, "occurred_at": utc(ms), "media_run_id": run,
            "asset_id": "rec-1", "start_ms": ms, "end_ms": ms + 3000, "type": "audio_order_candidate",
            "payload": {"kind": kind, "segment_ids": ["s1"] if kind == "threat_phrase" else [], "speaker": speaker, "quotes": list(quotes) if kind == "threat_phrase" else [],
                        "matched_terms": list(quotes) if kind == "threat_phrase" else [], "rise_db": 15.0},
            "model_name": "order_rules", "model_version": "7.4-v1", "quality_flags": [] if bound else ["binding_missing"],
            "evidence_refs": [{"contract_version": "2.0.0", **scope, "source_id": "mic-a", "evidence_id": f"{obs_id}:audio", "media_run_id": run,
                               "asset_id": "rec-1", "evidence_type": "audio", "start_ms": ms, "end_ms": ms + 3000, "sha256": None, "status": "pending"}]}
    if bound:
        base.update(stage_snapshot_id="snap-1", phase_id="DETAILED", phase_version=1, binding_status="bound")
    else:
        base.update(stage_snapshot_id=None, phase_id=None, phase_version=None, binding_status="unbound")
    return base


STILL = [("track:run-1:1", [0.20, 0.40, 0.26, 0.60]), ("track:run-1:2", [0.70, 0.40, 0.76, 0.60])]
RUSH = [("track:run-1:1", [0.50, 0.40, 0.56, 0.60]), ("track:run-1:2", [0.70, 0.40, 0.76, 0.60])]     # 轨迹 1 在 1 秒内位移 0.3


class ConflictRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = ContractValidator()

    def setUp(self):
        self.rules = ConflictRules()

    def _run(self, observations, ctx=None):
        ctx = ctx or context()
        out = []
        for o in observations:
            out.extend(self.rules.evaluate([o], ctx))
        for clue in out:
            self.assertEqual(self.validator.validate_object("risk-clue", clue), [], clue["reason"])
        return out

    def test_ac4_motion_candidates(self):
        detector = MotionDetector(ConflictConfig())
        self.assertEqual(detector.feed(0, [{"track_id": t, "bbox": b} for t, b in STILL]), [])
        cands = detector.feed(1000, [{"track_id": t, "bbox": b} for t, b in RUSH])
        kinds = sorted(c["kind"] for c in cands)
        self.assertEqual(kinds, ["converge", "fast_move"])
        self.assertEqual(detector.feed(2000, [{"track_id": t, "bbox": b} for t, b in RUSH]), [], "静止不再是候选")
        slow = MotionDetector(ConflictConfig())
        slow.feed(0, [{"track_id": "t", "bbox": [0.20, 0.40, 0.26, 0.60]}])
        self.assertEqual(slow.feed(1000, [{"track_id": "t", "bbox": [0.25, 0.40, 0.31, 0.60]}]), [], "慢走不算")

    def test_ac5_fusion_within_window_is_suspected_medium(self):
        clues = self._run([count(0, STILL), audio(500), count(1000, RUSH)])
        self.assertEqual([(c["conclusion"], c["risk_level"], c["rule_id"]) for c in clues], [("suspected", "low", RULE_ID), ("suspected", "medium", RULE_ID)])
        fused = clues[1]
        self.assertEqual(len(fused["observation_ids"]), 2)
        self.assertIn("mic-a:run-1:audio_order_candidate:threat_phrase:500", fused["observation_ids"])
        self.assertIn("speaker_unverified", fused["quality_flags"])
        self.assertIsNone(fused["person_id"])
        self.assertIn("责任人待人工核验", fused["reason"])
        self.assertEqual(fused["source_id"], SOURCE, "融合线索用摄像头源")
        self.rules = ConflictRules()
        reverse = self._run([count(0, STILL), count(1000, RUSH), audio(3000)])
        self.assertEqual([c["risk_level"] for c in reverse], ["none", "medium"], "先视觉后音频：先 insufficient 再融合")

    def test_ac6_threat_alone_is_low_with_speaker_unverified(self):
        clues = self._run([audio(500)])
        self.assertEqual([(c["conclusion"], c["risk_level"]) for c in clues], [("suspected", "low")])
        self.assertIn("speaker_unverified", clues[0]["quality_flags"])
        self.assertIn("「打你」", clues[0]["reason"])
        self.assertIn("需人工听审", clues[0]["reason"])
        self.assertEqual(self._run([audio(500)]), [], "同一候选不重复")

    def test_ac7_loud_burst_alone_emits_nothing(self):
        self.assertEqual(self._run([audio(500, kind="loud_burst")]), [])
        clues = self._run([count(0, STILL), count(1000, RUSH)])
        self.assertEqual([c["conclusion"] for c in clues], ["insufficient"], "声强突发不能当音频佐证")
        self.assertEqual(self._run([audio(1500, kind="loud_burst")]), [], "视觉候选之后的声强突发也不融合")

    def test_ac8_visual_alone_is_insufficient_once(self):
        clues = self._run([count(0, STILL), count(1000, RUSH), count(2000, STILL), count(3000, RUSH)])
        self.assertEqual([c["conclusion"] for c in clues], ["insufficient"])
        self.assertIn("同步音频中的威胁 / 争吵语句", clues[0]["missing_facts"])

    def test_ac9_window_reset_and_degraded(self):
        clues = self._run([audio(500), count(20000, STILL), count(21000, RUSH)])
        self.assertEqual([c["risk_level"] for c in clues], ["low", "none"], "超出 ±10 秒不融合")
        self.rules = ConflictRules()
        self._run([count(0, STILL), count(1000, RUSH)])
        self._run([count(1200, STILL, run="run-2")])          # 摄像头换轮次：上一轮的视觉候选作废
        clues = self._run([audio(1500)])
        self.assertEqual([c["risk_level"] for c in clues], ["low"], "新轮次不与旧轮次的视觉候选融合")
        self.rules = ConflictRules()
        self.assertEqual(self._run([count(0, STILL), count(1000, RUSH, flags=("camera_degraded",))]), [])

    def test_review_h1_fusion_across_real_run_ids_needs_sync_group(self):
        mic_run = "run:mic-a:snap-1:0:1"                       # 4.2 / 4.3 的真实 run id 带源名，两路永远不同
        clues = self._run([audio(500, run=mic_run), count(0, STILL), count(1000, RUSH)])
        self.assertEqual([c["risk_level"] for c in clues], ["low", "medium"], "同步组内按 UTC 时间融合")
        self.rules = ConflictRules()
        clues = self._run([audio(500, run=mic_run), count(0, STILL), count(1000, RUSH)], context(sync_groups={}))
        self.assertEqual([c["risk_level"] for c in clues], ["low", "none"], "没配同步来源：只出单模态")

    def test_review_h2_m4_m5_one_fused_clue_per_threat_and_new_visual_segment(self):
        frames = [count(0, STILL)] + [count(ms, RUSH if (ms // 1000) % 2 else STILL) for ms in range(1000, 5000, 1000)]
        clues = self._run([audio(500)] + frames)
        self.assertEqual([c["risk_level"] for c in clues], ["low", "medium"], "多帧快速动作只融合一次")
        self.assertEqual({r["evidence_type"] for r in clues[1]["evidence_refs"]}, {"audio", "video"}, "融合证据带两路")
        self.rules = ConflictRules()
        reverse = self._run([count(0, STILL), count(1000, RUSH), audio(3000)])
        self.assertEqual(reverse[1]["source_id"], "mic-a", "音频触发的融合线索与触发观察同源")
        self.assertEqual(reverse[1]["track_id"], "track:run-1:1")
        self.rules = ConflictRules()
        first = self._run([count(0, STILL), count(1000, RUSH)])
        later = self._run([count(600000, STILL), count(601000, RUSH)])
        self.assertEqual([c["conclusion"] for c in first + later], ["insufficient", "insufficient"], "静默很久后的新动作是新的一段")

    def test_ac10_contract_unbound_rule_version_inputs(self):
        clue = self._run([audio(500)], context("s09-v2"))[0]
        self.assertEqual(clue["rule_version"], "s09-v2")
        with self.assertRaises(ValueError):
            self.rules.evaluate([audio(500)], context(None))
        self.rules = ConflictRules()
        unbound = self._run([audio(500, bound=False), audio(600, bound=False)])
        self.assertEqual([(c["conclusion"], c["binding_status"]) for c in unbound], [("insufficient", "unbound")])
        self.assertEqual(self.rules.evaluate([obs("seat_vacant", 0, {"seat_id": "s"})], context()), [])
        self.assertEqual(evaluate.input_types, INPUT_TYPES)
        with self.assertRaises(ValueError):
            ConflictConfig(speed_threshold=0)


if __name__ == "__main__":
    unittest.main()
