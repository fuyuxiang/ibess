"""执行卡 7.2：S01 通讯设备寄存规则。桩验证（构造 7.1 事实与 6.x 桩事实）。
运行（services/vision-worker 下）：PYTHONPATH=. python -m unittest discover -s tests -p test_device_rules_v2.py -v
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from worker.device_rules import Deposit, DeviceRules, INPUT_TYPES, RULE_ID, evaluate, resolve_identity

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "contracts"))
from contract_validator import ContractValidator  # noqa: E402

T0 = datetime(2026, 10, 19, 2, 0, 0, tzinfo=timezone.utc)
SCOPE = {"project_id": "project-a", "room_id": "room-a", "session_id": "session-a"}
SOURCE, RUN, TRACK = "camera-entry", "run-1", "track:run-1:1"


def utc(ms):
    return (T0 + timedelta(milliseconds=ms)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def visual_fact(ms=5000, *, kind="phone", visibility="clear", holding="in_hand", activity="operating_device", status="ok",
                flags=(), holder=TRACK, too_small=False, bound=True, error_code=None):
    obs_id = f"{SOURCE}:{RUN}:visual_fact:review:{ms}:{kind}"
    facts = None if status != "ok" else {"object_kind": kind, "visibility": visibility, "holding": holding, "activity": activity,
                                          "evidence": [{"frame_index": 1, "region": "roi"}], "notes": "构造"}
    scope = SCOPE if bound else {"project_id": None, "room_id": None, "session_id": None}
    base = {"contract_version": "2.0.0", **scope, "source_id": SOURCE, "observation_id": obs_id, "occurred_at": utc(ms), "media_run_id": RUN,
            "asset_id": "asset-1", "start_ms": ms - 400, "end_ms": ms, "type": "visual_fact",
            "payload": {"status": status, "error_code": error_code, "facts": facts,
                        "candidate": {"kind": "paper" if kind == "paper" else "phone", "label": "cell phone", "bbox": [0.4, 0.4, 0.46, 0.5], "confidence": 0.6,
                                      "holder_track_id": holder, "too_small": too_small, "start_ms": ms},
                        "request_id": f"review:{ms}", "frames": [], "prompt_version": "visual-review-v1", "scene_hint": "S01"},
            "model_name": "fake-vlm", "model_version": "fake-vlm@visual-review-v1", "quality_flags": list(flags),
            "evidence_refs": [{"contract_version": "2.0.0", **scope, "source_id": SOURCE, "evidence_id": f"{obs_id}:keyframe", "media_run_id": RUN,
                               "asset_id": "asset-1", "evidence_type": "keyframe", "start_ms": ms, "end_ms": ms, "sha256": None, "status": "pending"}]}
    if bound:
        base.update(stage_snapshot_id="snap-1", phase_id="ATTENDANCE", phase_version=1, binding_status="bound")
    else:
        base.update(stage_snapshot_id=None, phase_id=None, phase_version=None, binding_status="unbound")
        base["quality_flags"].append("binding_missing")
    return base


def context(*, identities=None, deposits=(), authorizations=(), rule_version="s01-v1"):
    auths = list(authorizations)
    snap = {**SCOPE, "source_id": SOURCE, "stage_snapshot_id": "snap-1", "phase_id": "ATTENDANCE", "phase_version": 1,
            "rule_versions": {"S01": rule_version, "S10": "s10-v1"} if rule_version else {"S10": "s10-v1"},
            "exceptions": [{"fact_id": a["fact_id"], "fact_version": a["fact_version"]} for a in auths]}
    return {"snapshot": snap, "scene_id": "S01", "rule_version": rule_version, "exceptions": snap["exceptions"],
            "authorizations": auths, "identities": identities or {}, "deposits": list(deposits)}


def deposit(person="person-7", kind="phone", at=utc(-60000), returned=None, fact_id="deposit-1"):
    return {"fact_id": fact_id, "fact_version": 1, "person_id": person, "device_kind": kind, "deposited_at": at, "returned_at": returned}


def exemption(scope=None, fact_id="allow-1"):
    return {"fact_id": fact_id, "fact_version": 1, "kind": "device_allowed", "scope": scope or {"person_id": "person-7"},
            "valid_from": utc(-60000), "valid_until": utc(600000)}


IDENT = {TRACK: {"person_id": "person-7", "fact_id": "verify-1", "fact_version": 1}}


class DeviceRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = ContractValidator()

    def setUp(self):
        self.rules = DeviceRules()

    def _one(self, obs, ctx):
        clues = DeviceRules().evaluate([obs], ctx)          # 每次新实例：本组用例逐条看单次判定，去重另测
        for clue in clues:
            self.assertEqual(self.validator.validate_object("risk-clue", clue), [], clue["reason"])
        self.assertEqual(len(clues), 1)
        return clues[0]

    def test_ac1_deposited_but_device_in_hand_is_suspected(self):
        # D-26：已寄存同类设备、手里又见设备 → 疑似另有未寄存设备
        clue = self._one(visual_fact(), context(identities=IDENT, deposits=[deposit()]))
        self.assertEqual((clue["conclusion"], clue["risk_level"], clue["person_id"], clue["rule_id"]), ("suspected", "medium", "person-7", RULE_ID))
        self.assertIn("deposit-1 v1", clue["reason"])
        self.assertIn("未寄存", clue["reason"])
        returned = self._one(visual_fact(), context(identities=IDENT, deposits=[deposit(returned=utc(1000))]))
        self.assertEqual(returned["conclusion"], "suspected", "已取回的寄存不算")
        later = self._one(visual_fact(), context(identities=IDENT, deposits=[deposit(at=utc(9000))]))
        self.assertEqual(later["conclusion"], "suspected", "发生时刻之后才寄存的不算")

    def test_ac2_visible_device_without_deposit_or_exemption_is_suspected(self):
        clue = self._one(visual_fact(), context(identities=IDENT))
        self.assertEqual((clue["conclusion"], clue["risk_level"], clue["person_id"], clue["track_id"]), ("suspected", "medium", "person-7", TRACK))
        self.assertEqual(clue["observation_ids"], [visual_fact()["observation_id"]])
        self.assertEqual(clue["evidence_refs"][0]["evidence_type"], "keyframe")
        mismatch = self._one(visual_fact(kind="phone"), context(identities=IDENT, deposits=[deposit(kind="watch")]))
        self.assertEqual(mismatch["conclusion"], "suspected")
        self.assertIn("不一致", mismatch["reason"])

    def test_ac3_unverified_identity_is_insufficient(self):
        clue = self._one(visual_fact(), context(deposits=[deposit()]))
        self.assertEqual((clue["conclusion"], clue["person_id"]), ("insufficient", None))
        self.assertIn("持有人身份未经核验绑定", clue["missing_facts"])
        via_seat = context(deposits=[deposit()])
        via_seat["track_seats"] = {TRACK: "seat-1"}
        via_seat["seat_identities"] = {"seat-1": {"person_id": "person-7", "fact_id": "verify-2", "fact_version": 1}}
        clue = self._one(visual_fact(), via_seat)
        self.assertEqual((clue["conclusion"], clue["seat_id"], clue["person_id"]), ("suspected", "seat-1", "person-7"))
        self.assertIsNone(resolve_identity({}, None))
        self.assertIsNone(resolve_identity({"track_seats": {}}, TRACK))
        nobody = self._one(visual_fact(holder=None), context(identities=IDENT))
        self.assertEqual(nobody["conclusion"], "insufficient")

    def test_ac4_unclear_or_too_small_is_insufficient_failed_emits_nothing(self):
        for obs in (visual_fact(visibility="unclear", holding="unknown", activity="unknown"), visual_fact(flags=("visibility_unclear",)),
                    visual_fact(too_small=True, flags=("candidate_too_small",))):
            clue = self._one(obs, context(identities=IDENT))
            self.assertEqual(clue["conclusion"], "insufficient", obs["quality_flags"])
            self.assertTrue(clue["missing_facts"])
        # D-25：模型 / 网络失败是处理状态，规则不出任何线索（契约：禁止伪装 normal 或 insufficient）
        for code in ("TIMEOUT", "UPSTREAM_503", "MODEL_NOT_CONFIGURED"):
            failed = visual_fact(status="failed", error_code=code, flags=("model_failed",))
            self.assertEqual(DeviceRules().evaluate([failed], context(identities=IDENT)), [], code)

    def test_ac5_exemption_is_normal(self):
        for scope in ({"person_id": "person-7"}, {"room_id": "room-a"}):
            clue = self._one(visual_fact(), context(identities=IDENT, authorizations=[exemption(scope)]))
            self.assertEqual(clue["conclusion"], "normal", scope)
            self.assertIn("allow-1 v1", clue["reason"])
        other = self._one(visual_fact(), context(identities=IDENT, authorizations=[exemption({"person_id": "person-9"})]))
        self.assertEqual(other["conclusion"], "suspected")
        unreferenced = context(identities=IDENT, authorizations=[exemption()])
        unreferenced["exceptions"] = []
        self.assertEqual(self._one(visual_fact(), unreferenced)["conclusion"], "suspected", "快照没引用的豁免不算")

    def test_ac6_non_device_is_normal(self):
        for kind in ("paper", "cup", "none", "screen", "other"):
            clue = self._one(visual_fact(kind=kind, holding="in_hand", activity="reading"), context(identities=IDENT))
            self.assertEqual(clue["conclusion"], "normal", kind)
        unclear_non_device = self._one(visual_fact(kind="other", visibility="unclear", holding="unknown", activity="unknown"), context(identities=IDENT))
        self.assertEqual(unclear_non_device["conclusion"], "insufficient")

    def test_ac10_rule_version_unbound_and_input_types(self):
        clue = self._one(visual_fact(), context(identities=IDENT, rule_version="s01-v3"))
        self.assertEqual(clue["rule_version"], "s01-v3")
        with self.assertRaises(ValueError):
            self.rules.evaluate([visual_fact()], context(rule_version=None))
        unbound = self._one(visual_fact(bound=False), context(identities=IDENT))
        self.assertEqual((unbound["conclusion"], unbound["binding_status"]), ("insufficient", "unbound"))
        self.assertEqual(self.rules.evaluate([{"type": "person_count"}], context()), [])
        self.assertEqual(evaluate.input_types, INPUT_TYPES)
        self.assertEqual(evaluate([visual_fact()], context(identities=IDENT))[0]["scene_id"], "S01")

    def test_same_subject_same_conclusion_once_and_run_reset(self):
        rules = DeviceRules()
        ctx = context(identities=IDENT)
        out = []
        for ms in range(5000, 65000, 5000):                  # 同一人手持一分钟，按 7.1 去重约每 5 秒一次复核
            out += rules.evaluate([visual_fact(ms)], ctx)
        self.assertEqual([c["conclusion"] for c in out], ["suspected"], "持续可见只出一条 suspected")
        other = visual_fact(70000, holder="track:run-1:2")
        self.assertEqual(len(rules.evaluate([other], ctx)), 1, "另一持有人单独计")
        nobody_a = visual_fact(71000, holder=None)
        nobody_b = visual_fact(72000, holder=None)
        nobody_b["payload"]["candidate"]["bbox"] = [0.8, 0.1, 0.86, 0.2]
        self.assertEqual(len(rules.evaluate([nobody_a], ctx)) + len(rules.evaluate([nobody_b], ctx)), 2, "画面两处无人持有的设备各算一个主体")
        renewed = visual_fact(80000)
        renewed["media_run_id"] = "run-2"
        renewed["observation_id"] += ":run-2"
        renewed["evidence_refs"][0]["media_run_id"] = "run-2"
        self.assertEqual(len(rules.evaluate([renewed], ctx)), 1, "新媒体轮次重新计")

    def test_deposit_validation(self):
        with self.assertRaises(ValueError):
            Deposit.from_dict(deposit(person=""))
        with self.assertRaises(ValueError):
            Deposit.from_dict(dict(deposit(), fact_version=0))
        with self.assertRaises(ValueError):
            Deposit.from_dict(deposit(at=utc(10), returned=utc(0)))
        with self.assertRaises(ValueError):
            self.rules.evaluate([visual_fact()], context(identities=IDENT, deposits=["junk"]))


if __name__ == "__main__":
    unittest.main()
