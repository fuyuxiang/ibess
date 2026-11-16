"""5.01：A 扩展从 B 的库只读上下文（区域配置、阶段快照、工位身份、离岗许可）的单测。

用 SQLite 建与 B 同名同列的最小表，跑 DbContextSource 的真实 SQL；PG 上 JSONB 直接是对象，这里存 JSON 字符串，同时覆盖两种读法。
运行（services/vision-worker 下）：PYTHONPATH=. python -m pytest -q tests/test_a_context_v2.py
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, text

from worker.a_context import CachedContext, DbContextSource, RuleContext

SCHEMA = [
    "CREATE TABLE source_zone_configs (config_id TEXT, session_id TEXT, project_id TEXT, room_id TEXT, source_id TEXT, config_version INT, zones TEXT, entry_lines TEXT, updated_at TIMESTAMP)",
    "CREATE TABLE phase_snapshots (stage_snapshot_id TEXT, session_id TEXT, project_id TEXT, room_id TEXT, source_id TEXT, phase_id TEXT, phase_version INT, effective_at TIMESTAMP, enabled_scenes TEXT, rule_versions TEXT, exceptions TEXT)",
    "CREATE TABLE business_authorization_snapshots (snapshot_id TEXT, room_id TEXT, status TEXT)",
    "CREATE TABLE business_authorization_entries (entry_id TEXT, snapshot_id TEXT, person_id TEXT, seat_id TEXT, identity_verified BOOLEAN)",
    "CREATE TABLE phase_bindings (binding_id TEXT, session_id TEXT, source_id TEXT, active BOOLEAN)",
    "CREATE TABLE eval_sessions (session_id TEXT, status TEXT)",
    "CREATE TABLE supervision_session_workflows (session_id TEXT, identity_verification_enabled BOOLEAN)",
    "CREATE TABLE business_attendance_facts (fact_id TEXT, room_id TEXT, fact_type TEXT, person_id TEXT, seat_id TEXT, occurred_at TIMESTAMP, payload TEXT)",
]
NOW = datetime.now(timezone.utc)


class DbContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.engine = create_engine(f"sqlite:///{Path(self.tmp.name) / 'b.db'}")
        with self.engine.begin() as conn:
            for ddl in SCHEMA:
                conn.execute(text(ddl))

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def source(self):
        return DbContextSource("unused", permit_minutes=30, engine_factory=lambda: self.engine)

    def insert(self, sql, **params):
        with self.engine.begin() as conn:
            conn.execute(text(sql), params)

    def test_empty_database_gives_empty_context(self):
        ctx = self.source().load("camera-007")
        self.assertIsNone(ctx.zone_config)
        self.assertIsNone(ctx.snapshot)
        self.assertEqual((ctx.seat_identities, ctx.authorizations, ctx.exceptions), ({}, [], []))

    def test_full_context(self):
        zones = [{"zone_id": "z1", "name": "1号工位", "kind": "seat", "seat_id": "seat-1", "polygon": [[0, 0], [0.2, 0], [0.2, 0.2]]}]
        self.insert("INSERT INTO source_zone_configs VALUES ('c1','sess-1','proj-1','room-1','camera-007',3,:z,'[]',:t)", z=json.dumps(zones), t=NOW)
        self.insert("INSERT INTO source_zone_configs VALUES ('c0','sess-0','proj-1','room-1','camera-007',1,'[]','[]',:t)", t=NOW - timedelta(days=1))
        self.insert("INSERT INTO phase_snapshots VALUES ('snap-2','sess-1','proj-1','room-1','camera-007','DETAILED',2,:t,'[\"S03\"]','{\"S03\": \"s03-v2\"}',:e)",
                    t=NOW - timedelta(minutes=5), e=json.dumps([{"fact_id": "x", "fact_version": 1}]))
        self.insert("INSERT INTO phase_snapshots VALUES ('snap-future','sess-1','proj-1','room-1','camera-007','SCORING',3,:t,'[]','{}','[]')",
                    t=NOW + timedelta(hours=1))
        self.insert("INSERT INTO phase_bindings VALUES ('b1','sess-1','camera-007',1)")
        self.insert("INSERT INTO eval_sessions VALUES ('sess-1','running')")
        self.insert("INSERT INTO business_authorization_snapshots VALUES ('a1','room-1','active'), ('a0','room-1','superseded')")
        self.insert("INSERT INTO business_authorization_entries VALUES ('e1','a1','p-1','seat-1',1), ('e2','a1','p-2','seat-2',0), ('e3','a0','p-9','seat-9',1)")
        self.insert("INSERT INTO business_attendance_facts VALUES ('f1','room-1','leave_permit','p-1','seat-1',:t,NULL), "
                    "('f2','room-1','check_in','p-1','seat-1',:t,NULL), ('f3','room-2','leave_permit','p-3',NULL,:t,NULL)", t=NOW - timedelta(minutes=2))
        ctx = self.source().load("camera-007")
        self.assertEqual(ctx.zone_config["config_version"], 3, "取最新的区域配置")
        self.assertEqual(ctx.zone_config["zones"][0]["seat_id"], "seat-1")
        self.assertEqual(ctx.snapshot["stage_snapshot_id"], "snap-2", "未来生效的快照不取")
        self.assertEqual(ctx.snapshot["rule_versions"], {"S03": "s03-v2"})
        self.assertEqual(ctx.scope["phase_version"], 2)
        self.assertEqual(ctx.seat_identities, {"seat-1": {"person_id": "p-1", "verified": True}}, "只取 active 快照里身份已核验的")
        self.assertEqual([a["fact_id"] for a in ctx.authorizations], ["f1"], "只取本室的离岗许可")
        self.assertEqual(ctx.authorizations[0]["scope"], {"seat_id": "seat-1"})
        self.assertEqual({e["fact_id"] for e in ctx.exceptions}, {"x", "f1"}, "快照例外 + 本室许可")
        rule_ctx = ctx.rule_context("S03", "s03-v1")
        self.assertEqual(rule_ctx["rule_version"], "s03-v2")

    def test_snapshot_requires_active_binding_and_open_session(self):
        self.insert("INSERT INTO source_zone_configs VALUES ('c1','sess-1','proj-1','room-1','camera-007',3,'[]','[]',:t)", t=NOW)
        self.insert("INSERT INTO source_zone_configs VALUES ('c2','sess-2','proj-1','room-1','camera-007',9,'[]','[]',:t)", t=NOW + timedelta(seconds=1))
        self.insert("INSERT INTO phase_snapshots VALUES ('snap-1','sess-1','proj-1','room-1','camera-007','DETAILED',1,:t,'[]','{}','[]')",
                    t=NOW - timedelta(minutes=5))
        self.insert("INSERT INTO phase_bindings VALUES ('b1','sess-1','camera-007',1)")
        self.insert("INSERT INTO eval_sessions VALUES ('sess-1','running')")
        ctx = self.source().load("camera-007")
        self.assertEqual(ctx.snapshot["stage_snapshot_id"], "snap-1")
        self.assertEqual(ctx.zone_config["session_id"], "sess-1", "有快照时区域配置必须是同一场次的，不取别的场次更新的那份")
        self.insert("UPDATE eval_sessions SET status = 'closed'")
        ctx = self.source().load("camera-007")
        self.assertIsNone(ctx.snapshot, "场次已关闭：旧快照不再算 bound")
        self.assertEqual(ctx.zone_config["session_id"], "sess-2", "无快照时取本源最新的区域配置")
        self.insert("UPDATE eval_sessions SET status = 'running'")
        self.insert("UPDATE phase_bindings SET active = 0")
        self.assertIsNone(self.source().load("camera-007").snapshot, "绑定失效：不算 bound")

    def test_optional_identity_keeps_spatial_context_and_seat_permits(self):
        self.test_full_context()
        self.insert("INSERT INTO supervision_session_workflows VALUES ('sess-1',0)")
        ctx = self.source().load('camera-007')
        self.assertFalse(ctx.identity_verification_enabled)
        self.assertFalse(ctx.seat_identities)
        self.assertTrue(ctx.zone_config['zones'])
        self.assertEqual(ctx.authorizations[0]['scope'], {'seat_id': 'seat-1'})
        self.insert("UPDATE supervision_session_workflows SET identity_verification_enabled=1")
        self.assertEqual(self.source().load('camera-007').seat_identities['seat-1']['person_id'], 'p-1')

    def test_background_refresh_never_blocks(self):
        import threading
        import time as _t
        gate = threading.Event()

        class Slow:
            def load(self, source_id):
                gate.wait(5)
                return RuleContext(zone_config={"zones": [1]})

        cached = CachedContext(Slow(), "camera-007", refresh_seconds=1e9, background=True)
        began = _t.monotonic()
        self.assertIsNone(cached.get().zone_config, "库还没返回时用空上下文，不等")
        self.assertLess(_t.monotonic() - began, 0.5)
        gate.set()
        for _ in range(50):
            if cached.get().zone_config:
                break
            _t.sleep(0.02)
        self.assertEqual(cached.get().zone_config, {"zones": [1]})

    def test_cached_context_keeps_last_good_on_failure(self):
        calls = {"n": 0}

        class Flaky:
            def load(self, source_id):
                calls["n"] += 1
                if calls["n"] > 1:
                    raise RuntimeError("db down")
                return RuleContext(zone_config={"zones": []})

        clock = [0.0]
        cached = CachedContext(Flaky(), "camera-007", refresh_seconds=10, clock=lambda: clock[0])
        first = cached.get()
        clock[0] = 11
        self.assertIs(cached.get(), first, "刷新失败沿用上一份")
        clock[0] = 12
        cached.get()
        self.assertEqual(calls["n"], 2, "未到间隔不重复读库")


if __name__ == "__main__":
    unittest.main()
