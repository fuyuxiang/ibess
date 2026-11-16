"""执行卡 9.2：截片流水线、录制索引、时间映射、证据登记器。

桩验证：假裁剪器、构造段文件与索引 CSV；没有 ffmpeg。
运行（services/media-service 下）：PYTHONPATH=. python -m unittest discover -s tests -p test_clip_pipeline_v2.py -v
"""
import csv
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from service.clip_pipeline import (
    ClipConfig, ClipJobStore, ClipPipeline, ClipRequest, FfmpegCutter, RunRegistryMapper, SegmentIndex, WallClockMapper, recorder_args,
)
from service.evidence_adapter import FileEvidenceRegistrar, HttpEvidenceRegistrar, NullRegistrar, build_evidence_ref, sha256_of_file

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "contracts"))
sys.path.insert(0, str(REPO / "scripts"))
from contract_validator import ContractValidator  # noqa: E402
from media_run_registry import MediaRunRegistry  # noqa: E402  4.2 的运行记录（只读复用）

SCOPE = {"project_id": "project-a", "room_id": "room-a", "session_id": "session-a"}
SOURCE = "camera-a"
RECORDER_START = "2026-10-19T02:00:00.000Z"


class FakeCutter:
    def __init__(self, fail_times=0, payload=b"clip-bytes"):
        self.calls = []
        self.fail_times = fail_times
        self.payload = payload

    def cut(self, segment_paths, offset, duration, out_path):
        self.calls.append(([p.name for p in segment_paths], round(offset, 3), round(duration, 3)))
        if self.fail_times > 0:
            self.fail_times -= 1
            return False
        Path(out_path).write_bytes(self.payload)
        return True


def write_index(cache_dir, segments, missing_files=()):
    """segments: [(name, start, end)]；写 CSV 并创建段文件（missing_files 不创建）。"""
    index_path = cache_dir / "index.csv"
    with index_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        for name, start, end in segments:
            writer.writerow([name, f"{start:.6f}", f"{end:.6f}"])
            if name not in missing_files:
                (cache_dir / name).write_bytes(b"\x00" * 64)
    return index_path


def contiguous(count, seconds=2.0, start=0):
    return [(f"seg_{i:06d}.mp4", start + i * seconds, start + (i + 1) * seconds) for i in range(count)]


def request(evidence_id="camera-a:run-1:seat_vacancy_progress:seat-a01:30000:video", start_ms=30000, end_ms=30000, run="run-1",
            occurred="2026-10-19T02:00:30.000Z", source=SOURCE, **kw):
    return ClipRequest(evidence_id=evidence_id, source_id=source, media_run_id=run, start_ms=start_ms, end_ms=end_ms,
                       occurred_at=occurred, asset_id="asset-1", **SCOPE, **kw)


class Harness:
    def __init__(self, tmp, *, segments, missing_files=(), cutter=None, mapper=None, config=None, store_path=None):
        self.tmp = Path(tmp)
        self.cache = self.tmp / "cache"
        self.cache.mkdir(exist_ok=True)
        self.index_path = write_index(self.cache, segments, missing_files)
        self.index = SegmentIndex(self.index_path, self.cache)
        self.cutter = cutter or FakeCutter()
        self.registrar = NullRegistrar()
        self.store = ClipJobStore(store_path or self.tmp / "jobs.json")
        self.now_s = None
        self.pipeline = ClipPipeline(SOURCE, index=self.index, mapper=mapper or WallClockMapper(RECORDER_START), cutter=self.cutter,
                                     registrar=self.registrar, store=self.store, config=config or ClipConfig(), work_dir=self.tmp / "work",
                                     clock=lambda: self.now_s if self.now_s is not None else self.index.covered_until())


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = ContractValidator()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_ac1_waits_for_post_window_then_cuts_precisely(self):
        # 事件在录制第 30 秒；窗口 20～40 秒；先只有 0～36 秒落盘
        h = Harness(self.tmp.name, segments=contiguous(18))
        job = h.pipeline.submit(request())
        self.assertEqual(job.status, "pending")
        self.assertEqual(h.pipeline.tick(), [])
        self.assertEqual(h.cutter.calls, [], "后段未落盘不截")
        write_index(h.cache, contiguous(21))                       # 0～42 秒
        changed = h.pipeline.tick()
        self.assertEqual([j.status for j in changed], ["ready"])
        self.assertEqual(len(h.cutter.calls), 1)
        names, offset, duration = h.cutter.calls[0]
        self.assertEqual(names, [f"seg_{i:06d}.mp4" for i in range(10, 20)], "只拼 20～40 秒覆盖的段")
        self.assertEqual((offset, duration), (0.0, 20.0))
        ref = changed[0].evidence
        self.assertEqual((ref["start_ms"], ref["end_ms"], ref["status"]), (20000, 40000, "ready"))
        self.assertEqual(len(ref["sha256"]), 64)

    def test_ac1_offset_inside_first_segment(self):
        h = Harness(self.tmp.name, segments=contiguous(25))
        h.pipeline.submit(request(start_ms=31500, end_ms=32500, occurred="2026-10-19T02:00:31.500Z"))
        changed = h.pipeline.tick()
        names, offset, duration = h.cutter.calls[0]
        self.assertEqual(names[0], "seg_000010.mp4")       # 21.5 秒落在 20～22 段
        self.assertEqual((offset, duration), (1.5, 21.0))
        self.assertEqual(changed[0].evidence["start_ms"], 21500)

    def test_ac2_other_source_rejected(self):
        h = Harness(self.tmp.name, segments=contiguous(25))
        with self.assertRaises(ValueError):
            h.pipeline.submit(request(source="camera-b"))
        self.assertEqual(h.store.jobs, {})
        with self.assertRaises(ValueError):
            ClipPipeline("", index=h.index, mapper=WallClockMapper(RECORDER_START), cutter=h.cutter, registrar=h.registrar)

    def test_ac3_gap_in_window_is_missing_with_ranges(self):
        segments = contiguous(25)
        del segments[13]                                            # 26～28 秒没录到
        h = Harness(self.tmp.name, segments=segments)
        h.pipeline.submit(request())
        changed = h.pipeline.tick()
        self.assertEqual([j.status for j in changed], ["missing"])
        self.assertEqual(changed[0].missing_ranges, [[26000, 28000]])
        self.assertEqual(changed[0].error_code, "RECORDING_GAP")
        self.assertEqual(h.cutter.calls, [])
        ref = changed[0].evidence
        self.assertEqual((ref["status"], ref["sha256"]), ("missing", None))
        self.assertEqual(self.validator.validate_object("evidence-ref", ref), [])
        # 索引有行但文件丢了 → 也是缺失
        h2 = Harness(tempfile.mkdtemp(dir=self.tmp.name), segments=contiguous(25), missing_files=("seg_000015.mp4",))
        h2.pipeline.submit(request())
        self.assertEqual(h2.pipeline.tick()[0].missing_ranges, [[30000, 32000]])

    def test_ac3_cut_failure_retries_then_failed(self):
        h = Harness(self.tmp.name, segments=contiguous(25), cutter=FakeCutter(fail_times=5), config=ClipConfig(max_attempts=3))
        h.pipeline.submit(request())
        self.assertEqual(h.pipeline.tick(), [])
        self.assertEqual(h.pipeline.tick(), [])
        changed = h.pipeline.tick()
        self.assertEqual([(j.status, j.attempts, j.error_code) for j in changed], [("failed", 3, "CUT_FAILED")])
        self.assertEqual(self.validator.validate_object("evidence-ref", changed[0].evidence), [])
        self.assertEqual(h.pipeline.tick(), [], "failed 不再重试")

    def test_ac4_idempotent_submit_and_restart(self):
        h = Harness(self.tmp.name, segments=contiguous(25))
        first = h.pipeline.submit(request())
        self.assertIs(h.pipeline.submit(request()), first)
        h.pipeline.tick()
        self.assertEqual(len(h.cutter.calls), 1)
        restarted = Harness(self.tmp.name, segments=contiguous(25), store_path=h.store.path)
        self.assertEqual(restarted.store.get(request().evidence_id).status, "ready")
        restarted.pipeline.submit(request())
        restarted.pipeline.tick()
        self.assertEqual(restarted.cutter.calls, [], "重启后已完成任务不重做")
        self.assertEqual(restarted.store.get(request().evidence_id).evidence["status"], "ready")
        pending_store = Path(self.tmp.name) / "pending.json"
        h3 = Harness(self.tmp.name, segments=contiguous(18), store_path=pending_store)
        h3.pipeline.submit(request())
        h3.pipeline.tick()
        h4 = Harness(self.tmp.name, segments=contiguous(25), store_path=pending_store)
        self.assertEqual([j.status for j in h4.pipeline.tick()], ["ready"], "重启后 pending 任务继续")

    def test_ac5_ready_evidence_has_sha_and_no_paths(self):
        h = Harness(self.tmp.name, segments=contiguous(25))
        h.pipeline.submit(request())
        job = h.pipeline.tick()[0]
        ref = job.evidence
        self.assertEqual(self.validator.validate_object("evidence-ref", ref), [])
        import hashlib
        self.assertEqual(ref["sha256"], hashlib.sha256(b"clip-bytes").hexdigest())
        self.assertEqual((ref["media_run_id"], ref["asset_id"], ref["evidence_type"]), ("run-1", "asset-1", "video"))
        text = json.dumps(ref)
        for forbidden in ("path", "object_key", "url", self.tmp.name):
            self.assertNotIn(forbidden, text)
        self.assertEqual(h.registrar.registered[0][0], ref)
        self.assertIsNotNone(h.registrar.registered[0][1], "ready 证据登记时带文件")

    def test_ac6_time_mapping_prerecorded_and_live(self):
        registry = MediaRunRegistry()
        run = registry.start(session_id="session-a", source_id=SOURCE, asset_id="asset-1", stage_snapshot_id="snap-1", phase_id="DETAILED",
                             phase_version=1, started_at="2026-10-19T02:00:05.000Z", asset_duration_ms=60000)
        mapper = RunRegistryMapper(registry, RECORDER_START)
        req = request(run=run.media_run_id, start_ms=30000, end_ms=30000)
        self.assertAlmostEqual(mapper.to_recording_seconds(req, 20000), 25.0)     # 素材 20 秒 = UTC 02:00:25 = 录制 25 秒
        self.assertAlmostEqual(mapper.to_recording_seconds(req, 40000), 45.0)
        self.assertIsNone(mapper.to_recording_seconds(request(run="run-unknown"), 1000))
        self.assertIsNone(mapper.to_recording_seconds(req, 61000), "超出素材时长不映射")
        live = WallClockMapper(RECORDER_START)
        self.assertAlmostEqual(live.to_recording_seconds(request(), 20000), 20.0)
        self.assertAlmostEqual(live.to_recording_seconds(request(occurred="2026-10-19T02:01:00.000Z"), 20000), 50.0)
        h = Harness(self.tmp.name, segments=contiguous(25), mapper=mapper)
        h.pipeline.submit(req)
        job = h.pipeline.tick()[0]
        self.assertEqual(job.status, "ready")
        self.assertEqual(h.cutter.calls[0][0][0], "seg_000012.mp4", "录制 25 秒落在 24～26 段")
        self.assertEqual(h.cutter.calls[0][1], 1.0)

    def test_ac7_recorder_args_and_prune(self):
        args = recorder_args("rtsp://mediamtx:8554/cam1", Path("/cache"), Path("/cache/index.csv"))
        self.assertIn("-segment_list", args)
        self.assertIn("-reset_timestamps", args)
        self.assertEqual(args[args.index("-reset_timestamps") + 1], "1")
        self.assertNotIn("-strftime", args)
        self.assertNotIn("-segment_wrap", args)
        self.assertTrue(args[-1].endswith("seg_%06d.mp4"))
        self.assertTrue(all(isinstance(a, str) for a in args))
        h = Harness(self.tmp.name, segments=contiguous(25))
        h.index.reload()
        removed = h.index.prune(keep_seconds=10, now_seconds=50)
        self.assertEqual(removed, [f"seg_{i:06d}.mp4" for i in range(19)], "结束早于 40 秒的段（0～18）删掉，38～40 的段保留")
        self.assertFalse((h.cache / "seg_000000.mp4").exists())
        self.assertTrue((h.cache / "seg_000019.mp4").exists())
        self.assertTrue((h.cache / "seg_000024.mp4").exists())
        with h.index_path.open(encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(len(rows), 25, "索引 CSV 原样不动（ffmpeg 持有句柄在追加）")
        self.assertEqual([s.filename for s in h.index.segments], [f"seg_{i:06d}.mp4" for i in range(19, 25)])

    def test_ac8_recorder_never_covers_window_becomes_missing(self):
        h = Harness(self.tmp.name, segments=contiguous(18), config=ClipConfig(max_wait_seconds=30))
        h.pipeline.submit(request())
        self.assertEqual(h.pipeline.tick(now_recording_seconds=60), [])
        changed = h.pipeline.tick(now_recording_seconds=71)
        self.assertEqual([(j.status, j.error_code) for j in changed], [("missing", "RECORDING_GAP")])
        self.assertEqual(changed[0].missing_ranges, [[36000, 40000]])
        self.assertEqual(h.cutter.calls, [])

    # ---- 2026-10-19 独立复核回归 ----
    def test_r1_prune_does_not_rewrite_index_held_by_writer(self):
        h = Harness(self.tmp.name, segments=contiguous(25))
        h.index.reload()
        with h.index_path.open("a", encoding="utf-8", newline="") as writer:      # 模拟 ffmpeg 持有句柄追加
            h.index.prune(keep_seconds=10, now_seconds=50)
            writer.write("seg_000025.mp4,50.000000,52.000000\n")
            writer.flush()
        (h.cache / "seg_000025.mp4").write_bytes(b"\x00" * 64)
        h.index.reload()
        self.assertEqual(h.index.covered_until(), 52.0, "清理后 ffmpeg 追加的新行仍能读到")
        self.assertFalse(any(p.suffix == ".tmp" for p in h.cache.iterdir()))

    def test_r2_window_clamped_to_asset_end_and_phase_switch(self):
        registry = MediaRunRegistry()
        run = registry.start(session_id="session-a", source_id=SOURCE, asset_id="asset-1", stage_snapshot_id="snap-1", phase_id="DETAILED",
                             phase_version=1, started_at=RECORDER_START, asset_duration_ms=60000)
        h = Harness(self.tmp.name, segments=contiguous(40), mapper=RunRegistryMapper(registry, RECORDER_START))
        h.pipeline.submit(request(run=run.media_run_id, start_ms=55000, end_ms=55000, occurred="2026-10-19T02:00:55.000Z"))
        job = h.pipeline.tick()[0]
        self.assertEqual(job.status, "ready")
        self.assertEqual((job.evidence["start_ms"], job.evidence["end_ms"]), (45000, 60000), "窗口夹到素材末尾，不整窗判缺失")
        self.assertEqual(job.clipped_range, [45000, 60000])
        self.assertEqual(h.cutter.calls[0][1:], (1.0, 15.0))
        self.assertEqual(self.validator.validate_object("evidence-ref", job.evidence), [])
        registry2 = MediaRunRegistry()
        run2 = registry2.start(session_id="session-a", source_id=SOURCE, asset_id="asset-1", stage_snapshot_id="snap-1", phase_id="DETAILED",
                               phase_version=1, started_at=RECORDER_START, asset_duration_ms=60000)
        registry2.stop(run2.media_run_id, stopped_at="2026-10-19T02:00:40.000Z", reason="phase_switch", planned=True)
        h2 = Harness(tempfile.mkdtemp(dir=self.tmp.name), segments=contiguous(40), mapper=RunRegistryMapper(registry2, RECORDER_START))
        h2.pipeline.submit(request(run=run2.media_run_id, start_ms=30000, end_ms=30000, occurred="2026-10-19T02:00:30.000Z"))
        job = h2.pipeline.tick()[0]
        self.assertEqual((job.status, job.evidence["start_ms"], job.evidence["end_ms"]), ("ready", 20000, 40000), "阶段切换停流：截到停流时刻")
        h3 = Harness(tempfile.mkdtemp(dir=self.tmp.name), segments=contiguous(40), mapper=RunRegistryMapper(registry2, RECORDER_START))
        h3.pipeline.submit(request(run=run2.media_run_id, start_ms=59000, end_ms=59000, occurred="2026-10-19T02:00:59.000Z", evidence_id="late"))
        job = h3.pipeline.tick()[0]
        self.assertEqual((job.status, job.error_code), ("missing", "TIME_UNMAPPED"), "整个窗口都在停流之后才是缺失")

    def test_r3_unknown_run_waits_until_max_wait(self):
        registry = MediaRunRegistry()
        h = Harness(self.tmp.name, segments=contiguous(25), mapper=RunRegistryMapper(registry, RECORDER_START), config=ClipConfig(max_wait_seconds=30))
        h.pipeline.submit(request(run="run-not-yet"), now="2026-10-19T02:00:30.000Z")
        self.assertEqual(h.pipeline.tick(now="2026-10-19T02:00:50.000Z"), [], "轮次未登记先等")
        self.assertEqual(h.store.get(request(run="run-not-yet").evidence_id).status, "pending")
        changed = h.pipeline.tick(now="2026-10-19T02:01:05.000Z")
        self.assertEqual([(j.status, j.error_code) for j in changed], [("missing", "TIME_UNMAPPED")])

    def test_r4_default_clock_marks_missing_when_recorder_dies(self):
        h = Harness(self.tmp.name, segments=contiguous(18), config=ClipConfig(max_wait_seconds=30))
        h.pipeline.submit(request())
        self.assertEqual(h.pipeline.tick(), [], "墙钟未超出最长等待")
        h.now_s = 71.0
        changed = h.pipeline.tick()
        self.assertEqual([j.status for j in changed], ["missing"], "录制停更也不会永远 pending")

    def test_r5_point_window_on_segment_boundary_does_not_crash_tick(self):
        h = Harness(self.tmp.name, segments=contiguous(25))
        h.pipeline.submit(request(evidence_id="point", start_ms=30000, end_ms=30000, pre_ms=0, post_ms=0))
        h.pipeline.submit(request())
        changed = h.pipeline.tick()
        statuses = {j.evidence_id: j.status for j in changed}
        self.assertEqual(statuses["point"], "missing")
        self.assertEqual(statuses[request().evidence_id], "ready", "零长度窗口不影响其他任务")

    def test_r6_same_evidence_id_different_window_rejected(self):
        h = Harness(self.tmp.name, segments=contiguous(25))
        h.pipeline.submit(request())
        with self.assertRaises(ValueError):
            h.pipeline.submit(request(start_ms=20000, end_ms=20000))

    def test_r7_registrar_session_dir_is_sanitized(self):
        root = Path(self.tmp.name) / "evidence"
        registrar = FileEvidenceRegistrar(root.resolve())
        ref = build_evidence_ref(evidence_id="ev:1", project_id="p", room_id="r", session_id="..", source_id=SOURCE, media_run_id="run-1",
                                 asset_id="asset-1", evidence_type="video", start_ms=0, end_ms=1000, status="missing")
        registrar.register(ref, None)
        self.assertFalse((Path(self.tmp.name) / "ev_1.json").exists(), "不能写到根目录之外")
        self.assertTrue(any(p.name == "ev_1.json" for p in root.resolve().rglob("ev_1.json")))

    def test_r8_copy_mode_seeks_before_input(self):
        copy = FfmpegCutter(precise=False).build_args(Path("/tmp/list.txt"), 1.5, 20.0, Path("/tmp/out.mp4"))
        self.assertLess(copy.index("-ss"), copy.index("-i"), "copy 模式在输入侧 seek（关键帧对齐）")
        precise = FfmpegCutter().build_args(Path("/tmp/list.txt"), 1.5, 20.0, Path("/tmp/out.mp4"))
        self.assertGreater(precise.index("-ss"), precise.index("-i"), "精确模式在输出侧裁")

    def test_ac9_file_registrar_writes_private_dir_and_metadata(self):
        root = Path(self.tmp.name) / "evidence"
        registrar = FileEvidenceRegistrar(root.resolve())
        clip = Path(self.tmp.name) / "clip.mp4"
        clip.write_bytes(b"abc")
        ref = build_evidence_ref(evidence_id="ev:1", **SCOPE, source_id=SOURCE, media_run_id="run-1", asset_id="asset-1", evidence_type="video",
                                 start_ms=0, end_ms=1000, status="ready", sha256=sha256_of_file(clip))
        registrar.register(ref, clip)
        stored = root.resolve() / "session-a" / "ev_1.mp4"
        self.assertTrue(stored.is_file())
        self.assertEqual(sha256_of_file(stored), ref["sha256"])
        meta = json.loads((root.resolve() / "session-a" / "ev_1.json").read_text(encoding="utf-8"))
        self.assertEqual(meta, ref)
        tampered = dict(ref, sha256="0" * 64)
        with self.assertRaises(ValueError):
            registrar.register(tampered, clip)
        with self.assertRaises(ValueError):
            FileEvidenceRegistrar(Path("relative"))
        with self.assertRaises(ValueError):
            registrar.register(dict(ref, object_key="x"), clip)
        missing = build_evidence_ref(evidence_id="ev:2", **SCOPE, source_id=SOURCE, media_run_id="run-1", asset_id="asset-1",
                                     evidence_type="video", start_ms=0, end_ms=1000, status="missing")
        registrar.register(missing, None)
        self.assertTrue((root.resolve() / "session-a" / "ev_2.json").is_file())

    def test_ac10_ffmpeg_cutter_args(self):
        precise = FfmpegCutter().build_args(Path("/tmp/list.txt"), 1.5, 20.0, Path("/tmp/out.mp4"))
        self.assertEqual(precise[0], "ffmpeg")
        self.assertIn("concat", precise)
        self.assertEqual(precise[precise.index("-ss") + 1], "1.500")
        self.assertEqual(precise[precise.index("-t") + 1], "20.000")
        self.assertIn("libx264", precise)
        copy = FfmpegCutter(precise=False).build_args(Path("/tmp/list.txt"), 0, 20, Path("/tmp/out.mp4"))
        self.assertIn("copy", copy)
        self.assertNotIn("libx264", copy)
        self.assertNotIn(";", " ".join(precise))

    def test_registrar_failure_retries_and_http_stub_codes(self):
        class Boom:
            def __init__(self):
                self.calls = 0

            def register(self, ref, path):
                self.calls += 1
                if ref["status"] == "ready":
                    raise RuntimeError("HTTP_503")
                return ref

        h = Harness(self.tmp.name, segments=contiguous(25), config=ClipConfig(max_attempts=2))
        h.registrar = h.pipeline.registrar = Boom()
        h.pipeline.submit(request())
        self.assertEqual(h.pipeline.tick(), [])
        changed = h.pipeline.tick()
        self.assertEqual([(j.status, j.error_code) for j in changed], [("failed", "REGISTER_RuntimeError")])
        import io
        import urllib.error

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener_ok(req, timeout):
            self.assertEqual(req.get_method(), "PUT")
            self.assertTrue(req.full_url.endswith("/internal/evidence/ev:1"))
            self.assertTrue(req.get_header("Authorization").startswith("Bearer "))
            return Response(b'{"evidence_id":"ev:1","status":"ready"}')

        def opener_fail(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)

        ref = build_evidence_ref(evidence_id="ev:1", **SCOPE, source_id=SOURCE, media_run_id="run-1", asset_id="asset-1", evidence_type="video",
                                 start_ms=0, end_ms=1000, status="ready", sha256="a" * 64)
        self.assertEqual(HttpEvidenceRegistrar("http://api:8000", "tok", opener=opener_ok).register(ref, None)["evidence_id"], "ev:1")
        with self.assertRaises(RuntimeError) as ctx:
            HttpEvidenceRegistrar("http://api:8000", "tok", opener=opener_fail).register(ref, None)
        self.assertEqual(str(ctx.exception), "HTTP_401")
        self.assertNotIn("tok", repr(HttpEvidenceRegistrar("http://api:8000", "tok")))

    def test_request_validation_and_evidence_ref_rules(self):
        with self.assertRaises(ValueError):
            request(start_ms=5, end_ms=1)
        with self.assertRaises(ValueError):
            request(evidence_id="")
        with self.assertRaises(ValueError):
            request(occurred="2026-10-19 02:00:00")
        with self.assertRaises(ValueError):
            build_evidence_ref(evidence_id="e", **SCOPE, source_id=SOURCE, media_run_id="r", asset_id=None, evidence_type="video",
                               start_ms=0, end_ms=1, status="ready", sha256=None)
        with self.assertRaises(ValueError):
            build_evidence_ref(evidence_id="e", **SCOPE, source_id=SOURCE, media_run_id=None, asset_id=None, evidence_type="video",
                               start_ms=0, end_ms=1, status="ready", sha256="a" * 64)
        gap = build_evidence_ref(evidence_id="g", **SCOPE, source_id=SOURCE, media_run_id="r", asset_id=None, evidence_type="gap",
                                 start_ms=None, end_ms=None, status="missing")
        self.assertEqual(self.validator.validate_object("evidence-ref", gap), [])

    def test_index_tolerates_bad_rows_and_duplicate_filenames(self):
        cache = Path(self.tmp.name) / "c"
        cache.mkdir()
        path = cache / "index.csv"
        path.write_text("seg_000000.mp4,0,2\nbad,row\nseg_000001.mp4,2,x\nseg_000001.mp4,2,4\nseg_000002.mp4,5,4\n", encoding="utf-8")
        for name in ("seg_000000.mp4", "seg_000001.mp4"):
            (cache / name).write_bytes(b"\x00")
        index = SegmentIndex(path, cache)
        index.reload()
        self.assertEqual([s.filename for s in index.segments], ["seg_000000.mp4", "seg_000001.mp4"])
        self.assertEqual(index.covered_until(), 4.0)
        self.assertEqual(index.gaps(0, 6), [(4.0, 6)])


class WiringGateTest(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("CLIP_PIPELINE_WIRED") == "1", "10.1 接线后设 CLIP_PIPELINE_WIRED=1 启用")
    def test_old_clip_path_removed_from_main(self):
        main = (REPO / "services/media-service/service/main.py").read_text(encoding="utf-8")
        for token in ("-strftime", "segment_wrap", "extract_clip", "upload_evidence", "set_bucket_policy"):
            self.assertNotIn(token, main, f"旧机制 {token} 不得复活")


if __name__ == "__main__":
    unittest.main()
