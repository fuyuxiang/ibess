"""执行卡 7.2：S02 评审期间疑似使用通讯设备。桩验证。
运行（services/vision-worker 下）：PYTHONPATH=. python -m unittest discover -s tests -p test_phone_rules_v2.py -v
"""
import sys
import unittest
from pathlib import Path

from worker.phone_rules import INPUT_TYPES, LIMITATION, PhoneRuleConfig, PhoneRules, RULE_ID, evaluate

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_device_rules_v2 import IDENT, TRACK, context as s01_context, exemption, visual_fact  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "contracts"))
from contract_validator import ContractValidator  # noqa: E402


def context(**kw):
    ctx = s01_context(**kw)
    ctx["scene_id"] = "S02"
    ctx["rule_version"] = kw.get("rule_version", "s02-v1")
    ctx["snapshot"]["rule_versions"] = {"S02": ctx["rule_version"], "S10": "s10-v1"} if ctx["rule_version"] else {"S10": "s10-v1"}
    ctx["snapshot"]["phase_id"] = "DETAILED"
    return ctx


def fact(ms, **kw):
    obs = visual_fact(ms, **kw)
    obs["phase_id"] = "DETAILED" if obs["binding_status"] == "bound" else None
    obs["payload"]["scene_hint"] = "S02"
    return obs


class PhoneRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = ContractValidator()

    def setUp(self):
        self.rules = PhoneRules()

    def _run(self, observations, ctx=None):
        ctx = ctx or context(identities=IDENT)
        out = []
        for obs in observations:
            out.extend(self.rules.evaluate([obs], ctx))
        for clue in out:
            self.assertEqual(self.validator.validate_object("risk-clue", clue), [], clue["reason"])
        return out

    def test_ac7_two_consecutive_reviews_then_one_suspected(self):
        clues = self._run([fact(5000), fact(10000), fact(15000)])
        self.assertEqual([(c["conclusion"], c["risk_level"], c["rule_id"]) for c in clues], [("suspected", "medium", RULE_ID)])
        clue = clues[0]
        self.assertEqual(clue["observation_ids"], [fact(5000)["observation_id"], fact(10000)["observation_id"]])
        self.assertEqual((clue["start_ms"], clue["end_ms"]), (4600, 10000))
        self.assertEqual((clue["person_id"], clue["track_id"]), ("person-7", TRACK))
        self.assertIn(LIMITATION, clue["reason"])
        high = self._run([fact(30000, holding="at_ear", activity="phone_call"), fact(35000, holding="at_ear", activity="phone_call")])
        self.assertEqual([c["risk_level"] for c in high], ["high"], "同段后来出现贴耳 / 通话：补一条 high")
        self.assertTrue(high[0]["clue_id"].endswith(":high"))
        self.assertEqual(self._run([fact(40000, holding="at_ear", activity="phone_call")]), [], "升级只补一次")
        self.rules = PhoneRules()
        high = self._run([fact(30000, holding="at_ear", activity="phone_call"), fact(35000, holding="in_hand", activity="operating_device")])
        self.assertEqual([c["risk_level"] for c in high], ["high"], "任一次贴耳 / 通话即 high")
        self.rules = PhoneRules()
        photo = self._run([fact(1000, activity="photographing"), fact(6000, activity="photographing")])
        self.assertEqual(photo[0]["risk_level"], "medium")

    def test_ac8_streak_breaks_and_run_reset(self):
        clues = self._run([fact(5000), fact(10000, holding="on_table", activity="none"), fact(15000)])
        self.assertEqual(clues, [], "放桌上打断连续性，之后只算第一次")
        clues = self._run([fact(20000)])
        self.assertEqual(len(clues), 1)
        self.rules = PhoneRules()
        clues = self._run([fact(5000), fact(40000)])
        self.assertEqual(clues, [], "超过窗口不连续")
        self.rules = PhoneRules()
        self._run([fact(5000)])
        obs = fact(10000)
        obs["media_run_id"] = "run-2"
        obs["observation_id"] += ":run-2"
        obs["evidence_refs"][0]["media_run_id"] = "run-2"
        self.assertEqual(self._run([obs]), [], "新轮次从零计数")
        self.rules = PhoneRules()
        clues = self._run([fact(5000), fact(10000, kind="paper", activity="reading")])
        self.assertEqual(clues, [], "看纸不是手机使用")

    def test_ac4_unclear_is_insufficient_and_breaks_streak_failed_emits_nothing(self):
        clues = self._run([fact(5000), fact(10000, visibility="unclear", holding="unknown", activity="unknown"), fact(15000)])
        self.assertEqual([c["conclusion"] for c in clues], ["insufficient"])
        self.assertIn("看不清", clues[0]["reason"])
        # D-25：复核失败不出线索，也不打断连续计数
        self.rules = PhoneRules()
        clues = self._run([fact(5000), fact(8000, status="failed", error_code="UPSTREAM_503", flags=("model_failed",)), fact(10000)])
        self.assertEqual([(c["conclusion"], c["risk_level"]) for c in clues], [("suspected", "medium")])

    def test_review_regressions_paper_duplicate_partial_evidence_config(self):
        # 同一人手机与纸张交替复核：纸张不打断手机连续性
        clues = self._run([fact(5000), fact(7000, kind="paper", activity="reading"), fact(10000)])
        self.assertEqual([c["conclusion"] for c in clues], ["suspected"])
        self.assertEqual(len(clues[0]["evidence_refs"]), 2, "证据带上整段的关键帧")
        # 纸张看不清不会产生 S02 线索
        self.rules = PhoneRules()
        self.assertEqual(self._run([fact(5000, kind="paper", visibility="unclear", holding="unknown", activity="unknown")]), [])
        # 同一观察重复投递不累加
        self.rules = PhoneRules()
        self.assertEqual(self._run([fact(5000), fact(5000)]), [])
        # partial = 动作不确定，不算正例
        self.rules = PhoneRules()
        partial = [fact(ms, visibility="partial", holding="at_ear", activity="phone_call") for ms in (5000, 10000)]
        self.assertEqual(self._run(partial), [])
        # 派发器共享 context：别的场景的 config 不影响 S02
        from worker.absence_rules import AbsenceRuleConfig
        self.rules = PhoneRules()
        ctx = context(identities=IDENT)
        ctx["config"] = AbsenceRuleConfig()
        self.assertEqual(len(self._run([fact(5000), fact(10000)], ctx)), 1)

    def test_ac5_exemption_is_normal_once(self):
        ctx = context(identities=IDENT, authorizations=[exemption({"person_id": "person-7"})])
        clues = self._run([fact(5000), fact(10000), fact(15000)], ctx)
        self.assertEqual([c["conclusion"] for c in clues], ["normal"])
        self.assertIn("allow-1 v1", clues[0]["reason"])

    def test_ac9_identity_only_from_verification(self):
        clues = self._run([fact(5000), fact(10000)], context())
        self.assertEqual((clues[0]["conclusion"], clues[0]["person_id"], clues[0]["track_id"]), ("suspected", None, TRACK))
        ctx = context()
        ctx["seat_assignments"] = {"seat-1": "roster-person"}
        ctx["track_seats"] = {TRACK: "seat-1"}
        self.rules = PhoneRules()
        clues = self._run([fact(5000), fact(10000)], ctx)
        self.assertEqual((clues[0]["person_id"], clues[0]["seat_id"]), (None, "seat-1"))

    def test_ac10_contract_rule_version_unbound_config(self):
        clue = self._run([fact(5000), fact(10000)], context(identities=IDENT, rule_version="s02-v4"))[0]
        self.assertEqual(clue["rule_version"], "s02-v4")
        with self.assertRaises(ValueError):
            self.rules.evaluate([fact(5000)], context(rule_version=None))
        unbound = self.rules.evaluate([fact(5000, bound=False)], context())
        self.assertEqual((unbound[0]["conclusion"], unbound[0]["binding_status"]), ("insufficient", "unbound"))
        self.assertEqual(self.rules.evaluate([{"type": "seat_vacant"}], context()), [])
        self.assertEqual(evaluate.input_types, INPUT_TYPES)
        single = PhoneRules(PhoneRuleConfig(min_consecutive=1))
        self.assertEqual(len(single.evaluate([fact(5000)], context(identities=IDENT))), 1)
        with self.assertRaises(ValueError):
            PhoneRuleConfig(min_consecutive=0)


if __name__ == "__main__":
    unittest.main()
