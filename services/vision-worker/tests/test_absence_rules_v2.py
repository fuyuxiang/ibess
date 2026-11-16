"""执行卡 5.2：S03 离岗 / 越界 / 许可规则。

桩验证：构造的 5.1 观察 + 桩授权事实（6.3）与核验身份（6.2）。
运行（services/vision-worker 下）：PYTHONPATH=. python -m unittest discover -s tests -p test_absence_rules_v2.py -v
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from worker.absence_rules import AbsenceRuleConfig, AbsenceRules, Authorization, INPUT_TYPES, RULE_EXIT, RULE_SEAT, RULE_ZONE, evaluate

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "contracts"))
from contract_validator import ContractValidator  # noqa: E402

T0 = datetime(2026, 10, 19, 2, 0, 0, tzinfo=timezone.utc)
SCOPE = {"project_id": "project-a", "room_id": "room-a", "session_id": "session-a"}
SOURCE, RUN, SEAT = "camera-a", "run-1", "seat-uuid-1"


def utc(ms):
    return (T0 + timedelta(milliseconds=ms)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def snapshot(exceptions=()):
    return {"contract_version": "2.0.0", **SCOPE, "source_id": SOURCE, "stage_snapshot_id": "snap-1", "phase_id": "DETAILED",
            "phase_version": 1, "effective_at": "2026-10-19T01:00:00Z", "enabled_scenes": ["S03"], "always_on_scenes": ["S10"],
            "assets": [], "rule_versions": {"S03": "s03-v1", "S10": "s10-v1"}, "exceptions": list(exceptions), "sync_status": "pending"}


def obs(kind, ms, payload, *, start_ms=None, bound=True, flags=(), run=RUN):
    obs_id = f"{SOURCE}:{run}:{kind}:{payload.get('seat_id') or payload.get('zone_id') or payload.get('line_id')}:{ms}"
    base = {"contract_version": "2.0.0", "source_id": SOURCE, "observation_id": obs_id, "occurred_at": utc(ms), "media_run_id": run,
            "asset_id": "asset-1", "start_ms": ms if start_ms is None else start_ms, "end_ms": ms, "type": kind,
            "payload": {"config_version": 3, "track_ids": [], **payload}, "model_name": "zone_observer", "model_version": "5.1-v1",
            "quality_flags": list(flags), "evidence_refs": []}
    if bound:
        base.update(SCOPE, stage_snapshot_id="snap-1", phase_id="DETAILED", phase_version=1, binding_status="bound")
        scope = SCOPE
    else:
        base.update(project_id=None, room_id=None, session_id=None, stage_snapshot_id=None, phase_id=None, phase_version=None,
                    binding_status="unbound")
        base["quality_flags"].append("binding_missing")
        scope = {"project_id": None, "room_id": None, "session_id": None}
    base["evidence_refs"] = [{"contract_version": "2.0.0", **scope, "source_id": SOURCE, "evidence_id": f"{obs_id}:video", "media_run_id": run,
                              "asset_id": "asset-1", "evidence_type": "video", "start_ms": base["start_ms"], "end_ms": ms, "sha256": None,
                              "status": "pending"}]
    return base


def vacancy(ms, vacant_ms, since=0, **kw):
    return obs("seat_vacancy_progress", ms, {"seat_id": SEAT, "zone_id": "seat-a01", "vacant_since_ms": since, "vacant_ms": vacant_ms,
                                             "paused_ms": 0, "paused": False}, start_ms=since, **kw)


def occupied(ms, vacant_ms, since=0, **kw):
    return obs("seat_occupied", ms, {"seat_id": SEAT, "zone_id": "seat-a01", "occupied_since_ms": ms, "vacant_ms": vacant_ms,
                                     "track_ids": ["track:run-1:1"]}, start_ms=since, **kw)


def auth(fact_id="leave-1", version=1, kind="leave_approved", scope=None, valid_from=utc(0), valid_until=utc(600000)):
    return {"fact_id": fact_id, "fact_version": version, "kind": kind, "scope": scope or {"seat_id": SEAT},
            "valid_from": valid_from, "valid_until": valid_until}


def context(snap=None, authorizations=(), identities=None, config=None):
    snap = snap or snapshot([{"fact_id": a["fact_id"], "fact_version": a["fact_version"]} for a in authorizations])
    ctx = {"snapshot": snap, "scene_id": "S03", "rule_version": snap["rule_versions"]["S03"], "exceptions": snap["exceptions"],
           "authorizations": list(authorizations), "seat_identities": identities or {}}
    if config:
        ctx["config"] = config
    return ctx


class AbsenceRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = ContractValidator()

    def setUp(self):
        self.rules = AbsenceRules()

    def _run(self, observations, ctx=None):
        ctx = ctx or context()
        out = []
        for o in observations:
            out.extend(self.rules.evaluate([o], ctx))
        for clue in out:
            self.assertEqual(self.validator.validate_object("risk-clue", clue), [], clue["reason"])
        return out

    def test_ac1_short_absence_then_return_is_normal_only(self):
        clues = self._run([vacancy(1000, 1000), vacancy(5000, 5000), occupied(6000, 6000)])
        self.assertEqual([c["conclusion"] for c in clues], ["normal"])
        self.assertIn("未达阈值", clues[0]["reason"])
        self.assertEqual((clues[0]["start_ms"], clues[0]["end_ms"], clues[0]["seat_id"]), (0, 6000, SEAT))

    def test_ac2_vacancy_over_threshold_once_then_normal_on_return(self):
        clues = self._run([vacancy(9000, 9000), vacancy(10000, 10000), vacancy(11000, 11000), vacancy(20000, 20000), occupied(21000, 21000)])
        self.assertEqual([c["conclusion"] for c in clues], ["suspected", "normal"])
        suspected, closed = clues
        self.assertEqual((suspected["risk_level"], suspected["rule_id"], suspected["start_ms"], suspected["end_ms"]), ("medium", RULE_SEAT, 0, 10000))
        self.assertEqual(suspected["clue_id"], f"clue:S03:{RULE_SEAT}:{SOURCE}:{RUN}:seat:{SEAT}:0:none")
        self.assertEqual(suspected["observation_ids"], [vacancy(10000, 10000)["observation_id"]])
        self.assertEqual(suspected["evidence_refs"][0]["status"], "pending")
        self.assertIsNone(suspected["person_id"])
        self.assertEqual((closed["start_ms"], closed["end_ms"]), (0, 21000))
        self.assertIn("共 21000 ms", closed["reason"])
        self.assertIn(suspected["clue_id"], closed["reason"])

    def test_ac3_seat_or_room_permission_makes_normal_when_valid(self):
        seat_auth = auth()
        clues = self._run([vacancy(12000, 12000)], context(authorizations=[seat_auth]))
        self.assertEqual([(c["conclusion"], c["risk_level"]) for c in clues], [("normal", "none")])
        self.assertIn("leave-1 v1", clues[0]["reason"])
        room_break = auth("break-1", kind="break", scope={"room_id": "room-a"})
        self.rules = AbsenceRules()
        clues = self._run([vacancy(12000, 12000)], context(authorizations=[room_break]))
        self.assertEqual(clues[0]["conclusion"], "normal")
        expired = auth(valid_from=utc(0), valid_until=utc(5000))
        self.rules = AbsenceRules()
        clues = self._run([vacancy(12000, 12000)], context(authorizations=[expired]))
        self.assertEqual(clues[0]["conclusion"], "suspected")
        future = auth(valid_from=utc(60000), valid_until=None)
        self.rules = AbsenceRules()
        clues = self._run([vacancy(12000, 12000)], context(authorizations=[future]))
        self.assertEqual(clues[0]["conclusion"], "suspected")
        other_seat = auth(scope={"seat_id": "seat-uuid-9"})
        self.rules = AbsenceRules()
        clues = self._run([vacancy(12000, 12000)], context(authorizations=[other_seat]))
        self.assertEqual(clues[0]["conclusion"], "suspected")

    def test_ac4_personal_permission_requires_verified_identity(self):
        personal = auth("leave-p", scope={"person_id": "person-7"})
        clues = self._run([vacancy(12000, 12000)], context(authorizations=[personal]))
        self.assertEqual(clues[0]["conclusion"], "insufficient")
        self.assertIn("身份未经核验绑定", clues[0]["missing_facts"][0])
        self.assertIsNone(clues[0]["person_id"])
        self.rules = AbsenceRules()
        identities = {SEAT: {"person_id": "person-7", "fact_id": "verify-1", "fact_version": 1}}
        clues = self._run([vacancy(12000, 12000)], context(authorizations=[personal], identities=identities))
        self.assertEqual((clues[0]["conclusion"], clues[0]["person_id"]), ("normal", "person-7"))
        self.rules = AbsenceRules()
        other = {SEAT: {"person_id": "person-8", "fact_id": "verify-2", "fact_version": 1}}
        clues = self._run([vacancy(12000, 12000)], context(authorizations=[personal], identities=other))
        self.assertEqual((clues[0]["conclusion"], clues[0]["person_id"]), ("suspected", "person-8"), "核验身份不匹配个人许可 → 疑似，person_id 来自核验")

    def test_ac5_referenced_exception_without_content_is_insufficient(self):
        snap = snapshot([{"fact_id": "leave-1", "fact_version": 1}])
        clues = self._run([vacancy(12000, 12000)], context(snap=snap, authorizations=[]))
        self.assertEqual(clues[0]["conclusion"], "insufficient")
        self.assertEqual(clues[0]["missing_facts"], ["例外事实 leave-1 v1 内容未提供"])
        self.rules = AbsenceRules()
        unreferenced = auth("leave-2")            # context 有但快照没引用：不算许可
        clues = self._run([vacancy(12000, 12000)], context(snap=snapshot(), authorizations=[unreferenced]))
        self.assertEqual(clues[0]["conclusion"], "suspected")

    def test_ac6_unreliable_frames_are_not_judged(self):
        for flag in ("camera_degraded", "camera_offline", "sampling_gap", "camera_blacked"):
            self.assertEqual(self.rules.evaluate([vacancy(12000, 12000, flags=(flag,))], context()), [], flag)
        clues = self._run([vacancy(12000, 12000)])
        self.assertEqual(clues[0]["conclusion"], "suspected", "标记消失后照常判")

    def test_ac7_media_run_change_resets_open_state(self):
        first = self._run([vacancy(12000, 12000)])
        self.assertEqual(len(first), 1)
        self.assertEqual(self._run([vacancy(13000, 13000)]), [], "同轮次不重复")
        again = self._run([vacancy(12000, 12000, run="run-2")])
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["media_run_id"], "run-2")
        self.assertNotEqual(again[0]["clue_id"], first[0]["clue_id"])

    def test_ac8_restricted_zone_dwell_three_states(self):
        dwell = lambda ms, d: obs("zone_dwell_progress", ms, {"zone_id": "restricted-1", "track_id": "track:run-1:2", "dwell_ms": d, "paused_ms": 0}, start_ms=ms - d)  # noqa: E731
        self.assertEqual(self._run([dwell(1000, 1000)]), [])
        clues = self._run([dwell(2000, 2000), dwell(3000, 3000)])
        self.assertEqual([(c["conclusion"], c["rule_id"], c["track_id"]) for c in clues], [("suspected", RULE_ZONE, "track:run-1:2")])
        left = obs("zone_left", 5000, {"zone_id": "restricted-1", "track_id": "track:run-1:2", "dwell_ms": 5000}, start_ms=0)
        clues = self._run([left])
        self.assertEqual([c["conclusion"] for c in clues], ["normal"])
        self.assertEqual((clues[0]["start_ms"], clues[0]["end_ms"]), (0, 5000))
        self.rules = AbsenceRules()
        access = auth("zone-ok", kind="zone_access", scope={"zone_id": "restricted-1"})
        clues = self._run([dwell(2000, 2000)], context(authorizations=[access]))
        self.assertEqual(clues[0]["conclusion"], "normal")
        self.rules = AbsenceRules()
        self.assertEqual(self._run([obs("zone_left", 5000, {"zone_id": "restricted-1", "track_id": "t", "dwell_ms": 100})]), [], "没出过线索的离开不出 normal")

    def test_ac9_exit_crossing_three_states(self):
        crossed = lambda direction, ms=4000: obs("line_crossed", ms, {"line_id": "door", "track_id": "track:run-1:3", "direction": direction}, start_ms=ms - 200)  # noqa: E731
        self.assertEqual(self._run([crossed("in")]), [])
        clues = self._run([crossed("out")])
        self.assertEqual([(c["conclusion"], c["rule_id"], c["track_id"]) for c in clues], [("suspected", RULE_EXIT, "track:run-1:3")])
        self.rules = AbsenceRules()
        clues = self._run([crossed("out")], context(authorizations=[auth("break-1", kind="break", scope={"room_id": "room-a"})]))
        self.assertEqual(clues[0]["conclusion"], "normal")

    def test_ac10_rule_version_from_context_and_unbound_insufficient(self):
        snap = snapshot()
        snap["rule_versions"]["S03"] = "s03-v7"
        clues = self._run([vacancy(12000, 12000)], context(snap=snap))
        self.assertEqual(clues[0]["rule_version"], "s03-v7")
        self.rules = AbsenceRules()
        clues = self._run([vacancy(12000, 12000, bound=False)])
        self.assertEqual((clues[0]["conclusion"], clues[0]["binding_status"], clues[0]["person_id"]), ("insufficient", "unbound", None))
        self.assertIn("binding_missing", clues[0]["quality_flags"])

    def test_ac11_person_id_never_from_track_or_seat_assignment(self):
        ctx = context()
        ctx["seat_assignments"] = {SEAT: "person-from-roster"}        # 工位分配不是身份
        clues = self._run([vacancy(12000, 12000)], ctx)
        self.assertIsNone(clues[0]["person_id"])
        self.assertEqual(clues[0]["track_id"], None)

    def test_ac12_unrelated_types_ignored_and_module_level_evaluate(self):
        self.assertEqual(self.rules.evaluate([obs("person_count", 0, {"count": 0})], context()), [])
        self.assertEqual(evaluate.input_types, INPUT_TYPES)
        self.assertEqual(evaluate([obs("person_count", 0, {"count": 0})], context()), [])

    # ---- 2026-10-19 独立复核回归 ----
    def test_r5_permission_normal_and_insufficient_emitted_once_per_absence(self):
        beats = [vacancy(10000 + i * 1000, 10000 + i * 1000) for i in range(4)]
        clues = self._run(beats, context(authorizations=[auth()]))
        self.assertEqual([c["conclusion"] for c in clues], ["normal"], "有许可的空置只出一条 normal")
        self.rules = AbsenceRules()
        personal = auth("leave-p", scope={"person_id": "person-7"})
        clues = self._run(beats, context(authorizations=[personal]))
        self.assertEqual([c["conclusion"] for c in clues], ["insufficient"], "缺身份只出一条 insufficient")
        self.rules = AbsenceRules()
        snap = snapshot([{"fact_id": "leave-1", "fact_version": 1}])
        clues = self._run(beats[:2], context(snap=snap, authorizations=[]))
        self.assertEqual([c["conclusion"] for c in clues], ["insufficient"])
        clues = self._run(beats[2:], context(snap=snap, authorizations=[auth()]))
        self.assertEqual([c["conclusion"] for c in clues], ["normal"], "事实内容补齐后结论变化再出一条")
        self.rules = AbsenceRules()
        clues = self._run(beats + [occupied(15000, 15000)], context(authorizations=[auth()]))
        self.assertEqual([c["conclusion"] for c in clues], ["normal", "normal"])
        self.assertIn("此前已出 normal 线索", clues[1]["reason"])

    def test_r6_exit_crossing_with_seat_scoped_permission_is_insufficient_not_suspected(self):
        crossed = obs("line_crossed", 4000, {"line_id": "door", "track_id": "track:run-1:3", "direction": "out"}, start_ms=3800)
        clues = self._run([crossed], context(authorizations=[auth()]))
        self.assertEqual(clues[0]["conclusion"], "insufficient")
        self.assertIn("工位级离岗许可", clues[0]["missing_facts"][0])
        self.rules = AbsenceRules()
        clues = self._run([crossed], context(authorizations=[auth("p", scope={"person_id": "person-7"})]))
        self.assertEqual(clues[0]["conclusion"], "insufficient")
        self.rules = AbsenceRules()
        clues = self._run([crossed], context(authorizations=[auth(valid_from=utc(0), valid_until=utc(1000))]))
        self.assertEqual(clues[0]["conclusion"], "suspected", "过期的工位级许可不算")

    def test_r7_authorization_scope_single_dimension_only(self):
        with self.assertRaises(ValueError):
            Authorization.from_dict(auth(scope={"seat_id": SEAT, "person_id": "person-7"}))

    def test_r8_rule_version_missing_raises(self):
        ctx = context()
        ctx["rule_version"] = None
        snap = dict(ctx["snapshot"], rule_versions={"S10": "s10-v1"})
        ctx["snapshot"] = snap
        with self.assertRaises(ValueError):
            self.rules.evaluate([vacancy(12000, 12000)], ctx)
        ctx["snapshot"] = dict(snap, rule_versions={"S03": "s03-v9", "S10": "s10-v1"})
        clues = self._run([vacancy(12000, 12000)], ctx)
        self.assertEqual(clues[0]["rule_version"], "s03-v9", "没给 rule_version 时从快照取")

    def test_r9_zone_left_after_track_lost_closes_open_clue(self):
        dwell = obs("zone_dwell_progress", 3000, {"zone_id": "restricted-1", "track_id": "t", "dwell_ms": 2500, "paused_ms": 0}, start_ms=500)
        self._run([dwell])
        lost = obs("zone_left", 6000, {"zone_id": "restricted-1", "track_id": "t", "dwell_ms": 3500, "left_at_ms": 4000}, start_ms=500, flags=("track_lost",))
        clues = self._run([lost])
        self.assertEqual(clues[0]["conclusion"], "normal")
        self.assertIn("丢失", clues[0]["reason"])

    def test_r10_unbound_direct_call_emits_once(self):
        beats = [vacancy(10000 + i * 1000, 10000 + i * 1000, bound=False) for i in range(3)]
        clues = self._run(beats)
        self.assertEqual(len(clues), 1)

    def test_config_threshold_and_authorization_validation(self):
        cfg = AbsenceRuleConfig(vacancy_ms=3000, boundary_ms=500)
        clues = self._run([vacancy(3000, 3000)], context(config=cfg))
        self.assertEqual(clues[0]["conclusion"], "suspected")
        with self.assertRaises(ValueError):
            AbsenceRuleConfig(vacancy_ms=0)
        for bad in (dict(auth(), kind="holiday"), dict(auth(), scope={}), dict(auth(), scope={"camera_id": "x"}),
                    dict(auth(), fact_version=0), dict(auth(), valid_until=utc(0), valid_from=utc(100)), "junk"):
            with self.assertRaises(ValueError):
                Authorization.from_dict(bad)
        with self.assertRaises(ValueError):
            AbsenceRules().evaluate([vacancy(12000, 12000)], context(authorizations=[dict(auth(), kind="holiday")]))


if __name__ == "__main__":
    unittest.main()
