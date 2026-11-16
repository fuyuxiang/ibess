"""执行卡 7.3：S08 疑似拍摄 / 携离资料。桩验证。
运行（services/vision-worker 下）：PYTHONPATH=. python -m unittest discover -s tests -p test_document_rules_v2.py -v
"""
import sys
import unittest
from pathlib import Path

from worker.document_rules import CARRY_NOTE, DocumentRules, Handover, INPUT_TYPES, RULE_CARRY, RULE_PHOTO, evaluate

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_device_rules_v2 import IDENT, TRACK, context as s01_context, utc, visual_fact  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "contracts"))
from contract_validator import ContractValidator  # noqa: E402


def context(handovers=(), **kw):
    ctx = s01_context(**kw)
    ctx["scene_id"] = "S08"
    ctx["rule_version"] = kw.get("rule_version", "s08-v1")
    ctx["snapshot"]["rule_versions"] = {"S08": ctx["rule_version"], "S10": "s10-v1"} if ctx["rule_version"] else {"S10": "s10-v1"}
    ctx["snapshot"]["phase_id"] = "DETAILED"
    ctx["handovers"] = list(handovers)
    return ctx


def fact(ms, **kw):
    obs = visual_fact(ms, **kw)
    obs["phase_id"] = "DETAILED" if obs["binding_status"] == "bound" else None
    obs["payload"]["scene_hint"] = "S08"
    if kw.get("kind") == "paper":
        obs["payload"]["candidate"]["kind"] = "paper"
        obs["payload"]["candidate"]["label"] = "book"
    return obs


def crossing(ms, direction="out", track=TRACK):
    base = fact(ms)
    obs_id = f"camera-entry:run-1:line_crossed:door:{track}:{ms}"
    base.update(observation_id=obs_id, type="line_crossed", start_ms=ms - 200, end_ms=ms, model_name="zone_observer", model_version="5.1-v1",
                payload={"line_id": "door", "track_id": track, "direction": direction, "track_ids": [track], "config_version": 3})
    base["evidence_refs"] = [dict(base["evidence_refs"][0], evidence_id=f"{obs_id}:video", evidence_type="video", start_ms=ms - 200, end_ms=ms)]
    return base


def handover(person="person-7", fact_id="hand-1", handed=utc(-60000), returned=None):
    return {"fact_id": fact_id, "fact_version": 1, "person_id": person, "document_id": "doc-1", "handed_at": handed, "returned_at": returned}


def permit(kind, scope, fact_id="perm-1"):
    return {"fact_id": fact_id, "fact_version": 1, "kind": kind, "scope": scope, "valid_from": utc(-60000), "valid_until": utc(600000)}


class DocumentRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = ContractValidator()

    def setUp(self):
        self.rules = DocumentRules()

    def _run(self, observations, ctx=None):
        ctx = ctx or context(identities=IDENT)
        out = []
        for o in observations:
            out.extend(self.rules.evaluate([o], ctx))
        for clue in out:
            self.assertEqual(self.validator.validate_object("risk-clue", clue), [], clue["reason"])
        return out

    def test_ac5_photographing_three_states(self):
        shot = fact(5000, activity="photographing")
        clues = self._run([shot])
        self.assertEqual([(c["conclusion"], c["risk_level"], c["rule_id"], c["person_id"]) for c in clues], [("suspected", "medium", RULE_PHOTO, "person-7")])
        self.assertIn("不推断所摄内容", clues[0]["reason"])
        allowed = self._run([shot], context(identities=IDENT, authorizations=[permit("photo_allowed", {"person_id": "person-7"})]))
        self.assertEqual(allowed[0]["conclusion"], "normal")
        personal_unknown = self._run([shot], context(authorizations=[permit("photo_allowed", {"person_id": "person-7"})]))
        self.assertEqual(personal_unknown[0]["conclusion"], "insufficient")
        self.rules = DocumentRules()
        unclear = self._run([fact(5000, activity="photographing", flags=("visibility_unclear",))])
        self.assertEqual(unclear[0]["conclusion"], "insufficient")
        self.rules = DocumentRules()
        nobody = self._run([shot], context())
        self.assertEqual((nobody[0]["conclusion"], nobody[0]["person_id"]), ("suspected", None), "无个人许可、身份未核验：拍摄姿态本身可疑")

    def test_ac6_paper_carry_out_three_states(self):
        clues = self._run([fact(1000, kind="paper", holding="in_hand", activity="reading"), crossing(8000)])
        self.assertEqual([(c["conclusion"], c["risk_level"], c["rule_id"]) for c in clues], [("suspected", "low", RULE_CARRY)])
        self.assertIn(CARRY_NOTE, clues[0]["reason"])
        self.assertEqual(len(clues[0]["observation_ids"]), 2)
        self.assertEqual((clues[0]["start_ms"], clues[0]["end_ms"]), (600, 8000))
        self.rules = DocumentRules()
        ok = self._run([fact(1000, kind="paper", holding="in_hand", activity="reading"), crossing(8000)], context(identities=IDENT, handovers=[handover()]))
        self.assertEqual(ok[0]["conclusion"], "normal")
        self.assertIn("hand-1 v1", ok[0]["reason"])
        self.rules = DocumentRules()
        returned = self._run([fact(1000, kind="paper", holding="in_hand", activity="reading"), crossing(8000)], context(identities=IDENT, handovers=[handover(returned=utc(2000))]))
        self.assertEqual(returned[0]["conclusion"], "suspected", "已归还的交接不算")
        self.rules = DocumentRules()
        unknown = self._run([fact(1000, kind="paper", holding="in_hand", activity="reading"), crossing(8000)], context(handovers=[handover()]))
        self.assertEqual(unknown[0]["conclusion"], "insufficient")
        self.rules = DocumentRules()
        access = self._run([fact(1000, kind="paper", holding="in_hand", activity="reading"), crossing(8000)],
                           context(identities=IDENT, authorizations=[permit("document_access", {"room_id": "room-a"})]))
        self.assertEqual(access[0]["conclusion"], "normal")
        self.rules = DocumentRules()
        self.assertEqual(self._run([fact(1000, kind="paper", holding="in_hand", activity="reading"), crossing(30000)]), [], "超过 15 秒窗口不关联")
        self.rules = DocumentRules()
        self.assertEqual(self._run([crossing(8000)]), [], "没见过纸张候选的跨出不是本规则的事")
        self.rules = DocumentRules()
        self.assertEqual(self._run([fact(1000, kind="paper", holding="in_hand", activity="reading"), crossing(8000, direction="in")]), [])

    def test_ac7_negative_examples(self):
        self.assertEqual(self._run([fact(5000, kind="paper", holding="in_hand", activity="reading")]), [], "低头看纸不出线索")
        self.assertEqual(self._run([fact(5000, kind="phone", holding="in_hand", activity="operating_device")]), [], "普通拿手机归 S02")
        screen = self._run([fact(5000, kind="screen", visibility="unclear", holding="unknown", activity="unknown")])
        self.assertEqual(screen[0]["conclusion"], "insufficient")
        self.assertEqual(self._run([fact(5000, kind="cup", holding="in_hand", activity="none")]), [])
        unclear_paper = self._run([fact(1000, kind="paper", visibility="unclear", holding="unknown", activity="unknown"), crossing(8000)])
        self.assertEqual(unclear_paper[0]["conclusion"], "insufficient")

    def test_ac8_failed_review_emits_nothing(self):
        # 复核失败是处理状态（契约：禁止伪装 normal 或 insufficient；与 D-25 同一口径）
        self.assertEqual(self._run([fact(5000, status="failed", error_code="TIMEOUT", flags=("model_failed",))]), [])
        self.assertEqual(self._run([fact(6000, status="failed", error_code="UPSTREAM_503", flags=("model_failed",))]), [])

    def test_review_m2_m3_dedupe_consume_and_degraded(self):
        shots = [fact(ms, activity="photographing") for ms in (5000, 6000, 7000)]
        self.assertEqual(len(self._run(shots)), 1, "连续多帧拍摄姿态只出一条")
        self.rules = DocumentRules()
        clues = self._run([fact(1000, kind="paper", holding="in_hand", activity="reading"), crossing(8000), crossing(9000)])
        self.assertEqual([c["conclusion"] for c in clues], ["suspected"], "一次纸张记忆只判一次携离")
        self.rules = DocumentRules()
        degraded = crossing(8000)
        degraded["quality_flags"] = ["camera_degraded"]
        self.assertEqual(self._run([fact(1000, kind="paper", holding="in_hand", activity="reading"), degraded]), [], "故障帧的跨线不判")

    def test_ac9_ac10_contract_version_unbound_reset_and_once(self):
        clue = self._run([fact(5000, activity="photographing")], context(identities=IDENT, rule_version="s08-v3"))[0]
        self.assertEqual(clue["rule_version"], "s08-v3")
        with self.assertRaises(ValueError):
            self.rules.evaluate([fact(5000)], context(rule_version=None))
        unbound = self._run([fact(5000, bound=False), fact(6000, bound=False)])
        self.assertEqual([(c["conclusion"], c["binding_status"]) for c in unbound], [("insufficient", "unbound")])
        self.rules = DocumentRules()
        self._run([fact(1000, kind="paper", holding="in_hand", activity="reading")])
        other_run = crossing(8000)
        other_run["media_run_id"] = "run-2"
        other_run["evidence_refs"][0]["media_run_id"] = "run-2"
        self.assertEqual(self._run([other_run]), [], "新轮次清掉纸张记忆")
        self.assertEqual(self.rules.evaluate([{"type": "seat_vacant"}], context()), [])
        self.assertEqual(evaluate.input_types, INPUT_TYPES)
        with self.assertRaises(ValueError):
            Handover.from_dict(handover(person=""))
        with self.assertRaises(ValueError):
            Handover.from_dict(handover(handed=utc(10), returned=utc(0)))


if __name__ == "__main__":
    unittest.main()
