"""执行卡 2.3：派发器与事件适配器。

桩验证：桩规则、SQLite 临时库（设 EVENT_TEST_DATABASE_URL 走真实 PG 随机 schema）、无 Redis。
运行（services/event-service 下）：PYTHONPATH=. python -m unittest discover -s tests -p test_stage_dispatcher_v2.py -v
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from service.clue_models import ClueProcessingRecord, ObservationReceipt, RiskClueRecord
from service.event_adapter import EventAdapter, event_message, gap_id_of, persist_clue, process_observation
from service.models import Base, Decision, Event
from service.stage_dispatcher import (
    DispatchError, HandlerFailed, S10_RULE_ID, dispatch, new_clue, s10_health_rule, validate_clue,
)

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "contracts"))
from contract_validator import ContractValidator  # noqa: E402

SCOPE = {"project_id": "project-a", "room_id": "room-a", "session_id": "session-a"}
SOURCE = "camera-a"


def snapshot(version=1, enabled=("S03",), rules=None, source=SOURCE, effective="2026-10-19T02:00:00Z", snap_id=None):
    rules = rules or {s: f"{s.lower()}-v1" for s in (*enabled, "S10")}
    return {"contract_version": "2.0.0", **SCOPE, "source_id": source, "stage_snapshot_id": snap_id or f"snap-{version}",
            "phase_id": "DETAILED", "phase_version": version, "effective_at": effective, "enabled_scenes": list(enabled),
            "always_on_scenes": ["S10"], "assets": [], "rule_versions": rules, "exceptions": [], "sync_status": "pending"}


def evidence(obs_id, run="run-1", kind="video", status="ready", source=SOURCE):
    return {"contract_version": "2.0.0", **SCOPE, "source_id": source, "evidence_id": f"{obs_id}:evidence", "media_run_id": run,
            "asset_id": "asset-1", "evidence_type": kind, "start_ms": 12000 if kind != "gap" else None,
            "end_ms": 15000 if kind != "gap" else None,
            "sha256": "a" * 64 if status == "ready" else None, "status": status}


def observation(kind="person_detected", *, obs_id=None, bound=True, snap=None, occurred="2026-10-19T02:00:12Z", run="run-1",
                payload=None, flags=(), refs=None, source=SOURCE):
    snap = snap or snapshot()
    obs_id = obs_id or f"obs-{uuid4().hex[:8]}"
    base = {"contract_version": "2.0.0", "source_id": source, "observation_id": obs_id, "occurred_at": occurred,
            "media_run_id": run, "asset_id": "asset-1", "start_ms": 12000, "end_ms": 15000, "type": kind,
            "payload": payload if payload is not None else {"count": 1}, "model_name": "stub", "model_version": "v1",
            "quality_flags": list(flags), "evidence_refs": refs if refs is not None else [evidence(obs_id, run, source=source)]}
    if bound:
        base.update(SCOPE)
        base.update({"stage_snapshot_id": snap["stage_snapshot_id"], "phase_id": snap["phase_id"], "phase_version": snap["phase_version"],
                     "binding_status": "bound"})
    else:
        base.update({"project_id": None, "room_id": None, "session_id": None, "stage_snapshot_id": None, "phase_id": None,
                     "phase_version": None, "binding_status": "unbound"})
        if "binding_missing" not in base["quality_flags"]:
            base["quality_flags"].append("binding_missing")
    return base


def gap_observation(kind="video_offline", gap_id="gap:camera-a:run-1:offline:12000", **kw):
    obs_id = kw.pop("obs_id", f"obs-{uuid4().hex[:8]}")
    ref = {"contract_version": "2.0.0", **SCOPE, "source_id": SOURCE, "evidence_id": f"{gap_id}:evidence", "media_run_id": "run-1",
           "asset_id": "asset-1", "evidence_type": "gap", "start_ms": None, "end_ms": None, "sha256": None, "status": "missing"}
    if not kw.get("bound", True):
        ref.update({"project_id": None, "room_id": None, "session_id": None})
    return observation(kind, obs_id=obs_id, payload={"gap_id": gap_id, "gap_media_run_id": "run-1", "technical_reason": "no_frame"},
                       refs=[ref], **kw)


class StubRule:
    def __init__(self, scene, *, input_types=None, conclusion="suspected", fail=False, rule_version=None):
        self.scene = scene
        self.calls = []
        self.conclusion = conclusion
        self.fail = fail
        self.rule_version = rule_version
        if input_types is not None:
            self.input_types = set(input_types)

    def __call__(self, observations, context):
        self.calls.append((list(observations), context))
        if self.fail:
            raise RuntimeError("规则内部错误")
        obs = observations[0]
        risk = "medium" if self.conclusion == "suspected" else "none"
        return [new_clue(obs, scene_id=self.scene, rule_id=f"{self.scene.lower()}-rule", rule_version=self.rule_version or context["rule_version"],
                         conclusion=self.conclusion, risk_level=risk, reason="桩规则结论", missing_facts=["缺事实"] if self.conclusion == "insufficient" else ())]


class DispatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = ContractValidator()

    def test_ac1_disabled_scene_not_invoked_enabled_invoked_once(self):
        rule = StubRule("S03")
        clues = dispatch(observation("seat_absence", snap=snapshot(enabled=())), snapshot(enabled=()), {"S03": rule})
        self.assertEqual(clues, [])
        self.assertEqual(rule.calls, [])
        clues = dispatch(observation("seat_absence"), snapshot(), {"S03": rule})
        self.assertEqual(len(rule.calls), 1)
        self.assertEqual([c["scene_id"] for c in clues], ["S03"])
        self.assertEqual(rule.calls[0][1]["rule_version"], "s03-v1")
        self.assertEqual(self.validator.validate_object("risk-clue", clues[0]), [])

    def test_ac2_person_detected_only_reaches_rules_declaring_it(self):
        s01 = StubRule("S01", input_types={"device_at_entry"})
        s02 = StubRule("S02", input_types={"phone_in_hand"})
        s03 = StubRule("S03", input_types={"person_detected", "seat_absence"})
        s05 = StubRule("S05")   # 没声明类型：场景启用时都调，由规则自己判断
        snap = snapshot(enabled=("S01", "S02", "S03", "S05"))
        clues = dispatch(observation("person_detected", snap=snap), snap, {"S01": s01, "S02": s02, "S03": s03, "S05": s05})
        self.assertEqual((len(s01.calls), len(s02.calls), len(s03.calls), len(s05.calls)), (0, 0, 1, 1))
        self.assertEqual(sorted(c["scene_id"] for c in clues), ["S03", "S05"])

    def test_ac4_new_observation_types_are_not_dropped(self):
        s01 = StubRule("S01", input_types={"device_at_entry"})
        snap = snapshot(enabled=("S01",))
        clues = dispatch(observation("device_at_entry", snap=snap), snap, {"S01": s01})
        self.assertEqual(len(clues), 1)

    def test_ac3_recovery_bypasses_scene_filter_and_yields_normal(self):
        snap = snapshot(enabled=())
        rule = StubRule("S03")
        clues = dispatch(gap_observation("video_online", snap=snap), snap, {"S03": rule})
        self.assertEqual(rule.calls, [])
        self.assertEqual([(c["scene_id"], c["conclusion"], c["risk_level"]) for c in clues], [("S10", "normal", "none")])
        self.assertEqual(self.validator.validate_object("risk-clue", clues[0]), [])
        opened = dispatch(gap_observation("video_offline", snap=snap), snap, {})
        self.assertEqual([(c["conclusion"], c["risk_level"], c["rule_id"]) for c in opened], [("suspected", "medium", S10_RULE_ID)])
        self.assertEqual(opened[0]["rule_version"], "s10-v1")
        unknown = dispatch(gap_observation("camera_blacked", snap=snap, flags=("context_incomplete",)), snap, {})
        self.assertEqual((unknown[0]["conclusion"], unknown[0]["missing_facts"]), ("insufficient", ["缺健康采样或故障起点，缺口范围未知"]))
        custom = StubRule("S10", conclusion="insufficient", input_types={"video_offline"})
        custom_clues = dispatch(gap_observation("video_offline", snap=snap), snap, {"S10": custom})
        self.assertEqual(len(custom.calls), 1)
        self.assertEqual(custom_clues[0]["conclusion"], "insufficient")

    def test_ac5_scope_and_snapshot_checks(self):
        snap = snapshot()
        other_session = observation(snap=snap)
        other_session["session_id"] = "session-b"
        with self.assertRaises(DispatchError) as ctx:
            dispatch(other_session, snap, {"S03": StubRule("S03")})
        self.assertEqual(ctx.exception.code, "SCOPE_MISMATCH")
        with self.assertRaises(DispatchError) as ctx:
            dispatch(observation(snap=snap, source="camera-b"), snap, {})
        self.assertEqual(ctx.exception.code, "SCOPE_MISMATCH")
        wrong_snap = observation(snap=snap)
        wrong_snap["stage_snapshot_id"] = "snap-other"
        with self.assertRaises(DispatchError) as ctx:
            dispatch(wrong_snap, snap, {})
        self.assertEqual(ctx.exception.code, "SNAPSHOT_MISMATCH")
        with self.assertRaises(DispatchError) as ctx:
            dispatch(observation(snap=snap, occurred="2026-10-19T01:59:59Z"), snap, {})
        self.assertEqual(ctx.exception.code, "SNAPSHOT_NOT_EFFECTIVE")
        session_level = snapshot(source=None)
        session_level["source_id"] = None
        self.assertEqual(dispatch(observation(snap=session_level), session_level, {}), [])
        with self.assertRaises(DispatchError) as ctx:
            dispatch({"type": "x"}, snap, {})
        self.assertEqual(ctx.exception.code, "OBSERVATION_INVALID")
        with self.assertRaises(DispatchError):
            dispatch(observation(snap=snap), snap, {"S99": StubRule("S03")})

    def test_ac6_unbound_skips_scene_rules_and_s10_is_insufficient(self):
        rule = StubRule("S03")
        self.assertEqual(dispatch(observation("seat_absence", bound=False), snapshot(), {"S03": rule}), [])
        with self.assertRaises(DispatchError) as ctx:
            dispatch(observation("seat_absence"), None, {"S03": rule})
        self.assertEqual(ctx.exception.code, "SNAPSHOT_MISSING")
        self.assertEqual(rule.calls, [])
        clues = dispatch(gap_observation("video_offline", bound=False), None, {})
        self.assertEqual((clues[0]["conclusion"], clues[0]["risk_level"], clues[0]["person_id"], clues[0]["binding_status"]),
                         ("insufficient", "none", None, "unbound"))
        self.assertIn("binding_missing", clues[0]["quality_flags"])
        self.assertEqual(self.validator.validate_object("risk-clue", clues[0]), [])

    def test_ac7_handler_exception_is_processing_failure(self):
        with self.assertRaises(HandlerFailed) as ctx:
            dispatch(observation("seat_absence"), snapshot(), {"S03": StubRule("S03", fail=True)})
        self.assertEqual(ctx.exception.scene_id, "S03")

    def test_ac10_clue_validation(self):
        snap = snapshot()
        obs = observation("seat_absence", snap=snap)
        with self.assertRaises(DispatchError) as ctx:
            dispatch(obs, snap, {"S03": StubRule("S03", rule_version="s03-v9")})
        self.assertEqual(ctx.exception.code, "RULE_VERSION_MISMATCH")
        good = new_clue(obs, scene_id="S03", rule_id="r", rule_version="s03-v1", conclusion="suspected", risk_level="medium", reason="x")
        self.assertIsNone(validate_clue(good, snap, "S03", obs))
        no_evidence = dict(good, evidence_refs=[])
        with self.assertRaises(DispatchError):
            validate_clue(no_evidence, snap, "S03", obs)
        bad_level = dict(good, risk_level="none")
        with self.assertRaises(DispatchError):
            validate_clue(bad_level, snap, "S03", obs)
        disabled = dict(good, scene_id="S05", rule_version="s05-v1")
        with self.assertRaises(DispatchError) as ctx:
            validate_clue(disabled, snap, "S05", obs)
        self.assertEqual(ctx.exception.code, "SCENE_DISABLED")
        with self.assertRaises(DispatchError):
            dispatch(obs, snap, {"S03": lambda observations, context: "not a list"})

    def test_ac11_new_clue_from_unbound_forces_insufficient(self):
        obs = observation("seat_absence", bound=False)
        clue = new_clue(obs, scene_id="S03", rule_id="r", rule_version="s03-v1", conclusion="suspected", risk_level="high",
                        reason="x", person_id="someone")
        self.assertEqual((clue["conclusion"], clue["risk_level"], clue["person_id"], clue["missing_facts"]),
                         ("insufficient", "none", None, ["上下文不完整"]))
        self.assertEqual(self.validator.validate_object("risk-clue", clue), [])

    def test_r1_bound_observation_without_snapshot_is_processing_failure_not_suspected(self):
        obs = gap_observation("video_offline")
        obs["stage_snapshot_id"] = "snap-does-not-exist"
        with self.assertRaises(DispatchError) as ctx:
            dispatch(obs, None, {})
        self.assertEqual(ctx.exception.code, "SNAPSHOT_MISSING")

    def test_r4_validate_clue_rejects_occurred_at_drift_and_foreign_evidence(self):
        snap = snapshot()
        obs = observation("seat_absence", snap=snap)
        good = new_clue(obs, scene_id="S03", rule_id="r", rule_version="s03-v1", conclusion="suspected", risk_level="medium", reason="x")
        drift = dict(good, occurred_at="2026-10-19T02:00:20Z")
        with self.assertRaises(DispatchError):
            validate_clue(drift, snap, "S03", obs)
        foreign = dict(good, evidence_refs=[dict(good["evidence_refs"][0], media_run_id="run-other")])
        with self.assertRaises(DispatchError):
            validate_clue(foreign, snap, "S03", obs)
        dup_ids = dict(good, observation_ids=[obs["observation_id"], obs["observation_id"]])
        with self.assertRaises(DispatchError):
            validate_clue(dup_ids, snap, "S03", obs)
        empty_flag = dict(good, quality_flags=[""])
        with self.assertRaises(DispatchError):
            validate_clue(empty_flag, snap, "S03", obs)
        null_scope = dict(good, session_id=None)
        with self.assertRaises(DispatchError):
            validate_clue(null_scope, None, "S03")
        bad_phase = dict(good, phase_version=True)
        with self.assertRaises(DispatchError):
            validate_clue(bad_phase, None, "S03")

    def test_r6_invalid_calendar_date_is_dispatch_error(self):
        obs = observation("seat_absence", occurred="2026-13-45T00:00:00Z")
        with self.assertRaises(DispatchError) as ctx:
            dispatch(obs, snapshot(), {})
        self.assertEqual(ctx.exception.code, "TIME_INVALID")

    def test_r7_custom_s10_rule_input_types_respected(self):
        snap = snapshot(enabled=())
        custom = StubRule("S10", conclusion="insufficient", input_types={"video_offline"})
        clues = dispatch(gap_observation("camera_blacked", snap=snap), snap, {"S10": custom})
        self.assertEqual(custom.calls, [], "声明只吃 video_offline 的规则不被喂 camera_blacked")
        self.assertEqual([(c["conclusion"], c["rule_id"]) for c in clues], [("suspected", S10_RULE_ID)], "回落内置规则")

    def test_s10_fixture_observations_produce_expected_conclusions(self):
        for name, expected in (("S10-suspected", "suspected"), ("S10-normal", "normal"), ("S10-insufficient", "insufficient")):
            fixture = REPO / "docs/contracts/fixtures" / f"{name}.json"
            if not fixture.is_file():
                self.skipTest("当前代码分发不含独立契约测试资料")
            bundle = json.loads(fixture.read_text(encoding="utf-8"))
            snap = bundle["phase_configs"][0]
            clues = []
            for obs in bundle["observations"]:
                clues.extend(dispatch(obs, snap, {}))
            self.assertEqual(clues[-1]["conclusion"], expected, name)
            for clue in clues:
                self.assertEqual(self.validator.validate_object("risk-clue", clue), [], name)


class AdapterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = [Event.__table__, Decision.__table__, RiskClueRecord.__table__, ClueProcessingRecord.__table__, ObservationReceipt.__table__]
        url = os.environ.get("EVENT_TEST_DATABASE_URL")
        cls.tmp = None
        if url:
            cls.backend = "postgresql"
            cls.admin = create_engine(url)
            cls.schema = "test_dispatch_v2_" + uuid4().hex
            with cls.admin.begin() as connection:
                connection.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
            cls.engine = create_engine(url, connect_args={"options": f"-csearch_path={cls.schema}"})
        else:
            cls.backend = "sqlite"
            cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
            cls.engine = create_engine(f"sqlite:///{Path(cls.tmp.name) / 'events.db'}")
        Base.metadata.create_all(cls.engine, tables=cls.tables)
        cls.Session = sessionmaker(bind=cls.engine)
        cls.validator = ContractValidator()

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        if cls.backend == "postgresql":
            with cls.admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
            cls.admin.dispose()
        elif cls.tmp is not None:
            cls.tmp.cleanup()

    def setUp(self):
        with self.Session.begin() as db:
            for table in reversed(self.tables):
                db.execute(table.delete())
        self.published = []
        self.adapter = EventAdapter(self.Session, cooldown_seconds=5, publisher=self.published.append)

    def _count(self, model):
        with self.Session() as db:
            return len(db.scalars(select(model)).all())

    def _clue(self, **kw):
        obs = observation("seat_absence", **{k: v for k, v in kw.items() if k in ("obs_id", "occurred", "run", "source")})
        return new_clue(obs, scene_id="S03", rule_id="s03-rule", rule_version="s03-v1", conclusion=kw.get("conclusion", "suspected"),
                        risk_level="medium" if kw.get("conclusion", "suspected") == "suspected" else "none", reason="工位空置超过阈值",
                        missing_facts=["缺事实"] if kw.get("conclusion") == "insufficient" else (), seat_id="seat-1")

    def test_ac8_suspected_creates_pending_event_idempotently(self):
        clue = self._clue()
        clue_id = self.adapter.persist_clue(clue)
        self.assertEqual(clue_id, clue["clue_id"])
        self.assertEqual(self.adapter.persist_clue(clue), clue_id)
        self.assertEqual(persist_clue(clue, self.Session), clue_id)
        self.assertEqual((self._count(RiskClueRecord), self._count(Event)), (1, 1))
        with self.Session() as db:
            row = db.scalar(select(RiskClueRecord))
            event = db.scalar(select(Event))
        self.assertEqual(row.event_id, event.event_id)
        self.assertEqual((event.status, event.skill_code, event.skill_version, event.event_type, event.camera_id, event.seat_id),
                         ("PENDING", "s03-rule", "s03-v1", "s03-rule", SOURCE, "seat-1"))
        self.assertEqual(event.occurred_at, datetime(2026, 10, 19, 2, 0, 12))
        self.assertEqual(len(self.published), 1)
        self.assertEqual(self.published[0]["event_id"], event.event_id)
        self.assertEqual(set(self.published[0]) , set(event_message(event)))

    def test_ac8_cooldown_buckets_by_session_source_scene_rule_run(self):
        first = self._clue(occurred="2026-10-19T02:00:12Z")
        self.adapter.persist_clue(first)
        within = self._clue(occurred="2026-10-19T02:00:15Z")
        self.adapter.persist_clue(within)
        self.assertEqual(self._count(Event), 1, "冷却窗内复用同一事件")
        with self.Session() as db:
            rows = {r.clue_id: r for r in db.scalars(select(RiskClueRecord)).all()}
        self.assertEqual(rows[within["clue_id"]].event_id, rows[first["clue_id"]].event_id)
        self.assertEqual(rows[within["clue_id"]].deduped_from, first["clue_id"])
        self.adapter.persist_clue(self._clue(occurred="2026-10-19T02:00:30Z"))
        self.assertEqual(self._count(Event), 2, "超出冷却窗建新事件")
        self.adapter.persist_clue(self._clue(occurred="2026-10-19T02:00:31Z", source="camera-b"))
        self.assertEqual(self._count(Event), 3, "换源不互相压")
        self.adapter.persist_clue(self._clue(occurred="2026-10-19T02:00:31Z", run="run-2"))
        self.assertEqual(self._count(Event), 4, "换媒体轮次不互相压")
        self.assertEqual(self._count(RiskClueRecord), 5, "线索一条不少")

    def test_ac9_normal_and_insufficient_do_not_create_events(self):
        self.adapter.persist_clue(self._clue(conclusion="normal"))
        self.adapter.persist_clue(self._clue(conclusion="insufficient"))
        self.assertEqual((self._count(RiskClueRecord), self._count(Event)), (2, 0))
        self.assertEqual(self.published, [])

    def test_ac3_recovery_closes_gap_events_only_for_same_gap(self):
        snap = snapshot(enabled=())
        gap_a, gap_b = "gap:camera-a:run-1:offline:12000", "gap:camera-a:run-1:quality:20000"
        for gap in (gap_a, gap_b):
            for clue in dispatch(gap_observation("video_offline" if gap == gap_a else "camera_blacked", gap_id=gap, snap=snap), snap, {}):
                self.adapter.persist_clue(clue)
        self.assertEqual(self._count(Event), 2)
        recovered = dispatch(gap_observation("video_online", gap_id=gap_a, snap=snap, occurred="2026-10-19T02:00:40Z"), snap, {})
        self.adapter.persist_clue(recovered[0])
        with self.Session() as db:
            events = {e.camera_id + ":" + e.status: e for e in db.scalars(select(Event)).all()}
            statuses = sorted(e.status for e in db.scalars(select(Event)).all())
            decisions = db.scalars(select(Decision)).all()
        self.assertEqual(statuses, ["CLOSED", "PENDING"])
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, "auto_close")
        self.assertEqual(self._count(RiskClueRecord), 3, "恢复线索自己也留下")
        self.assertEqual(self.adapter.close_gap_events(SCOPE["session_id"], SOURCE, gap_a), 0, "已关的不重复关")

    def test_r2_concurrent_replay_leaves_no_orphan_event(self):
        clue = self._clue()
        adapter = EventAdapter(self.Session, cooldown_seconds=5)
        original = adapter._cooldown_twin

        def race(db, value, occurred, gap):
            EventAdapter(self.Session, cooldown_seconds=0).persist_clue(value)   # 另一进程抢先写入同一 clue_id
            return original(db, value, occurred, gap)

        adapter._cooldown_twin = race
        adapter.persist_clue(clue)
        with self.Session() as db:
            events = db.scalars(select(Event)).all()
            referenced = {r.event_id for r in db.scalars(select(RiskClueRecord)).all()}
        self.assertEqual(len(events), 1)
        self.assertTrue(all(e.event_id in referenced for e in events), "每条事件都被某条线索引用，没有孤儿")

    def test_r3_gap_id_only_from_structured_channel(self):
        snap = snapshot(enabled=())
        opened = dispatch(gap_observation("video_offline", snap=snap), snap, {})[0]
        tricky = dict(opened, reason="设备故障 缺口 gap:forged:1；只是技术事实", evidence_refs=[evidence(opened["observation_ids"][0], kind="video")])
        tricky["clue_id"] = "clue:tricky"
        self.adapter.persist_clue(tricky)
        with self.Session() as db:
            row = db.scalar(select(RiskClueRecord).where(RiskClueRecord.clue_id == "clue:tricky"))
        self.assertIsNone(row.gap_id, "自述文本里的「缺口 …」不被解析")
        self.adapter.persist_clue(opened)
        with self.Session() as db:
            row = db.scalar(select(RiskClueRecord).where(RiskClueRecord.clue_id == opened["clue_id"]))
        self.assertEqual(row.gap_id, "gap:camera-a:run-1:offline:12000", "结构化通道 1：gap 型证据的 evidence_id")
        recovered_obs = gap_observation("video_online", snap=snap, occurred="2026-10-19T02:00:40Z")
        recovered_obs["evidence_refs"] = [evidence(recovered_obs["observation_id"], kind="video")]
        recovered = dispatch(recovered_obs, snap, {})[0]
        self.adapter.persist_clue(recovered, gap_id=gap_id_of(recovered_obs), close_gap=True)
        with self.Session() as db:
            statuses = sorted(e.status for e in db.scalars(select(Event)).all())
        self.assertEqual(statuses, ["CLOSED", "PENDING"], "结构化通道 2：调用方传 payload.gap_id；tricky 的事件仍 PENDING")

    def test_r8_cooldown_anchors_on_chain_head_not_last(self):
        for n, second in enumerate(range(12, 60, 4)):
            self.adapter.persist_clue(self._clue(occurred=f"2026-10-19T02:00:{second:02d}Z", obs_id=f"obs-{n}"))
        self.assertGreaterEqual(self._count(Event), 5, "链首锚定：5 秒冷却不会把 48 秒的连续线索归并成一条事件")

    def test_ac7_failure_recorded_without_clue_or_event(self):
        obs = observation("seat_absence")
        try:
            dispatch(obs, snapshot(), {"S03": StubRule("S03", fail=True)})
        except HandlerFailed as exc:
            row_id = self.adapter.persist_failure(obs, scene_id=exc.scene_id, rule_id="s03-rule", code="HANDLER_FAILED", message=str(exc))
        self.assertGreater(row_id, 0)
        with self.Session() as db:
            failure = db.scalar(select(ClueProcessingRecord))
        self.assertEqual((failure.status, failure.error_code, failure.scene_id), ("failed", "HANDLER_FAILED", "S03"))
        self.assertEqual((self._count(RiskClueRecord), self._count(Event)), (0, 0))

    def test_ac12_observation_receipt_dedupes_redelivery(self):
        obs = observation("seat_absence")
        self.assertFalse(self.adapter.seen(obs))
        self.assertTrue(self.adapter.mark_processed(obs))
        self.assertTrue(self.adapter.seen(obs))
        self.assertFalse(self.adapter.mark_processed(obs))
        self.assertEqual(self._count(ObservationReceipt), 1)

    def test_r9_receipt_written_only_after_clues_persisted(self):
        obs = observation("seat_absence")
        snap = snapshot()
        ids = process_observation(obs, snap, {"S03": StubRule("S03")}, self.adapter)
        self.assertEqual(len(ids), 1)
        self.assertEqual((self._count(RiskClueRecord), self._count(ObservationReceipt)), (1, 1))
        self.assertEqual(process_observation(obs, snap, {"S03": StubRule("S03")}, self.adapter), [], "重投跳过")
        failing = observation("seat_absence")
        self.assertEqual(process_observation(failing, snap, {"S03": StubRule("S03", fail=True)}, self.adapter), [])
        self.assertEqual(self._count(ClueProcessingRecord), 1)
        self.assertFalse(self.adapter.seen(failing), "规则失败不记收据，修好后可重算")
        bad_scope = observation("seat_absence")
        bad_scope["session_id"] = "session-zzz"
        self.assertEqual(process_observation(bad_scope, snap, {"S03": StubRule("S03")}, self.adapter), [])
        self.assertEqual(self._count(ClueProcessingRecord), 2)
        self.assertTrue(self.adapter.seen(bad_scope), "作用域不符是确定性失败，记收据不再重算")

    def test_adapter_rejects_invalid_clue_and_publisher_failure_is_contained(self):
        with self.assertRaises(DispatchError):
            self.adapter.persist_clue({"clue_id": "x"})
        boom = EventAdapter(self.Session, publisher=lambda message: (_ for _ in ()).throw(ConnectionError("redis down")))
        boom.persist_clue(self._clue())
        self.assertEqual((self._count(RiskClueRecord), self._count(Event)), (1, 1))

    def test_model_check_constraints(self):
        from sqlalchemy.exc import IntegrityError
        with self.assertRaises(IntegrityError):
            with self.Session.begin() as db:
                db.add(RiskClueRecord(clue_id="bad", source_id=SOURCE, binding_status="bound", scene_id="S03", rule_id="r", rule_version="v",
                                      conclusion="normal", risk_level="high", occurred_at=datetime.now(timezone.utc), media_run_id="run-1",
                                      start_ms=0, end_ms=1, reason="x", observation_ids=[], missing_facts=[], quality_flags=[],
                                      evidence_refs=[], clue={}))
        with self.assertRaises(IntegrityError):
            with self.Session.begin() as db:
                db.add(ClueProcessingRecord(observation_id="o", source_id=SOURCE, status="ok", error_code="X", error_message="m",
                                            occurred_at=datetime.now(timezone.utc)))


class WiringGateTest(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("STAGE_DISPATCHER_WIRED") == "1", "10.1 接线后设 STAGE_DISPATCHER_WIRED=1 启用")
    def test_old_template_path_removed_from_main(self):
        main = (REPO / "services/event-service/service/main.py").read_text(encoding="utf-8")
        for token in ("EVENT_TEMPLATES", "observation_allowed", "RECOVERY_MAP", 'setdefault("session-001"'):
            self.assertNotIn(token, main, f"旧机制 {token} 不得复活")


if __name__ == "__main__":
    unittest.main()
