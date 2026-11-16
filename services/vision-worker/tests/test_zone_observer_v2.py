"""执行卡 5.1：几何、跟踪、区域事实观察。

桩验证：构造的归一化框序列 + 逻辑媒体时钟，没有真实视频与 YOLO。
运行（services/vision-worker 下）：PYTHONPATH=. python -m unittest discover -s tests -p test_zone_observer_v2.py -v
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from worker.person_tracking import IouTracker
from worker.stage_health import FrameContext
from worker.zone_geometry import (
    bbox_iou, bbox_polygon_overlap_ratio, crossing_direction, point_in_polygon, validate_bbox, validate_polygon,
)
from worker.person_tracking import IouTracker as _IouTracker  # noqa: F401
from worker.zone_observer import ZoneConfig, ZoneObserver, ZoneObserverConfig, normalize_detections

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "contracts"))
from contract_validator import ContractValidator  # noqa: E402

T0 = datetime(2026, 10, 19, 2, 0, 0, tzinfo=timezone.utc)
SEAT = [[0.10, 0.20], [0.40, 0.20], [0.40, 0.50], [0.10, 0.50]]          # 工位 A01
RESTRICTED = [[0.60, 0.10], [0.95, 0.10], [0.95, 0.90], [0.60, 0.90]]    # 限制区
LINE = {"line_id": "door", "name": "入口", "start": [0.50, 0.60], "end": [0.50, 0.95], "direction": "both"}
ZONES = {"config_version": 3,
         "zones": [{"zone_id": "seat-a01", "name": "工位A01", "kind": "seat", "seat_id": "seat-uuid-1", "polygon": SEAT},
                   {"zone_id": "restricted-1", "name": "资料区", "kind": "restricted", "seat_id": None, "polygon": RESTRICTED}],
         "entry_lines": [LINE]}
SITTING = [0.15, 0.25, 0.35, 0.65]      # 人框与工位重叠大，但脚点 (0.25, 0.65) 在工位多边形外
STANDING_FAR = [0.70, 0.30, 0.80, 0.80]  # 在限制区里


def ctx(ms, *, run="run-1", bound=True, source="camera-a"):
    fields = dict(source_id=source, occurred_at=(T0 + timedelta(milliseconds=ms)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                  media_run_id=run, start_ms=ms, asset_id="asset-1")
    if bound:
        fields.update(project_id="project-a", room_id="room-a", session_id="session-a", stage_snapshot_id="snap-1",
                      phase_id="DETAILED", phase_version=1)
    return FrameContext(**fields)


def person(bbox, conf=0.9):
    return {"bbox": list(bbox), "label": "person", "confidence": conf}


def run_script(observer, frames, *, step_ms, flags_at=None, run="run-1"):
    """frames: list of detections per frame；返回全部观察。"""
    out = []
    for index, dets in enumerate(frames):
        ms = index * step_ms
        flags = (flags_at or {}).get(index, ())
        out.extend(observer.update(ctx(ms, run=run), dets, quality_flags=flags))
    return out


class GeometryTest(unittest.TestCase):
    def test_ac5_point_on_edge_counts_inside_and_concave_overlap(self):
        poly = validate_polygon(SEAT)
        self.assertTrue(point_in_polygon((0.10, 0.30), poly), "左边界上的点算在内")
        self.assertTrue(point_in_polygon((0.40, 0.50), poly), "顶点算在内")
        self.assertTrue(point_in_polygon((0.25, 0.35), poly))
        self.assertFalse(point_in_polygon((0.41, 0.35), poly))
        concave = validate_polygon([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.5, 0.5], [0.0, 1.0]])
        self.assertAlmostEqual(bbox_polygon_overlap_ratio((0.0, 0.0, 1.0, 0.5), concave), 1.0, places=6)
        self.assertAlmostEqual(bbox_polygon_overlap_ratio((0.4, 0.6, 0.6, 1.0), concave), 0.0, places=6, msg="框整个落在凹口里")
        self.assertAlmostEqual(bbox_polygon_overlap_ratio((0.0, 0.0, 0.5, 0.75), concave), 0.34375 / 0.375, places=6)
        self.assertEqual(bbox_polygon_overlap_ratio((0.5, 0.6, 0.6, 0.7), poly), 0.0)
        # 框边恰在多边形边上：重叠比例稠密区间稳定
        self.assertAlmostEqual(bbox_polygon_overlap_ratio((0.40, 0.20, 0.50, 0.30), poly), 0.0, places=6)
        self.assertAlmostEqual(bbox_polygon_overlap_ratio((0.30, 0.20, 0.40, 0.30), poly), 1.0, places=6)

    def test_iou_and_bbox_validation(self):
        self.assertAlmostEqual(bbox_iou((0, 0, 0.5, 0.5), (0, 0, 0.5, 0.5)), 1.0)
        self.assertAlmostEqual(bbox_iou((0, 0, 0.5, 0.5), (0.25, 0, 0.75, 0.5)), 1 / 3, places=6)
        self.assertEqual(bbox_iou((0, 0, 0.1, 0.1), (0.5, 0.5, 0.6, 0.6)), 0.0)
        for bad in ([0.2, 0.2, 0.1, 0.3], [0.0, 0.0, 1.2, 0.5], [0.1, 0.1, 0.1, 0.5], [0.1, 0.1, 0.5], [float("nan"), 0, 0.5, 0.5]):
            with self.assertRaises(ValueError):
                validate_bbox(bad)
        with self.assertRaises(ValueError):
            validate_polygon([[0, 0], [1, 1]])
        with self.assertRaises(ValueError):
            validate_polygon([[0, 0], [1, 1], [2, 2]])

    def test_ac8_crossing_direction_convention(self):
        start, end = (0.5, 0.6), (0.5, 0.95)
        # 门线 start→end 朝图像下方；约定：从右侧（叉积负）到左侧（叉积正）为 in
        self.assertEqual(crossing_direction((0.6, 0.8), (0.4, 0.8), start, end), "in")
        self.assertEqual(crossing_direction((0.4, 0.8), (0.6, 0.8), start, end), "out")
        self.assertIsNone(crossing_direction((0.4, 0.3), (0.6, 0.3), start, end), "绕过线段端点不算跨线")
        self.assertIsNone(crossing_direction((0.4, 0.8), (0.45, 0.8), start, end))
        self.assertIsNone(crossing_direction((0.4, 0.8), (0.5, 0.8), start, end), "一端落在线上不判定，由观察层沿用上一侧")


class TrackerTest(unittest.TestCase):
    def test_ac4_ids_stable_then_new_after_tolerance(self):
        tracker = IouTracker("run-1", iou_threshold=0.3, max_missed_ms=1000)
        a = tracker.update([person([0.1, 0.1, 0.3, 0.5])], 0)[0]
        b = tracker.update([person([0.11, 0.1, 0.31, 0.5])], 200)[0]
        self.assertEqual(a.track_id, b.track_id)
        self.assertTrue(a.track_id.startswith("track:run-1:"))
        self.assertEqual(tracker.update([], 400), [])
        c = tracker.update([person([0.12, 0.1, 0.32, 0.5])], 900)[0]
        self.assertEqual(c.track_id, a.track_id, "容忍时间内重现沿用 id")
        tracker.update([], 1200)
        tracker.update([], 2500)
        d = tracker.update([person([0.12, 0.1, 0.32, 0.5])], 2600)[0]
        self.assertNotEqual(d.track_id, a.track_id, "超过容忍给新 id")
        two = tracker.update([person([0.12, 0.1, 0.32, 0.5]), person([0.6, 0.1, 0.8, 0.5])], 2800)
        self.assertEqual(len({t.track_id for t in two}), 2)
        with self.assertRaises(ValueError):
            tracker.update([], 100)

    def test_ac7_reset_clears_and_renumbers(self):
        tracker = IouTracker("run-1")
        tracker.update([person([0.1, 0.1, 0.3, 0.5])], 0)
        tracker.reset("run-2")
        self.assertEqual(tracker.all_tracks(), [])
        t = tracker.update([person([0.1, 0.1, 0.3, 0.5])], 0)[0]
        self.assertEqual(t.track_id, "track:run-2:1")
        with self.assertRaises(ValueError):
            IouTracker("")


class ObserverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = ContractValidator()

    def setUp(self):
        self.observer = ZoneObserver(ZONES)

    def _types(self, observations, kind):
        return [o for o in observations if o["type"] == kind]

    def test_ac1_zero_person_frames_still_report(self):
        obs = run_script(self.observer, [[]] * 16, step_ms=200)        # 3 秒无人
        counts = self._types(obs, "person_count")
        self.assertEqual(counts[0]["payload"]["count"], 0)
        self.assertGreaterEqual(len(counts), 3, "人数按心跳持续输出")
        progress = self._types(obs, "seat_vacancy_progress")
        self.assertTrue(progress)
        self.assertEqual([p["payload"]["seat_id"] for p in progress][:1], ["seat-uuid-1"])
        values = [p["payload"]["vacant_ms"] for p in progress]
        self.assertEqual(values, sorted(values))
        self.assertEqual(values[-1], 3000)
        self.assertEqual(progress[-1]["start_ms"], 0)
        for o in obs:
            self.assertEqual(self.validator.validate_object("observation", o), [], o["type"])

    def test_ac2_sitting_overlap_counts_even_if_foot_outside(self):
        obs = run_script(self.observer, [[person(SITTING)]] * 6, step_ms=200)
        occupied = self._types(obs, "seat_occupied")
        self.assertEqual(len(occupied), 0, "首帧就有人：初始态直接占用，不输出翻转")
        self.assertEqual(self._types(obs, "seat_vacancy_progress"), [])
        self.assertTrue(self.observer.status()["seats"]["seat-a01"]["occupied"])
        # 人离开 → 滞回 1000 ms 后确认空置
        obs = run_script(ZoneObserver(ZONES), [[person(SITTING)]] * 3 + [[]] * 10, step_ms=200)
        vacant = self._types(obs, "seat_vacant")
        self.assertEqual(len(vacant), 1)
        self.assertEqual((vacant[0]["start_ms"], vacant[0]["end_ms"]), (600, 1600), "600 ms 第一次无人即空置起点，1600 ms 确认")
        self.assertEqual(vacant[0]["payload"]["vacant_since_ms"], 600)
        self.assertEqual(vacant[0]["payload"]["vacant_ms"], 1000)
        # 重叠不足：站在工位旁边不算占用
        obs = run_script(ZoneObserver(ZONES), [[person([0.42, 0.20, 0.55, 0.60])]] * 8, step_ms=200)
        self.assertTrue(self._types(obs, "seat_vacancy_progress"))

    def test_ac2_occupy_after_vacancy_reports_elapsed(self):
        frames = [[]] * 11 + [[person(SITTING)]] * 5     # 2 秒无人，再有人 1 秒
        obs = run_script(self.observer, frames, step_ms=200)
        occupied = self._types(obs, "seat_occupied")
        self.assertEqual(len(occupied), 1)
        self.assertEqual(occupied[0]["payload"]["vacant_ms"], 2200, "空置到第一次看到人（2200 ms）为止；滞回期间算进占用")
        self.assertEqual(occupied[0]["payload"]["occupied_since_ms"], 2200)
        self.assertEqual((occupied[0]["start_ms"], occupied[0]["end_ms"]), (0, 2800))
        self.assertTrue(occupied[0]["payload"]["track_ids"][0].startswith("track:run-1:"))

    def test_ac3_degraded_frames_pause_timer_and_flag(self):
        flags = {i: ("camera_degraded",) for i in range(5, 15)}       # 1000～2800 ms 画面故障
        obs = run_script(self.observer, [[]] * 21, step_ms=200, flags_at=flags)
        progress = self._types(obs, "seat_vacancy_progress")
        final = progress[-1]["payload"]
        self.assertEqual(progress[-1]["end_ms"], 4000)
        self.assertEqual(final["vacant_ms"] + final["paused_ms"], 4000)
        self.assertEqual(final["paused_ms"], 2000, "故障 10 帧 × 200 ms 不计入空置")
        self.assertEqual(final["vacant_ms"], 2000)
        flagged = [o for o in obs if "camera_degraded" in o["quality_flags"]]
        self.assertTrue(flagged)
        self.assertEqual(self._types(obs, "seat_occupied"), [])
        # 采样中断：相邻帧间隔 > 2000 ms
        gap = ZoneObserver(ZONES)
        gap.update(ctx(0), [])
        gap.update(ctx(200), [])
        later = gap.update(ctx(5000), [])
        self.assertTrue(any("sampling_gap" in o["quality_flags"] for o in later))
        progress = [o for o in later if o["type"] == "seat_vacancy_progress"]
        self.assertEqual(progress[-1]["payload"]["paused_ms"], 4800)
        self.assertEqual(progress[-1]["payload"]["vacant_ms"], 200)

    def test_ac3_degraded_detections_do_not_flip_state(self):
        frames = [[]] * 8 + [[person(SITTING)]] * 6
        flags = {i: ("camera_blacked",) for i in range(8, 14)}
        obs = run_script(self.observer, frames, step_ms=200, flags_at=flags)
        self.assertEqual(self._types(obs, "seat_occupied"), [], "故障帧里的检测不可信，不翻转占用")

    def test_ac6_sampling_rate_does_not_change_durations(self):
        fast = run_script(ZoneObserver(ZONES), [[]] * 51, step_ms=200)     # 5 fps，10 秒
        slow = run_script(ZoneObserver(ZONES), [[]] * 21, step_ms=500)     # 2 fps，10 秒
        self.assertEqual(self._types(fast, "seat_vacancy_progress")[-1]["payload"]["vacant_ms"], 10000)
        self.assertEqual(self._types(slow, "seat_vacancy_progress")[-1]["payload"]["vacant_ms"], 10000)

    def test_ac7_media_run_change_resets_everything(self):
        self.observer.update(ctx(0), [person(SITTING)])
        self.observer.update(ctx(200), [person(SITTING)])
        first_ids = {t["track_id"] for t in self.observer.status()["tracks"]}
        out = self.observer.update(ctx(0, run="run-2"), [person(SITTING)])
        status = self.observer.status()
        self.assertEqual(status["media_run_id"], "run-2")
        self.assertTrue(all(t["track_id"].startswith("track:run-2:") for t in status["tracks"]))
        self.assertFalse(first_ids & {t["track_id"] for t in status["tracks"]})
        self.assertTrue(all(o["media_run_id"] == "run-2" for o in out))
        self.observer.reset("run-3")
        self.assertEqual(self.observer.status()["tracks"], [])
        with self.assertRaises(ValueError):
            self.observer.update(ctx(0, run="run-3", source="camera-b"), [])

    def test_ac8_line_crossing_reports_direction_and_respects_config(self):
        path = [[0.38, 0.70, 0.50, 0.90], [0.42, 0.70, 0.54, 0.90], [0.46, 0.70, 0.58, 0.90], [0.50, 0.70, 0.62, 0.90]]
        obs = run_script(self.observer, [[person(b)] for b in path], step_ms=200)     # 中心从左往右过门线
        crossed = self._types(obs, "line_crossed")
        self.assertEqual([c["payload"]["direction"] for c in crossed], ["out"])
        self.assertEqual(crossed[0]["payload"]["line_id"], "door")
        self.assertEqual((crossed[0]["start_ms"], crossed[0]["end_ms"]), (200, 400))
        self.assertTrue(crossed[0]["payload"]["track_id"].startswith("track:run-1:"))
        back = run_script(ZoneObserver(ZONES), [[person(b)] for b in reversed(path)], step_ms=200)
        self.assertEqual([c["payload"]["direction"] for c in self._types(back, "line_crossed")], ["in"])
        only_in = dict(ZONES, entry_lines=[dict(LINE, direction="in")])
        obs = run_script(ZoneObserver(only_in), [[person(b)] for b in path], step_ms=200)
        self.assertEqual(self._types(obs, "line_crossed"), [], "只配 in 的门线不报 out")
        obs = run_script(ZoneObserver(only_in), [[person(b)] for b in reversed(path)], step_ms=200)
        self.assertEqual([c["payload"]["direction"] for c in self._types(obs, "line_crossed")], ["in"])

    def test_ac9_restricted_zone_enter_dwell_leave(self):
        outside, inside = [0.50, 0.30, 0.62, 0.80], [0.55, 0.30, 0.67, 0.80]      # 相邻框 IoU 0.41，同一轨迹
        frames = [[person(outside)]] * 2 + [[person(inside)]] * 12 + [[person(outside)]] * 4
        obs = run_script(self.observer, frames, step_ms=200)
        entered = self._types(obs, "zone_entered")
        dwell = self._types(obs, "zone_dwell_progress")
        left = self._types(obs, "zone_left")
        self.assertEqual(len(entered), 1)
        self.assertEqual(entered[0]["payload"]["zone_id"], "restricted-1")
        self.assertTrue(dwell and dwell[-1]["payload"]["dwell_ms"] >= 1000)
        self.assertEqual(len(left), 1)
        self.assertGreaterEqual(left[0]["payload"]["dwell_ms"], 2000)
        self.assertEqual(left[0]["payload"]["track_id"], entered[0]["payload"]["track_id"])

    def test_ac10_contract_and_unbound(self):
        obs = run_script(self.observer, [[person(SITTING)], [], [], [], [], [], [], []], step_ms=200)
        for o in obs:
            self.assertEqual(self.validator.validate_object("observation", o), [], o["type"])
            self.assertEqual(o["evidence_refs"][0]["status"], "pending")
            self.assertEqual(o["evidence_refs"][0]["evidence_type"], "video")
            self.assertEqual((o["model_name"], o["model_version"]), ("zone_observer", "5.1-v1"))
            self.assertNotIn("person_id", o["payload"])
        unbound = ZoneObserver(ZONES).update(ctx(0, bound=False), [])
        self.assertTrue(unbound)
        for o in unbound:
            self.assertEqual(o["binding_status"], "unbound")
            self.assertIn("binding_missing", o["quality_flags"])
            self.assertIsNone(o["stage_snapshot_id"])
            self.assertEqual(self.validator.validate_object("observation", o), [])
        ids = [o["observation_id"] for o in obs]
        self.assertEqual(len(ids), len(set(ids)), "observation_id 不重复")

    def test_ac11_invalid_detections_ignored_and_flagged(self):
        bad = [{"bbox": [0.5, 0.5, 0.4, 0.9], "label": "person", "confidence": 0.9},
               {"bbox": [0.1, 0.1, 1.5, 0.9], "label": "person", "confidence": 0.9},
               {"bbox": [0.1, 0.1, 0.3, 0.9], "label": "person", "confidence": 2.0},
               {"bbox": [0.1, 0.1, 0.3, 0.9], "label": "chair", "confidence": 0.9},
               "garbage", {"label": "person", "confidence": 0.9}]
        obs = self.observer.update(ctx(0), bad)
        count = self._types(obs, "person_count")[0]
        self.assertEqual(count["payload"]["count"], 0)
        self.assertIn("detection_invalid", count["quality_flags"])
        low = self.observer.update(ctx(200), [person(SITTING, conf=0.1)])
        self.assertEqual(self._types(low, "person_count"), [], "低置信度只是过滤，不是非法")

    def test_ac12_zone_config_validation(self):
        with self.assertRaises(ValueError):
            ZoneConfig.from_dict({"config_version": "3", "zones": [], "entry_lines": []})
        with self.assertRaises(ValueError):
            ZoneConfig.from_dict({"config_version": 1, "zones": [{"zone_id": "z", "name": "n", "kind": "seat", "seat_id": "s", "polygon": [[0, 0], [1, 1]]}]})
        with self.assertRaises(ValueError):
            ZoneConfig.from_dict({"config_version": 1, "zones": [{"zone_id": "z", "name": "n", "kind": "seat", "seat_id": None, "polygon": SEAT}]})
        with self.assertRaises(ValueError):
            ZoneConfig.from_dict({"config_version": 1, "zones": [{"zone_id": "z", "name": "n", "kind": "restricted", "seat_id": None, "polygon": [[0, 0], [1.2, 0], [1, 1]]}]})
        with self.assertRaises(ValueError):
            ZoneConfig.from_dict({"config_version": 1, "zones": [], "entry_lines": [{"line_id": "l", "name": "n", "start": [0.1, 0.1], "end": [0.1, 0.1]}]})
        cfg = ZoneConfig.from_dict({"config_version": 0, "zones": [], "entry_lines": []})
        self.assertEqual((cfg.seats, cfg.restricted, cfg.lines), ((), (), ()))
        with self.assertRaises(ValueError):
            ZoneObserverConfig(occupancy_overlap=0)

    # ---- 2026-10-19 独立复核回归 ----
    def test_r1_degraded_frames_pause_zone_dwell_timer(self):
        frames = [[person(STANDING_FAR)]] * 25
        flags = {i: ("camera_degraded",) for i in range(10, 15)}          # 2000～2800 ms 故障
        obs = run_script(self.observer, frames, step_ms=200, flags_at=flags)
        dwell = self._types(obs, "zone_dwell_progress")
        final = dwell[-1]["payload"]
        self.assertEqual(final["paused_ms"], 1000, "故障 5 帧不计入停留")
        self.assertEqual(final["dwell_ms"] + final["paused_ms"], dwell[-1]["end_ms"], "进入起点为 0，停留 + 暂停 = 当前时刻")
        self.assertEqual(final["dwell_ms"], 3000)
        self.assertEqual(len(self._types(obs, "zone_entered")), 1, "故障后同一人不重新进入")

    def test_r2_center_on_line_counts_single_crossing_and_touch_back_is_not_crossing(self):
        on_line = [[0.44, 0.70, 0.56, 0.90], [0.46, 0.70, 0.58, 0.90]]     # 中心 0.44 → 0.50（线上）→ 0.52
        path = [[0.38, 0.70, 0.50, 0.90]] + on_line
        obs = run_script(self.observer, [[person(b)] for b in path], step_ms=200)
        self.assertEqual([c["payload"]["direction"] for c in self._types(obs, "line_crossed")], ["out"], "恰落线上只算一次跨越")
        touch = [[0.38, 0.70, 0.50, 0.90], [0.44, 0.70, 0.56, 0.90], [0.38, 0.70, 0.50, 0.90]]   # 碰线折返
        obs = run_script(ZoneObserver(ZONES), [[person(b)] for b in touch], step_ms=200)
        self.assertEqual(self._types(obs, "line_crossed"), [])

    def test_r3_small_seat_roi_fully_covered_counts_occupied(self):
        small = dict(ZONES, zones=[{"zone_id": "seat-s", "name": "小工位", "kind": "seat", "seat_id": "seat-small",
                                   "polygon": [[0.40, 0.40], [0.55, 0.40], [0.55, 0.55], [0.40, 0.55]]}])
        observer = ZoneObserver(small)
        obs = run_script(observer, [[person([0.30, 0.20, 0.60, 0.80])]] * 4, step_ms=200)     # 大人框完全盖住小工位
        self.assertTrue(observer.status()["seats"]["seat-s"]["occupied"])
        self.assertEqual(self._types(obs, "seat_vacancy_progress"), [])
        self.assertAlmostEqual(bbox_polygon_overlap_ratio((0.30, 0.20, 0.60, 0.80), validate_polygon(small["zones"][0]["polygon"])), 1.0, places=6)

    def test_r4_track_lost_inside_zone_emits_zone_left(self):
        frames = [[person(STANDING_FAR)]] * 15 + [[]] * 15
        obs = run_script(self.observer, frames, step_ms=200)
        left = self._types(obs, "zone_left")
        self.assertEqual(len(left), 1)
        self.assertIn("track_lost", left[0]["quality_flags"])
        self.assertEqual(left[0]["payload"]["left_at_ms"], 2800, "按最后可见时刻结束")
        self.assertEqual(left[0]["payload"]["dwell_ms"], 2800, "进入起点 0，最后可见 2800")
        self.assertEqual(self._types(obs, "zone_entered")[0]["payload"]["track_id"], left[0]["payload"]["track_id"])

    def test_r5_observation_ids_unique_for_equal_timestamps(self):
        first = self.observer.update(ctx(0), [])
        second = self.observer.update(ctx(0), [])
        ids = [o["observation_id"] for o in first + second]
        self.assertEqual(len(ids), len(set(ids)))

    def test_r6_polygon_self_intersection_and_duplicate_seat_rejected(self):
        with self.assertRaises(ValueError):
            validate_polygon([[0, 0], [1, 1], [1, 0], [0, 1]])
        dup = dict(ZONES, zones=[dict(ZONES["zones"][0]), dict(ZONES["zones"][0], zone_id="seat-a02")])
        with self.assertRaises(ValueError):
            ZoneConfig.from_dict(dup)

    def test_r7_pending_vacancy_reset_by_degraded_frames(self):
        frames = [[person(SITTING)]] * 5 + [[]] * 20                        # 1000 ms 起无人
        flags = {i: ("camera_blacked",) for i in range(6, 13)}             # 1200～2400 故障
        obs = run_script(self.observer, frames, step_ms=200, flags_at=flags)
        vacant = self._types(obs, "seat_vacant")
        self.assertEqual(len(vacant), 1)
        self.assertEqual(vacant[0]["payload"]["vacant_since_ms"], 2600, "故障清掉待定，故障后重新从第一帧无人算起")
        self.assertEqual(vacant[0]["end_ms"], 3600)

    def test_normalize_detections_from_pixels(self):
        out = normalize_detections([{"bbox": [128, 72, 640, 720], "class": "person", "conf": 0.8}, {"bbox": [-5, 0, 2000, 900], "class": "person", "conf": 0.5}], 1280, 720)
        self.assertEqual(out[0]["bbox"], [0.1, 0.1, 0.5, 1.0])
        self.assertEqual(out[0]["label"], "person")
        self.assertEqual(out[1]["bbox"], [0.0, 0.0, 1.0, 1.0])
        with self.assertRaises(ValueError):
            normalize_detections([], 0, 720)


if __name__ == "__main__":
    unittest.main()
