"""执行卡 7.3：S05 跨区域接触。桩验证（构造 5.1 事实、6.x 桩事实；跨室只有协议夹具）。
运行（services/vision-worker 下）：PYTHONPATH=. python -m unittest discover -s tests -p test_cross_zone_rules_v2.py -v
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from worker.cross_zone_rules import CrossZoneConfig, CrossZoneRules, INPUT_TYPES, RULE_CROSS_ROOM, RULE_PROXIMITY, RULE_ZONE, cross_room_protocol, evaluate

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "contracts"))
from contract_validator import ContractValidator  # noqa: E402

T0 = datetime(2026, 10, 19, 2, 0, 0, tzinfo=timezone.utc)
SCOPE = {"project_id": "project-a", "room_id": "room-a", "session_id": "session-a"}
SOURCE, RUN = "camera-a", "run-1"


def utc(ms):
    return (T0 + timedelta(milliseconds=ms)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def obs(kind, ms, payload, *, start_ms=None, bound=True, source=SOURCE, run=RUN, flags=()):
    obs_id = f"{source}:{run}:{kind}:{payload.get('zone_id') or payload.get('line_id') or 'all'}:{payload.get('track_id') or ''}:{ms}"
    scope = SCOPE if bound else {"project_id": None, "room_id": None, "session_id": None}
    base = {"contract_version": "2.0.0", **scope, "source_id": source, "observation_id": obs_id, "occurred_at": utc(ms), "media_run_id": run,
            "asset_id": "asset-1", "start_ms": ms if start_ms is None else start_ms, "end_ms": ms, "type": kind,
            "payload": {"config_version": 3, "track_ids": [], **payload}, "model_name": "zone_observer", "model_version": "5.1-v1",
            "quality_flags": list(flags),
            "evidence_refs": [{"contract_version": "2.0.0", **scope, "source_id": source, "evidence_id": f"{obs_id}:video", "media_run_id": run,
                               "asset_id": "asset-1", "evidence_type": "video", "start_ms": ms if start_ms is None else start_ms, "end_ms": ms,
                               "sha256": None, "status": "pending"}]}
    if bound:
        base.update(stage_snapshot_id="snap-1", phase_id="DETAILED", phase_version=1, binding_status="bound")
    else:
        base.update(stage_snapshot_id=None, phase_id=None, phase_version=None, binding_status="unbound")
        base["quality_flags"].append("binding_missing")
    return base


def dwell(ms, d, track="track:run-1:1", zone="restricted-1", **kw):
    return obs("zone_dwell_progress", ms, {"zone_id": zone, "track_id": track, "dwell_ms": d, "paused_ms": 0}, start_ms=ms - d, **kw)


def left(ms, d, track="track:run-1:1", zone="restricted-1", **kw):
    return obs("zone_left", ms, {"zone_id": zone, "track_id": track, "dwell_ms": d}, start_ms=ms - d, **kw)


def count(ms, tracks, **kw):
    return obs("person_count", ms, {"count": len(tracks), "tracks": [{"track_id": t, "bbox": b} for t, b in tracks], "track_ids": [t for t, _ in tracks]}, **kw)


def auth(fact_id="zone-ok", kind="zone_access", scope=None):
    return {"fact_id": fact_id, "fact_version": 1, "kind": kind, "scope": scope or {"zone_id": "restricted-1"}, "valid_from": utc(-60000), "valid_until": utc(600000)}


def context(authorizations=(), identities=None, rule_version="s05-v1", room_sources=None):
    auths = list(authorizations)
    snap = {**SCOPE, "source_id": SOURCE, "stage_snapshot_id": "snap-1", "phase_id": "DETAILED", "phase_version": 1,
            "rule_versions": {"S05": rule_version, "S10": "s10-v1"} if rule_version else {"S10": "s10-v1"},
            "exceptions": [{"fact_id": a["fact_id"], "fact_version": a["fact_version"]} for a in auths]}
    ctx = {"snapshot": snap, "scene_id": "S05", "rule_version": rule_version, "exceptions": snap["exceptions"], "authorizations": auths,
           "identities": identities or {}}
    if room_sources is not None:
        ctx["room_sources"] = room_sources
    return ctx


IDENT = {"track:run-1:1": {"person_id": "person-1", "fact_id": "v1", "fact_version": 1}, "track:run-1:2": {"person_id": "person-2", "fact_id": "v2", "fact_version": 1}}
NEAR = [("track:run-1:1", [0.40, 0.40, 0.46, 0.60]), ("track:run-1:2", [0.44, 0.40, 0.50, 0.60])]      # 中心相距 0.04
FAR = [("track:run-1:1", [0.10, 0.40, 0.16, 0.60]), ("track:run-1:2", [0.70, 0.40, 0.76, 0.60])]


class CrossZoneRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = ContractValidator()

    def setUp(self):
        self.rules = CrossZoneRules()

    def _run(self, observations, ctx=None):
        ctx = ctx or context()
        out = []
        for o in observations:
            out.extend(self.rules.evaluate([o], ctx))
        for clue in out:
            self.assertEqual(self.validator.validate_object("risk-clue", clue), [], clue["reason"])
        return out

    def test_ac1_zone_dwell_three_states_once(self):
        clues = self._run([dwell(1000, 1000), dwell(2000, 2000), dwell(3000, 3000), left(5000, 5000)])
        self.assertEqual([(c["conclusion"], c["risk_level"], c["rule_id"]) for c in clues], [("suspected", "medium", RULE_ZONE), ("normal", "none", RULE_ZONE)])
        self.assertIn("不证明跨室", clues[0]["reason"])
        self.assertEqual((clues[1]["start_ms"], clues[1]["end_ms"]), (0, 5000))
        self.rules = CrossZoneRules()
        allowed = self._run([dwell(2000, 2000)], context(authorizations=[auth()]))
        self.assertEqual(allowed[0]["conclusion"], "normal")
        self.rules = CrossZoneRules()
        personal = self._run([dwell(2000, 2000)], context(authorizations=[auth("p", scope={"person_id": "person-1"})]))
        self.assertEqual(personal[0]["conclusion"], "insufficient")
        self.rules = CrossZoneRules()
        verified = self._run([dwell(2000, 2000)], context(authorizations=[auth("p", scope={"person_id": "person-1"})], identities=IDENT))
        self.assertEqual((verified[0]["conclusion"], verified[0]["person_id"]), ("normal", "person-1"))

    def test_ac2_proximity_three_states(self):
        frames = [count(ms, NEAR) for ms in (0, 1000, 2000, 3000, 4000, 5000, 6000)]
        clues = self._run(frames, context(identities=IDENT))
        self.assertEqual([(c["conclusion"], c["risk_level"], c["rule_id"]) for c in clues], [("suspected", "low", RULE_PROXIMITY)])
        self.assertEqual((clues[0]["start_ms"], clues[0]["end_ms"], clues[0]["person_id"]), (0, 5000, "person-1"))
        apart = self._run([count(7000, FAR)], context(identities=IDENT))
        self.assertEqual([c["conclusion"] for c in apart], ["normal"])
        self.assertIn("分开", apart[0]["reason"])
        self.rules = CrossZoneRules()
        unknown = self._run(frames, context())
        self.assertEqual(unknown[0]["conclusion"], "insufficient")
        self.assertIn("身份未经核验", unknown[0]["missing_facts"][0])
        self.rules = CrossZoneRules()
        allowed = self._run(frames, context(authorizations=[auth("contact", kind="contact_allowed", scope={"room_id": "room-a"})], identities=IDENT))
        self.assertEqual(allowed[0]["conclusion"], "normal")
        self.rules = CrossZoneRules()
        self.assertEqual(self._run([count(ms, FAR) for ms in (0, 3000, 6000)], context(identities=IDENT)), [], "不接近不出")
        self.rules = CrossZoneRules()
        self.assertEqual(self._run([count(0, NEAR), count(3000, NEAR)], context(identities=IDENT)), [], "不足 5 秒不出")

    def test_ac3_single_source_cross_room_is_insufficient(self):
        clues = cross_room_protocol({SOURCE: [dwell(3000, 3000)]}, context(identities=IDENT, room_sources={SOURCE: "room-a"}))
        self.assertEqual([(c["conclusion"], c["rule_id"]) for c in clues], [("insufficient", RULE_CROSS_ROOM)])
        self.assertIn("第二评标室机位的同一人员轨迹（单路视频无法证明跨室）", clues[0]["missing_facts"])
        self.assertIn("single_source", clues[0]["quality_flags"])
        self.assertEqual(self.validator.validate_object("risk-clue", clues[0]), [])
        none = cross_room_protocol({SOURCE: [dwell(3000, 3000)]}, context(identities=IDENT))
        self.assertEqual(none[0]["conclusion"], "insufficient", "没给 room_sources 也按单源")

    def test_ac4_multi_source_protocol_fixture(self):
        other = dwell(60000, 3000, track="track:run-2:9", zone="restricted-b", source="camera-b", run="run-2")
        other["room_id"] = "room-b"
        other["evidence_refs"][0]["room_id"] = "room-b"
        identities = dict(IDENT, **{"track:run-2:9": {"person_id": "person-1", "fact_id": "v9", "fact_version": 1}})
        ctx = context(identities=identities, room_sources={SOURCE: "room-a", "camera-b": "room-b"})
        clues = cross_room_protocol({SOURCE: [dwell(3000, 3000)], "camera-b": [other]}, ctx)
        self.assertEqual([(c["conclusion"], c["risk_level"], c["person_id"]) for c in clues], [("suspected", "medium", "person-1")])
        self.assertIn("protocol_fixture", clues[0]["quality_flags"])
        self.assertIn("未在真实多机位验证", clues[0]["reason"])
        self.assertEqual(len(clues[0]["observation_ids"]), 2)
        self.assertEqual(self.validator.validate_object("risk-clue", clues[0]), [])
        ctx_ok = context(authorizations=[auth("b-ok", scope={"zone_id": "restricted-b"})], identities=identities, room_sources={SOURCE: "room-a", "camera-b": "room-b"})
        self.assertEqual(cross_room_protocol({SOURCE: [dwell(3000, 3000)], "camera-b": [other]}, ctx_ok)[0]["conclusion"], "normal")
        self.assertEqual(cross_room_protocol({SOURCE: [dwell(3000, 3000)], "camera-b": [other]}, context(room_sources={SOURCE: "room-a", "camera-b": "room-b"})), [],
                         "身份未核验不做跨室关联")

    def test_review_m1_m6_l1_personal_permit_missing_fact_degraded_and_bad_track(self):
        other = dwell(60000, 3000, track="track:run-2:9", zone="restricted-b", source="camera-b", run="run-2")
        other["room_id"] = "room-b"
        other["evidence_refs"][0]["room_id"] = "room-b"
        identities = dict(IDENT, **{"track:run-2:9": {"person_id": "person-1", "fact_id": "v9", "fact_version": 1}})
        rooms = {SOURCE: "room-a", "camera-b": "room-b"}
        personal = context(authorizations=[auth("p-ok", scope={"person_id": "person-1"})], identities=identities, room_sources=rooms)
        self.assertEqual(cross_room_protocol({SOURCE: [dwell(3000, 3000)], "camera-b": [other]}, personal)[0]["conclusion"], "normal",
                         "第二室给本人的个人区域权限要能套上")
        missing = context(identities=identities, room_sources=rooms)
        missing["exceptions"] = [{"fact_id": "gone", "fact_version": 1}]
        clue = cross_room_protocol({SOURCE: [dwell(3000, 3000)], "camera-b": [other]}, missing)[0]
        self.assertEqual(clue["conclusion"], "insufficient", "快照引用的权限事实内容缺失：不能判 suspected")
        self.assertEqual(self.validator.validate_object("risk-clue", clue), [])
        # 故障帧不能累计接近时长
        clues = self._run([count(0, NEAR), count(1000, NEAR, flags=("camera_degraded",)), count(3000, NEAR, flags=("camera_degraded",)), count(5000, NEAR)],
                          context(identities=IDENT))
        self.assertEqual(clues, [], "只看到 0 秒和 5 秒两帧，故障帧之间不算持续接近")
        # 轨迹缺 track_id 不让规则崩
        broken = count(0, NEAR)
        broken["payload"]["tracks"].append({"bbox": [0.41, 0.4, 0.47, 0.6]})
        self.assertEqual(CrossZoneRules().evaluate([broken], context(identities=IDENT)), [])

    def test_ac9_contract_rule_version_unbound_reset_and_degraded(self):
        clue = self._run([dwell(2000, 2000)], context(rule_version="s05-v2"))[0]
        self.assertEqual(clue["rule_version"], "s05-v2")
        with self.assertRaises(ValueError):
            self.rules.evaluate([dwell(2000, 2000)], context(rule_version=None))
        self.rules = CrossZoneRules()
        unbound = self._run([dwell(2000, 2000, bound=False), dwell(3000, 3000, bound=False)])
        self.assertEqual([(c["conclusion"], c["binding_status"]) for c in unbound], [("insufficient", "unbound")])
        self.rules = CrossZoneRules()
        self.assertEqual(self._run([dwell(2000, 2000, flags=("camera_degraded",))]), [], "故障帧不判")
        self.rules = CrossZoneRules()
        self._run([dwell(2000, 2000)])
        again = self._run([dwell(2000, 2000, run="run-2")])
        self.assertEqual(len(again), 1, "新轮次重新出")
        self.assertEqual(self.rules.evaluate([{"type": "visual_fact"}], context()), [])
        self.assertEqual(evaluate.input_types, INPUT_TYPES)
        with self.assertRaises(ValueError):
            CrossZoneConfig(proximity_distance=1.5)

    def test_ac10_same_segment_emits_once_and_fixture_normal(self):
        clues = self._run([dwell(2000, 2000), dwell(3000, 3000), dwell(4000, 4000)])
        self.assertEqual(len(clues), 1)
        ctx = context(authorizations=[auth()])
        self.rules = CrossZoneRules()
        clues = self._run([dwell(2000, 2000), dwell(3000, 3000)], ctx)
        self.assertEqual([c["conclusion"] for c in clues], ["normal"], "有权限也只出一条 normal")


if __name__ == "__main__":
    unittest.main()
