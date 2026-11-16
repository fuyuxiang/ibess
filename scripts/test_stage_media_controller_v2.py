"""执行卡 4.2：阶段媒体控制器与媒体运行记录。

桩验证：发布器、探针、ACK 发送器、配置补查、时钟都是假对象；真实子进程只用来验 terminate / kill 停止逻辑。
运行（项目根）：PYTHONPATH=scripts python -m unittest discover -s scripts -p test_stage_media_controller_v2.py -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tests" / "contracts"))

from media_run_registry import MediaRunRegistry, make_media_run_id, parse_utc  # noqa: E402
from stage_media_controller import (  # noqa: E402
    APPLY_FAILED, ASSET_MISSING, ControllerConfig, FfmpegPublisher, MediaController, MediaMtxProbe,
    ResolvedAsset, parse_rfc3339, redact_url, stop_process,
)
from contract_validator import ContractValidator  # noqa: E402  只读加载 1.2 校验器

SESSION, SOURCE = "session-a1", "camera-a1"
T0 = datetime(2026, 10, 19, 2, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start=T0):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += timedelta(seconds=seconds)

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


class FakeHandle:
    def __init__(self, path, url):
        self.path, self.url = path, url
        self.code = None
        self.stopped = False
        self.stop_order = None


class FakePublisher:
    def __init__(self, fail_start=False):
        self.handles = []
        self.events = []
        self.fail_start = fail_start

    def start(self, path, publish_url):
        if self.fail_start:
            raise OSError("ffmpeg not found")
        handle = FakeHandle(path, publish_url)
        self.handles.append(handle)
        self.events.append(("start", path))
        return handle

    def poll(self, handle):
        return handle.code

    def stop(self, handle, grace_seconds):
        handle.stopped = True
        handle.code = -15 if handle.code is None else handle.code
        self.events.append(("stop", handle.path))


class FakeProbe:
    def __init__(self, answer=True):
        self.answer = answer
        self.calls = 0
        self.since = []

    def is_publishing(self, path_name, since=None):
        self.calls += 1
        self.since.append(since)
        return self.answer


class FakeFetcher:
    def __init__(self, config=None):
        self.config = config
        self.calls = 0

    def fetch(self, session_id):
        self.calls += 1
        return self.config if session_id == SESSION else None


class FakeAcks:
    def __init__(self):
        self.sent = []

    def send(self, session_id, ack):
        self.sent.append((session_id, ack))


class FakeResolver:
    def __init__(self, root):
        self.root = Path(root)
        self.assets = {}

    def add(self, asset_id, name="clip.mp4", duration_ms=60000, create=True):
        path = self.root / name
        if create:
            path.write_bytes(b"\x00" * 1024)
        self.assets[asset_id] = ResolvedAsset(asset_id=asset_id, path=path, duration_ms=duration_ms, sha256=None)
        return path

    def resolve(self, session_id, asset_id):
        return self.assets.get(asset_id) if session_id == SESSION else None


def config(version, snapshot=None, assets=None, phase="DETAILED", source=SOURCE, session=SESSION):
    return {
        "contract_version": "2.0.0", "project_id": "project-a", "room_id": "room-a", "session_id": session,
        "source_id": source, "stage_snapshot_id": snapshot or f"snap-{version}", "phase_id": phase,
        "phase_version": version, "effective_at": "2026-10-19T01:00:00Z",
        "enabled_scenes": ["S03"], "always_on_scenes": ["S10"],
        "assets": assets if assets is not None else [{"slot": "video", "source_id": source, "asset_id": "asset-1"}],
        "rule_versions": {"S03": "s03-v1", "S10": "s10-v1"}, "exceptions": [], "sync_status": "pending",
    }


def message(cfg, notification_id):
    return {"type": "phase_changed", "notification_id": notification_id, "session_id": cfg["session_id"],
            "source_id": cfg["source_id"], "phase_version": str(cfg["phase_version"]),
            "payload": json.dumps(cfg, ensure_ascii=False)}


class ControllerHarness:
    def __init__(self, tmp, *, probe=True, fail_start=False, acks=True, registry_path=None, fetcher=None):
        self.tmp = Path(tmp)
        self.clock = FakeClock()
        self.publisher = FakePublisher(fail_start=fail_start)
        self.probe = FakeProbe(probe) if probe is not None else None
        self.acks = FakeAcks() if acks else None
        self.resolver = FakeResolver(self.tmp)
        self.registry = MediaRunRegistry(registry_path or self.tmp / "runs.json")
        self.runs_seen = []
        self.controller = MediaController(
            ControllerConfig(session_id=SESSION, source_id=SOURCE, confirm_seconds=5, confirm_poll_seconds=1, stop_grace_seconds=3,
                             refresh_interval_seconds=30, ack_retry_seconds=5),
            registry=self.registry, resolver=self.resolver, publisher=self.publisher, probe=self.probe,
            acks=self.acks, fetcher=fetcher, clock=self.clock, run_sink=self.runs_seen.append)


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = MediaRunRegistry(Path(self.tmp.name) / "runs.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _start(self, started="2026-10-19T02:00:00.000Z", loop=0):
        return self.registry.start(session_id=SESSION, source_id=SOURCE, asset_id="asset-1", stage_snapshot_id="snap-1",
                                   phase_id="DETAILED", phase_version=1, started_at=started, asset_duration_ms=60000, loop_index=loop)

    def test_ac7_locate_and_reverse_mapping(self):
        run = self._start()
        self.assertEqual(run.media_run_id, make_media_run_id(SOURCE, "snap-1", 0, "2026-10-19T02:00:00.000Z"))
        self.assertEqual(run.locate_ms(T0 + timedelta(seconds=12, milliseconds=500)), 12500)
        self.assertIsNone(run.locate_ms(T0 - timedelta(seconds=1)))
        self.assertIsNone(run.locate_ms(T0 + timedelta(seconds=61)), "超过素材时长不换算")
        self.assertEqual(run.utc_of(12500), "2026-10-19T02:00:12.500Z")
        self.assertIsNone(run.utc_of(-1))
        self.assertIsNone(run.utc_of(True))
        self.registry.mark_playing(run.media_run_id, "2026-10-19T02:00:01.000Z")
        self.registry.stop(run.media_run_id, stopped_at="2026-10-19T02:00:30.000Z", reason="phase_switch", planned=True)
        self.assertIsNone(run.locate_ms(T0 + timedelta(seconds=31)), "停止后的时刻不属于本轮")
        self.assertIsNone(run.utc_of(45000))
        located = self.registry.locate(SOURCE, T0 + timedelta(seconds=10))
        self.assertEqual(located["media_run_id"], run.media_run_id)
        self.assertEqual(located["start_ms"], 10000)
        self.assertIsNone(self.registry.locate("other-camera", T0 + timedelta(seconds=10)))

    def test_ac6_next_loop_gets_new_run_id_and_boundaries(self):
        first = self._start()
        self.registry.mark_playing(first.media_run_id, "2026-10-19T02:00:01.000Z")
        self.registry.stop(first.media_run_id, stopped_at="2026-10-19T02:01:00.000Z", reason="loop_end", planned=True)
        second = self.registry.next_loop(first, started_at="2026-10-19T02:01:00.200Z")
        self.assertNotEqual(first.media_run_id, second.media_run_id)
        self.assertEqual(second.loop_index, 1)
        self.assertEqual(second.asset_id, first.asset_id)
        kinds = [(b["kind"], b["loop_index"]) for b in self.registry.boundaries(SOURCE)]
        self.assertEqual(kinds, [("run_start", 0), ("run_end", 0), ("run_start", 1)])
        self.assertEqual(self.registry.boundaries(SOURCE)[1]["start_ms"], 60000)

    def test_ac11_planned_switch_marker_and_idempotent_stop(self):
        run = self._start()
        self.registry.stop(run.media_run_id, stopped_at="2026-10-19T02:00:05.000Z", reason="phase_switch", planned=True)
        self.assertEqual((run.status, run.stop_reason, run.planned), ("stopped", "phase_switch", True))
        again = self.registry.stop(run.media_run_id, stopped_at="2026-10-19T02:00:09.000Z", reason="shutdown", planned=False)
        self.assertEqual(again.stopped_at, "2026-10-19T02:00:05.000Z", "已结束的 run 不被改写")
        with self.assertRaises(ValueError):
            self.registry.stop(run.media_run_id, stopped_at="2026-10-19T02:00:09.000Z", reason="whatever", planned=False)

    def test_persist_restore_and_close_dangling(self):
        run = self._start()
        self.registry.mark_playing(run.media_run_id, "2026-10-19T02:00:01.000Z")
        reloaded = MediaRunRegistry(self.registry.path)
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded.current(SOURCE).media_run_id, run.media_run_id)
        closed = reloaded.close_dangling(at="2026-10-19T02:05:00.000Z")
        self.assertEqual([r.stop_reason for r in closed], ["controller_restart"])
        self.assertFalse(closed[0].planned)
        self.assertIsNone(reloaded.current(SOURCE))
        with self.assertRaises(ValueError):
            MediaRunRegistry().restore({"version": 99})

    def test_registry_refuses_defaults_and_double_start(self):
        with self.assertRaises(ValueError):
            self.registry.start(session_id="", source_id=SOURCE, asset_id="a", stage_snapshot_id="s", phase_id="DETAILED",
                                phase_version=1, started_at="2026-10-19T02:00:00Z")
        with self.assertRaises(ValueError):
            self.registry.start(session_id=SESSION, source_id=SOURCE, asset_id="a", stage_snapshot_id="s", phase_id="DETAILED",
                                phase_version=0, started_at="2026-10-19T02:00:00Z")
        self._start()
        with self.assertRaises(ValueError):
            self._start()


class ControllerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = ContractValidator()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = ControllerHarness(self.tmp.name)
        self.h.resolver.add("asset-1")

    def tearDown(self):
        self.tmp.cleanup()

    def _assert_ack_contract(self, ack):
        self.assertEqual(self.validator.validate_object("control-ack", ack), [])

    def test_ac1_apply_starts_stream_and_acks_applied_after_probe(self):
        ack = self.h.controller.apply_config(config(1), received_at="2026-10-19T02:00:00.000Z")
        self.assertEqual(ack["status"], "applied")
        self.assertIsNone(ack["error"])
        self.assertGreaterEqual(parse_utc(ack["applied_at"]), parse_utc(ack["received_at"]))
        self.assertEqual(ack["consumer_id"], "media-controller")
        self.assertEqual(ack["source_id"], SOURCE)
        self._assert_ack_contract(ack)
        self.assertEqual([e[0] for e in self.h.publisher.events], ["start"])
        self.assertEqual(self.h.publisher.handles[0].url, "rtsp://mediamtx:8554/cam1")
        run = self.h.registry.current(SOURCE)
        self.assertEqual((run.status, run.asset_id, run.phase_version, run.loop_index), ("playing", "asset-1", 1, 0))
        self.assertEqual(self.h.acks.sent[0][1], ack)
        self.assertEqual(self.h.controller.status()["applied_phase_version"], 1)

    def test_ac2_missing_asset_stops_old_stream_and_acks_missing(self):
        self.h.controller.apply_config(config(1), received_at="2026-10-19T02:00:00.000Z")
        for cfg in (config(2, assets=[]),
                    config(3, assets=[{"slot": "video", "source_id": SOURCE, "asset_id": "never-registered"}]),
                    config(4, assets=[{"slot": "audio", "source_id": SOURCE, "asset_id": "asset-1"}])):
            ack = self.h.controller.apply_config(cfg, received_at="2026-10-19T02:00:10.000Z")
            self.assertEqual(ack["status"], "missing_asset")
            self.assertEqual(ack["error"], ASSET_MISSING)
            self.assertIsNone(ack["applied_at"])
            self._assert_ack_contract(ack)
        self.assertEqual([e[0] for e in self.h.publisher.events], ["start", "stop"])
        self.assertTrue(self.h.publisher.handles[0].stopped)
        self.assertIsNone(self.h.registry.current(SOURCE))
        self.assertEqual(self.h.registry.runs(SOURCE)[0].stop_reason, "phase_switch")
        # 文件被移走也算缺失
        self.h.resolver.add("asset-gone", name="gone.mp4", create=False)
        ack = self.h.controller.apply_config(config(5, assets=[{"slot": "video", "source_id": SOURCE, "asset_id": "asset-gone"}]),
                                             received_at="2026-10-19T02:00:20.000Z")
        self.assertEqual(ack["status"], "missing_asset")
        self.assertEqual(len(self.h.publisher.handles), 1)

    def test_ac3_batch_applies_highest_version_and_late_old_version_ignored(self):
        batch = [message(config(1), "n1"), message(config(3), "n3"), message(config(2), "n2")]
        acks = self.h.controller.handle_messages(batch)
        self.assertEqual([a["phase_version"] for a in acks], [3])
        self.assertEqual(len(self.h.publisher.handles), 1)
        self.assertEqual(self.h.registry.current(SOURCE).phase_version, 3)
        late = self.h.controller.handle_messages([message(config(2, snapshot="snap-2-late"), "n2-late")])
        self.assertEqual(late, [])
        self.assertEqual(len(self.h.publisher.handles), 1, "旧版本不重启流")
        self.assertEqual(self.h.controller.status()["applied_phase_version"], 3)
        self.assertIsNone(self.h.controller.apply_config(config(2), received_at="2026-10-19T02:00:30.000Z"))

    def test_ac10_duplicate_notification_and_foreign_scope_ignored(self):
        first = self.h.controller.handle_messages([message(config(1), "n1")])
        self.assertEqual(len(first), 1)
        self.assertEqual(self.h.controller.handle_messages([message(config(1), "n1")]), [])
        other_session = message(config(9, session="session-other"), "n9")
        other_source = message(config(9, source="camera-other"), "n10")
        bad_payload = {"type": "phase_changed", "notification_id": "n11", "session_id": SESSION, "source_id": SOURCE,
                       "phase_version": "9", "payload": "{not json"}
        self.assertEqual(self.h.controller.handle_messages([other_session, other_source, bad_payload, {"type": "noise"}]), [])
        self.assertEqual(len(self.h.publisher.handles), 1)
        self.assertEqual(len(self.h.acks.sent), 1)

    def test_ac4_switch_stops_old_before_starting_new_and_shutdown_leaves_nothing(self):
        self.h.controller.apply_config(config(1), received_at="2026-10-19T02:00:00.000Z")
        self.h.resolver.add("asset-2", name="clip2.mp4")
        self.h.controller.apply_config(config(2, assets=[{"slot": "video", "source_id": SOURCE, "asset_id": "asset-2"}]),
                                       received_at="2026-10-19T02:00:10.000Z")
        self.assertEqual([e[0] for e in self.h.publisher.events], ["start", "stop", "start"])
        first, second = self.h.publisher.handles
        self.assertTrue(first.stopped)
        self.assertFalse(second.stopped)
        runs = self.h.registry.runs(SOURCE)
        self.assertEqual((runs[0].stop_reason, runs[0].planned), ("phase_switch", True))
        self.h.controller.shutdown()
        self.assertTrue(all(h.stopped for h in self.h.publisher.handles))
        self.assertIsNone(self.h.registry.current(SOURCE))
        self.assertEqual(runs[1].stop_reason, "shutdown")
        self.assertTrue(runs[1].planned)

    def test_ac5_restart_closes_dangling_run_and_reapplies_persisted_config(self):
        self.h.controller.apply_config(config(1), received_at="2026-10-19T02:00:00.000Z")
        old_run_id = self.h.registry.current(SOURCE).media_run_id

        class Fetcher:
            def __init__(self):
                self.calls = 0

            def fetch(self, session_id):
                self.calls += 1
                return config(1) if session_id == SESSION else None

        fetcher = Fetcher()
        restarted = ControllerHarness(self.tmp.name, registry_path=self.h.registry.path, fetcher=fetcher)
        restarted.resolver.add("asset-1")
        restarted.clock.advance(60)
        ack = restarted.controller.recover()
        self.assertEqual(fetcher.calls, 1)
        self.assertEqual(ack["status"], "applied")
        runs = restarted.registry.runs(SOURCE)
        self.assertEqual(runs[0].media_run_id, old_run_id)
        self.assertEqual((runs[0].status, runs[0].stop_reason, runs[0].planned), ("stopped", "controller_restart", False))
        self.assertNotEqual(runs[1].media_run_id, old_run_id)
        self.assertEqual(runs[1].status, "playing")
        self.assertEqual(len(restarted.publisher.handles), 1)

    def test_ac6_process_exit_zero_starts_next_loop_with_new_run_id(self):
        self.h.controller.apply_config(config(1), received_at="2026-10-19T02:00:00.000Z")
        first = self.h.registry.current(SOURCE)
        self.h.clock.advance(60)
        self.h.publisher.handles[0].code = 0
        self.h.controller.tick()
        second = self.h.registry.current(SOURCE)
        self.assertNotEqual(first.media_run_id, second.media_run_id)
        self.assertEqual((first.status, first.stop_reason, first.planned), ("stopped", "loop_end", True))
        self.assertEqual((second.loop_index, second.status, second.asset_id), (1, "playing", "asset-1"))
        self.assertEqual(len(self.h.acks.sent), 1, "循环不需要再回 ACK")
        kinds = [b["kind"] for b in self.h.registry.boundaries(SOURCE)]
        self.assertEqual(kinds, ["run_start", "run_end", "run_start"])

    def test_ac8_start_failure_dead_process_or_no_confirmation_ack_failed(self):
        broken = ControllerHarness(self.tmp.name, fail_start=True, registry_path=Path(self.tmp.name) / "broken.json")
        broken.resolver.add("asset-1")
        ack = broken.controller.apply_config(config(1), received_at="2026-10-19T02:00:00.000Z")
        self.assertEqual((ack["status"], ack["error"], ack["applied_at"]), ("failed", APPLY_FAILED, None))
        self._assert_ack_contract(ack)
        self.assertEqual(broken.registry.runs(SOURCE)[0].status, "failed")

        never = ControllerHarness(self.tmp.name, probe=False, registry_path=Path(self.tmp.name) / "never.json")
        never.resolver.add("asset-1")
        ack = never.controller.apply_config(config(1), received_at="2026-10-19T02:00:00.000Z")
        self.assertEqual(ack["status"], "failed")
        self.assertTrue(never.publisher.handles[0].stopped, "确认失败的进程被停掉")
        self.assertEqual(never.registry.runs(SOURCE)[0].note, "confirm_timeout")

        class DiesImmediately(FakePublisher):
            def start(self, path, publish_url):
                handle = super().start(path, publish_url)
                handle.code = 1
                return handle

        dead = ControllerHarness(self.tmp.name, registry_path=Path(self.tmp.name) / "dead.json")
        dead.publisher = dead.controller.publisher = DiesImmediately()
        dead.resolver.add("asset-1")
        ack = dead.controller.apply_config(config(1), received_at="2026-10-19T02:00:00.000Z")
        self.assertEqual(ack["status"], "failed")
        self.assertEqual(dead.registry.runs(SOURCE)[0].note, "process_exited_before_confirm")

    def test_ac8_repeated_crash_exhausts_restarts_then_acks_failed(self):
        self.h.controller.apply_config(config(1), received_at="2026-10-19T02:00:00.000Z")
        for _ in range(4):
            self.h.publisher.handles[-1].code = 1
            self.h.controller.tick()
        statuses = [a["status"] for _, a in self.h.acks.sent]
        self.assertEqual(statuses, ["applied", "failed"])
        self.assertTrue(all(r.status in ("failed", "stopped") for r in self.h.registry.runs(SOURCE)[:-1]))
        self.assertEqual(self.h.controller.status()["restarts"], 4)

    def test_ac12_without_token_streams_but_marks_ack_disabled(self):
        quiet = ControllerHarness(self.tmp.name, acks=False)
        quiet.resolver.add("asset-1")
        ack = quiet.controller.apply_config(config(1), received_at="2026-10-19T02:00:00.000Z")
        self.assertEqual(ack["status"], "applied")
        status = quiet.controller.status()
        self.assertTrue(status["ack_disabled"])
        self.assertEqual(status["current_run"]["status"], "playing")
        self.assertNotIn("token", json.dumps(status).lower())

    def test_probe_unavailable_falls_back_to_process_alive_and_marks_it(self):
        unknown = ControllerHarness(self.tmp.name, probe=None)
        unknown.resolver.add("asset-1")
        ack = unknown.controller.apply_config(config(1), received_at="2026-10-19T02:00:00.000Z")
        self.assertEqual(ack["status"], "applied")
        self.assertTrue(unknown.controller.status()["probe_unavailable"])
        self.assertGreaterEqual(parse_utc(ack["applied_at"]) - parse_utc(ack["received_at"]), timedelta(seconds=5))

    def test_ac9_every_ack_shape_passes_contract(self):
        acks = []
        acks.append(self.h.controller.apply_config(config(1), received_at="2026-10-19T02:00:00.000Z"))
        acks.append(self.h.controller.apply_config(config(2, assets=[]), received_at="2026-10-19T02:00:05.000Z"))
        broken = ControllerHarness(self.tmp.name, fail_start=True)
        broken.resolver.add("asset-1")
        acks.append(broken.controller.apply_config(config(1), received_at="2026-10-19T02:00:00.000Z"))
        for ack in acks:
            self._assert_ack_contract(ack)
        self.assertEqual([a["status"] for a in acks], ["applied", "missing_asset", "failed"])

    def test_run_sink_receives_every_run_change_without_paths(self):
        self.h.controller.apply_config(config(1), received_at="2026-10-19T02:00:00.000Z")
        self.h.controller.shutdown()
        self.assertEqual([r["status"] for r in self.h.runs_seen], ["playing", "stopped"])
        self.assertNotIn("path", json.dumps(self.h.runs_seen[0]))


class ReviewFindingsTest(unittest.TestCase):
    """2026-10-19 独立复核发现的回归（D-10～D-21）。"""

    @classmethod
    def setUpClass(cls):
        cls.validator = ContractValidator()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _harness(self, **kw):
        h = ControllerHarness(self.tmp.name, **kw)
        h.resolver.add("asset-1")
        return h

    def test_r10_registry_close_dangling_scoped_to_source(self):
        registry = MediaRunRegistry(Path(self.tmp.name) / "shared.json")
        for source in ("cam-a", "cam-b"):
            registry.start(session_id=SESSION, source_id=source, asset_id="a", stage_snapshot_id="s", phase_id="DETAILED",
                           phase_version=1, started_at="2026-10-19T02:00:00.000Z")
        closed = registry.close_dangling(at="2026-10-19T02:01:00.000Z", source_id="cam-a")
        self.assertEqual([r.source_id for r in closed], ["cam-a"])
        self.assertIsNotNone(registry.current("cam-b"), "别的源的 run 不被本源重启关掉")
        h = self._harness(registry_path=registry.path)
        h.controller.recover()
        self.assertIsNotNone(registry.current("cam-b"))

    def test_r11_restart_budget_resets_after_full_loop(self):
        h = self._harness()
        h.controller.apply_config(config(1), received_at="2026-10-19T02:00:00.000Z")
        for _ in range(3):                                   # 3 次崩溃都在预算内
            h.publisher.handles[-1].code = 1
            h.controller.tick()
        self.assertEqual(h.controller.status()["restarts"], 3)
        h.publisher.handles[-1].code = 0                      # 完整播完一轮
        h.controller.tick()
        self.assertEqual(h.controller.status()["restarts"], 0, "播完一轮即重置预算")
        h.publisher.handles[-1].code = 1
        h.controller.tick()
        self.assertIsNotNone(h.registry.current(SOURCE), "第 4 次崩溃不再被旧预算判死")
        self.assertEqual([a["status"] for _, a in h.acks.sent], ["applied"])

    def test_r12_restart_without_fetcher_keeps_version_floor_from_registry(self):
        h = self._harness()
        h.controller.handle_messages([message(config(5), "n5")])
        restarted = ControllerHarness(self.tmp.name, registry_path=h.registry.path, acks=True)
        restarted.resolver.add("asset-1")
        restarted.controller.recover()                        # 无 fetcher：只能靠运行记录
        self.assertEqual(restarted.controller.status()["version_floor"], 5)
        self.assertEqual(restarted.controller.handle_messages([message(config(2), "n2-replayed")]), [], "重投的旧版本不回退")
        self.assertEqual(len(restarted.publisher.handles), 0)
        acks = restarted.controller.handle_messages([message(config(6), "n6")])
        self.assertEqual([a["phase_version"] for a in acks], [6])

    def test_r13_malformed_config_rejected_before_side_effects(self):
        h = self._harness()
        h.controller.apply_config(config(1), received_at="2026-10-19T02:00:00.000Z")
        broken = config(2)
        del broken["room_id"]
        self.assertIsNone(h.controller.apply_config(broken, received_at="2026-10-19T02:00:10.000Z"))
        self.assertEqual([e[0] for e in h.publisher.events], ["start"], "缺字段的配置不停旧流、不起新流")
        for bad in (dict(config(2), phase_version=True), dict(config(2), phase_id="REVIEW"),
                    dict(config(2), assets=[{"slot": "video"}]), dict(config(2), project_id="")):
            self.assertEqual(h.controller.handle_messages([message(bad, f"bad-{id(bad)}")]), [])
        self.assertEqual(len(h.publisher.handles), 1)

    def test_r14_ack_mirrors_snapshot_source_and_records_http_code(self):
        import urllib.error
        h = self._harness()
        ack = h.controller.apply_config(config(1), received_at="2026-10-19T02:00:00.000Z")
        self.assertEqual(ack["source_id"], SOURCE)
        self._assert_contract(ack)

        class Rejecting:
            def __init__(self):
                self.calls = 0

            def send(self, session_id, ack):
                self.calls += 1
                raise urllib.error.HTTPError("http://api/x", 409, "conflict", {}, None)

        h2 = self._harness(registry_path=Path(self.tmp.name) / "h2.json")
        h2.acks = h2.controller.acks = Rejecting()
        h2.controller.apply_config(config(1), received_at="2026-10-19T02:00:00.000Z")
        status = h2.controller.status()
        self.assertEqual(status["last_ack_error"], "HTTP_409")
        self.assertTrue(status["ack_pending"])
        self.assertNotIn("http://", json.dumps(status))

    def test_r15_periodic_refresh_and_ack_retry(self):
        fetcher = FakeFetcher(config(1))
        h = self._harness(fetcher=fetcher)
        h.controller.recover()
        self.assertEqual(h.controller.status()["applied_phase_version"], 1)
        fetcher.config = config(2)
        h.controller.tick()
        self.assertEqual(h.controller.status()["applied_phase_version"], 1, "未到刷新间隔不补查")
        h.clock.advance(31)
        h.controller.tick()
        self.assertEqual(h.controller.status()["applied_phase_version"], 2, "周期补查应用更新的持久配置")
        self.assertEqual(fetcher.calls, 2)

        class Flaky:
            def __init__(self):
                self.calls = 0
                self.sent = []

            def send(self, session_id, ack):
                self.calls += 1
                if self.calls == 1:
                    raise ConnectionError("down")
                self.sent.append(ack)

        h2 = self._harness(registry_path=Path(self.tmp.name) / "h2.json")
        h2.acks = h2.controller.acks = Flaky()
        h2.controller.apply_config(config(1), received_at="2026-10-19T02:00:00.000Z")
        self.assertTrue(h2.controller.status()["ack_pending"])
        h2.controller.tick()
        self.assertTrue(h2.controller.status()["ack_pending"], "未到重试间隔")
        h2.clock.advance(6)
        h2.controller.tick()
        self.assertFalse(h2.controller.status()["ack_pending"])
        self.assertEqual(h2.acks.sent[0]["status"], "applied")

    def test_r15_consumer_group_reads_pending_before_new(self):
        from stage_media_controller import RedisNotifications

        class FakeRedis:
            def __init__(self):
                self.reads = []

            def xgroup_create(self, *args, **kwargs):
                raise Exception("BUSYGROUP Consumer Group name already exists")

            def xreadgroup(self, group, consumer, streams, count=None, block=None):
                start = list(streams.values())[0]
                self.reads.append(start)
                if start == "0":
                    return [(b"phase-control", [(b"1-0", {b"type": b"phase_changed", b"notification_id": b"n1"})])]
                return [(b"phase-control", [(b"2-0", {b"type": b"phase_changed", b"notification_id": b"n2"})])]

        notifications = RedisNotifications(FakeRedis(), "phase-control", "media-controller:cam", consumer="host")
        first = notifications.read()
        second = notifications.read()
        self.assertEqual([m[0] for m in first], ["1-0"])
        self.assertEqual([m[0] for m in second], ["2-0"])
        self.assertEqual(notifications.client.reads, ["0", ">"])
        self.assertEqual(first[0][1]["notification_id"], "n1")

    def test_r16_probe_requires_ready_time_not_before_this_run(self):
        import io

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(body):
            return lambda request, timeout: Response(body)

        since = datetime(2026, 10, 19, 2, 0, 0, tzinfo=timezone.utc)
        old = MediaMtxProbe("http://m:9997", opener=opener(b'{"ready":true,"readyTime":"2026-10-19T01:59:00.123456789Z"}'))
        self.assertFalse(old.is_publishing("cam1", since=since), "readyTime 早于本轮 → 是旧发布者")
        new = MediaMtxProbe("http://m:9997", opener=opener(b'{"ready":true,"readyTime":"2026-10-19T02:00:00.5Z"}'))
        self.assertTrue(new.is_publishing("cam1", since=since))
        missing = MediaMtxProbe("http://m:9997", opener=opener(b'{"ready":true}'))
        self.assertIsNone(missing.is_publishing("cam1", since=since), "没有 readyTime 按不可知")
        self.assertTrue(missing.is_publishing("cam1"))
        self.assertIsNone(parse_rfc3339("not-a-time"))
        h = self._harness()
        h.controller.apply_config(config(1), received_at="2026-10-19T02:00:00.000Z")
        self.assertEqual(h.probe.since[0], parse_utc(h.registry.runs(SOURCE)[0].started_at), "控制器把本轮开始时间交给探针")

    def test_r18_same_version_different_snapshot_is_conflict_everywhere(self):
        h = self._harness(fetcher=FakeFetcher(config(1)))
        h.controller.apply_config(config(1), received_at="2026-10-19T02:00:00.000Z")
        self.assertIsNone(h.controller.apply_config(config(1, snapshot="snap-1-other"), received_at="2026-10-19T02:00:05.000Z"))
        self.assertEqual(h.controller.handle_messages([message(config(1, snapshot="snap-1-other"), "n1b")]), [])
        self.assertEqual(len(h.publisher.handles), 1)
        self.assertEqual(h.controller.status()["applied_snapshot"], "snap-1")

    def test_r19_boundary_end_clamped_to_asset_duration(self):
        registry = MediaRunRegistry()
        run = registry.start(session_id=SESSION, source_id=SOURCE, asset_id="a", stage_snapshot_id="s", phase_id="DETAILED",
                             phase_version=1, started_at="2026-10-19T02:00:00.000Z", asset_duration_ms=60000)
        registry.stop(run.media_run_id, stopped_at="2026-10-19T02:01:30.000Z", reason="loop_end", planned=True)
        self.assertEqual(registry.boundaries(SOURCE)[-1]["start_ms"], 60000)

    def test_r20_redact_url_survives_bad_port_and_ipv6(self):
        self.assertEqual(redact_url("rtsp://u:p@host:abc/x"), "<url>")
        self.assertEqual(redact_url("rtsp://u:p@[::1]:8554/cam1"), "rtsp://[::1]:8554/cam1")

    def test_r21_seen_notifications_bounded(self):
        from stage_media_controller import MAX_SEEN_NOTIFICATIONS
        h = self._harness()
        for n in range(MAX_SEEN_NOTIFICATIONS + 50):
            h.controller.handle_messages([message(config(99, session="session-other"), f"n{n}")])
        self.assertLessEqual(len(h.controller._seen_notifications), MAX_SEEN_NOTIFICATIONS)

    def _assert_contract(self, ack):
        self.assertEqual(self.validator.validate_object("control-ack", ack), [])


class RealProcessTest(unittest.TestCase):
    def test_ac4_stop_process_terminates_then_kills_without_orphan(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(0.2)"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}))
        self.assertIsNone(proc.poll())
        code = stop_process(proc, grace_seconds=2)
        self.assertIsNotNone(proc.poll())
        self.assertNotEqual(code, 0)

    def test_ac4_stop_process_kills_when_terminate_is_ignored(self):
        script = ("import signal, time\n"
                  "signal.signal(signal.SIGTERM, lambda *a: None)\n"
                  "print('ready', flush=True)\n"
                  "while True: time.sleep(0.2)")
        if os.name == "nt":
            self.skipTest("Windows 没有可忽略的 SIGTERM，terminate 即 kill")
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, start_new_session=True)
        proc.stdout.readline()
        stop_process(proc, grace_seconds=1)
        self.assertIsNotNone(proc.poll())

    def test_ffmpeg_args_are_an_array_without_shell_or_stream_loop(self):
        args = FfmpegPublisher("ffmpeg").build_args(Path("/assets/authorized/demo/clip.mp4"), "rtsp://mediamtx:8554/cam1")
        self.assertEqual(args[0], "ffmpeg")
        self.assertIn("-re", args)
        self.assertNotIn("-stream_loop", args, "每轮一个进程，不用无限循环")
        self.assertEqual(args[-1], "rtsp://mediamtx:8554/cam1")
        self.assertTrue(all(isinstance(a, str) for a in args))
        self.assertNotIn(";", " ".join(args))

    def test_redact_url_drops_credentials(self):
        self.assertEqual(redact_url("rtsp://user:secret@cam:554/stream"), "rtsp://cam:554/stream")

    def test_mediamtx_probe_interprets_ready_404_and_errors(self):
        import io
        import urllib.error

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener_ready(request, timeout):
            self.assertTrue(request.full_url.endswith("/v3/paths/get/cam1"))
            return Response(b'{"name":"cam1","ready":true}')

        def opener_404(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 404, "not found", {}, None)

        def opener_down(request, timeout):
            raise urllib.error.URLError("refused")

        self.assertTrue(MediaMtxProbe("http://mediamtx:9997", opener=opener_ready).is_publishing("cam1"))
        self.assertFalse(MediaMtxProbe("http://mediamtx:9997", opener=opener_404).is_publishing("cam1"))
        self.assertIsNone(MediaMtxProbe("http://mediamtx:9997", opener=opener_down).is_publishing("cam1"))


if __name__ == "__main__":
    unittest.main()
