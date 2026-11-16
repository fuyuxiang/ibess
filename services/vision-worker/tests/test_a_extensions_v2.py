"""5.01：A 扩展接入 B 主循环的单测（AC-1～AC-7、AC-10）。纯本机：假上下文、假时钟、合成帧，不连库不连模型。

运行（services/vision-worker 下）：PYTHONPATH=. python -m pytest -q tests/test_a_extensions_v2.py
"""
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from worker.a_context import CachedContext, RuleContext, permits_to_authorizations
from worker.a_extensions import A_EVENT_TYPE, AExtensions, clue_to_event

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
W, H = 640, 480
SNAPSHOT = {"stage_snapshot_id": "snap-1", "session_id": "sess-1", "project_id": "proj-1", "room_id": "room-1", "source_id": "camera-007",
            "phase_id": "DETAILED", "phase_version": 1, "enabled_scenes": ["S03", "S05", "S09", "S10"],
            "rule_versions": {"S03": "s03-v1", "S05": "s05-v1", "S09": "s09-v1", "S10": "s10-v1"}, "exceptions": []}
SEAT = {"zone_id": "z-seat-1", "name": "1号工位", "kind": "seat", "seat_id": "seat-1",
        "polygon": [[0.05, 0.5], [0.3, 0.5], [0.3, 0.95], [0.05, 0.95]]}
RESTRICTED = {"zone_id": "z-cab", "name": "档案柜区", "kind": "restricted",
              "polygon": [[0.6, 0.1], [0.95, 0.1], [0.95, 0.6], [0.6, 0.6]]}


class FakeSource:
    def __init__(self, ctx: RuleContext):
        self.ctx = ctx

    def load(self, source_id):
        return self.ctx


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def zone_ctx(zones, *, snapshot=SNAPSHOT, authorizations=(), exceptions=()):
    return RuleContext(zone_config={"session_id": "sess-1", "project_id": "proj-1", "room_id": "room-1", "source_id": "camera-007",
                                    "config_version": 1, "zones": list(zones), "entry_lines": []},
                       snapshot=dict(snapshot) if snapshot else None, authorizations=list(authorizations), exceptions=list(exceptions))


def make_ext(ctx: RuleContext, clock: Clock, **switches):
    wall = lambda: 1_790_000_000.0 + (clock.t - 1000.0)     # noqa: E731
    return AExtensions("camera-007", context=CachedContext(FakeSource(ctx), "camera-007", refresh_seconds=1e9, clock=clock),
                       switches=switches or None, clock=clock, wall=wall)


def textured(seed=0):
    rng = np.random.default_rng(seed)
    return rng.integers(60, 200, size=(H, W, 3), dtype=np.uint8)


def person(x1, y1, x2, y2, conf=0.9):
    return {"class": "person", "conf": conf, "bbox": [x1 * W, y1 * H, x2 * W, y2 * H]}


def run(ext, clock, frames, *, step=0.2, frame=None):
    """frames：每帧的 persons 列表。返回 (最后一帧追加键, 全部事件)。"""
    extra, events = {}, []
    img = textured() if frame is None else frame
    for persons in frames:
        extra, new = ext.on_frame(img, persons, [])
        events += new
        clock.t += step
    return extra, events


class ZonesTest(unittest.TestCase):
    def test_ac1_inside_and_zone_name_for_b_operator(self):
        clock = Clock()
        ext = make_ext(zone_ctx([RESTRICTED]), clock)
        extra, _ = ext.on_frame(textured(), [person(0.7, 0.2, 0.8, 0.5)], [])
        self.assertEqual(extra["inside"], True)
        self.assertEqual(extra["zone_name"], "档案柜区")
        extra, _ = ext.on_frame(textured(), [person(0.1, 0.1, 0.2, 0.3)], [])
        self.assertEqual(extra["inside"], False)
        self.assertNotIn("zone_name", extra)

    def test_no_zone_config_adds_nothing(self):
        clock = Clock()
        ext = make_ext(RuleContext(), clock, A_HEALTH_ENABLED=False)
        extra, events = ext.on_frame(textured(), [person(0.7, 0.2, 0.8, 0.5)], [])
        self.assertEqual(extra, {})
        self.assertEqual(events, [])

    def test_ac2_seat_vacancy_when_others_still_in_frame(self):
        clock = Clock()
        ext = make_ext(zone_ctx([SEAT]), clock, A_HEALTH_ENABLED=False)
        sitting = [person(0.1, 0.55, 0.25, 0.9), person(0.6, 0.2, 0.7, 0.5)]
        away = [person(0.6, 0.2, 0.7, 0.5)]          # 工位上的人走了，画面里还有别人
        _, events = run(ext, clock, [sitting] * 5 + [away] * 70)
        seat = [e for e in events if e["event_type"] == "seat_vacancy"]
        self.assertEqual(len(seat), 1, events)
        self.assertEqual(seat[0]["conclusion"], "suspected")
        self.assertEqual(seat[0]["scene_id"], "S03")
        self.assertNotEqual(seat[0]["risk_level"], "none")

    def test_ac2_empty_frame_left_to_b(self):
        clock = Clock()
        ext = make_ext(zone_ctx([SEAT]), clock, A_HEALTH_ENABLED=False)
        _, events = run(ext, clock, [[person(0.1, 0.55, 0.25, 0.9)]] * 5 + [[]] * 70)
        self.assertEqual([e for e in events if e["event_type"] == "seat_vacancy"], [])

    def test_ac2_leave_permit_suppresses(self):
        clock = Clock()
        start = datetime.fromtimestamp(1_790_000_000.0, timezone.utc) - timedelta(minutes=1)
        auths, refs = permits_to_authorizations([{"fact_id": "f-1", "seat_id": "seat-1", "person_id": None, "occurred_at": start, "payload": None}], 30)
        ext = make_ext(zone_ctx([SEAT], authorizations=auths, exceptions=refs), clock, A_HEALTH_ENABLED=False)
        sitting = [person(0.1, 0.55, 0.25, 0.9), person(0.6, 0.2, 0.7, 0.5)]
        _, events = run(ext, clock, [sitting] * 5 + [[person(0.6, 0.2, 0.7, 0.5)]] * 70)
        self.assertEqual([e for e in events if e["event_type"] == "seat_vacancy"], [])

    def test_unbound_rules_do_not_judge(self):
        """A 的规则对未绑定场次不判（只给一条不带事实的「未绑定」线索，扩展把它丢掉）：没有阶段快照的摄像头不出 A 规则事件。"""
        clock = Clock()
        ext = make_ext(zone_ctx([SEAT], snapshot=None), clock, A_HEALTH_ENABLED=False)
        sitting = [person(0.1, 0.55, 0.25, 0.9), person(0.6, 0.2, 0.7, 0.5)]
        extra, events = run(ext, clock, [sitting] * 5 + [[person(0.6, 0.2, 0.7, 0.5)]] * 70)
        self.assertEqual(events, [])
        self.assertIn("inside", extra, "区域事实照常追加，B 的 zone_intrusion 不依赖阶段绑定")

    def test_unbound_without_facts_emits_nothing(self):
        clock = Clock()
        ext = make_ext(zone_ctx([RESTRICTED], snapshot=None), clock, A_HEALTH_ENABLED=False)
        _, events = run(ext, clock, [[person(0.1, 0.1, 0.2, 0.3)]] * 20)
        self.assertEqual(events, [], "未绑定且画面里没有对应事实时不出事件")

    def test_ac5_s05_close_contact(self):
        clock = Clock()
        ext = make_ext(zone_ctx([RESTRICTED]), clock, A_HEALTH_ENABLED=False, A_S09_MOTION_ENABLED=False)
        pair = [person(0.30, 0.3, 0.40, 0.7), person(0.33, 0.3, 0.43, 0.7)]     # 两人框中心相距 0.03 < 0.08
        _, events = run(ext, clock, [pair] * 40)
        s05 = [e for e in events if e["scene_id"] == "S05"]
        self.assertTrue(s05, events)
        self.assertTrue(all(e["event_type"] == "close_contact" for e in s05))

    def test_s05_restricted_dwell_left_to_b(self):
        clock = Clock()
        ext = make_ext(zone_ctx([RESTRICTED]), clock, A_HEALTH_ENABLED=False)
        extra, events = run(ext, clock, [[person(0.7, 0.2, 0.8, 0.5)]] * 60)
        self.assertTrue(extra["inside"])
        self.assertEqual([e for e in events if e["scene_id"] == "S05"], [], "限制区停留由 B 的 zone_intrusion 判")

    def test_ac7_s09_fast_motion_is_insufficient(self):
        clock = Clock()
        ext = make_ext(zone_ctx([RESTRICTED]), clock, A_HEALTH_ENABLED=False, A_S05_ENABLED=False)
        frames = []
        for i in range(30):
            x = 0.05 + (i % 10) * 0.06 if (i // 10) % 2 == 0 else 0.65 - (i % 10) * 0.06   # 0.3 画面宽 / 秒（阈值 0.25），框间 IoU 仍能连成同一轨迹
            frames.append([person(x, 0.3, x + 0.2, 0.7)])
        _, events = run(ext, clock, frames)
        s09 = [e for e in events if e["scene_id"] == "S09"]
        self.assertTrue(s09, events)
        self.assertTrue(all(e["event_type"] == "order_motion" and e["reason"].startswith("证据不足：") for e in s09))

    def test_switch_off_skips_rules(self):
        clock = Clock()
        ext = make_ext(zone_ctx([SEAT, RESTRICTED]), clock, A_ZONES_ENABLED=False, A_HEALTH_ENABLED=False)
        extra, events = run(ext, clock, [[person(0.7, 0.2, 0.8, 0.5)]] * 60)
        self.assertEqual(extra, {})
        self.assertEqual(events, [])


class HealthTest(unittest.TestCase):
    def test_ac3_dark_frame_event_but_black_left_to_b(self):
        clock = Clock()
        ext = make_ext(RuleContext(snapshot=dict(SNAPSHOT)), clock)
        dark = np.random.default_rng(1).integers(0, 30, size=(H, W, 3), dtype=np.uint8)   # 平均亮度约 15（过暗），方差远大于黑屏阈值
        _, events = run(ext, clock, [[]] * 3, frame=textured())
        _, events = run(ext, clock, [[]] * 30, frame=dark)
        quality = [e for e in events if e["scene_id"] == "S10"]
        self.assertEqual(len(quality), 1, events)
        self.assertEqual(quality[0]["event_type"], "video_quality_degraded")
        clock2 = Clock()
        ext2 = make_ext(RuleContext(snapshot=dict(SNAPSHOT)), clock2)
        run(ext2, clock2, [[]] * 3, frame=textured())
        _, black_events = run(ext2, clock2, [[]] * 30, frame=np.zeros((H, W, 3), dtype=np.uint8))
        self.assertEqual([e for e in black_events if e["scene_id"] == "S10"], [], "纯黑屏归 B 的 camera_blacked")


class ReviewFixTest(unittest.TestCase):
    """5.01 提交前复核的修复：偏移须配参考图、坏区域配置不连带、健康检查限频。"""

    def _shifted_frames(self, base, n=40):
        out = [base] * 3
        shifted = np.roll(base, 60, axis=1)               # 整幅画面平移 60 像素
        return out + [shifted] * n

    def test_shift_needs_configured_reference(self):
        base = textured(5)
        clock = Clock()
        ext = make_ext(RuleContext(snapshot=dict(SNAPSHOT)), clock)
        events = []
        for frame in self._shifted_frames(base):
            events += ext.on_frame(frame, [], [])[1]
            clock.t += 0.2
        self.assertEqual([e for e in events if e["event_type"] == "camera_shifted"], [], "没配参考图：不做偏移检测")
        clock = Clock()
        ext = AExtensions("camera-007", context=CachedContext(FakeSource(RuleContext(snapshot=dict(SNAPSHOT))), "camera-007",
                                                              refresh_seconds=1e9, clock=clock),
                          clock=clock, wall=lambda: 1_790_000_000.0 + (clock.t - 1000.0), shift_reference=base)
        events = []
        for frame in self._shifted_frames(base):
            events += ext.on_frame(frame, [], [])[1]
            clock.t += 0.2
        self.assertEqual(len([e for e in events if e["event_type"] == "camera_shifted"]), 1, events)

    def test_bad_zone_config_isolated_and_logged_once(self):
        bad = {"zone_id": "z-bad", "name": "坏区", "kind": "restricted", "polygon": [[0, 0], [1, 1]]}   # 只有两个点
        clock = Clock()
        ext = make_ext(zone_ctx([bad], snapshot=dict(SNAPSHOT)), clock)
        dark = np.random.default_rng(1).integers(0, 30, size=(H, W, 3), dtype=np.uint8)
        with self.assertLogs("vision-worker.a_ext", level="WARNING") as logs:
            run(ext, clock, [[]] * 3, frame=textured())
            _, events = run(ext, clock, [[person(0.1, 0.1, 0.2, 0.3)]] * 30, frame=dark)
        self.assertEqual(len([m for m in logs.output if "区域配置" in m]), 1, "同一版坏配置只报一次")
        self.assertTrue([e for e in events if e["scene_id"] == "S10"], "区域坏了，画面质量照常")

    def test_health_interval_limits_work(self):
        clock = Clock()
        ext = make_ext(RuleContext(), clock)
        ext.health_interval_s = 1.0
        calls = []
        original = ext.health.update
        ext.health.update = lambda ctx: calls.append(ctx.frame.shape) or original(ctx)
        run(ext, clock, [[]] * 10, step=0.2)                 # 2 秒、10 帧
        self.assertEqual(len(calls), 2, "按 1 秒间隔只算 2 次")
        self.assertEqual(calls[0][1], 320, "缩到 320 宽再算")


class FakeReviewer:
    def __init__(self, fail=False):
        self.client = object()
        self.fail = fail
        self.calls = 0

    def review(self, request, ctx):
        self.calls += 1
        if self.fail:
            raise RuntimeError("upstream down")
        return {"type": "visual_fact", "payload": {"status": "failed", "error_code": "TIMEOUT", "facts": None,
                                                   "candidate": request.candidate.to_dict()}}


class VisualTest(unittest.TestCase):
    def _ext(self, reviewer):
        clock = Clock()
        from worker.visual_review import load_prompt
        prompt = load_prompt(ROOT / "prompts", "visual-review")
        ext = AExtensions("camera-007", context=None, reviewer=reviewer, prompt=prompt, model="fake", clock=clock,
                          wall=lambda: 1_790_000_000.0 + (clock.t - 1000.0), switches={"A_HEALTH_ENABLED": False})
        return ext, clock

    def test_ac4_failed_review_recorded_not_concluded(self):
        reviewer = FakeReviewer()
        ext, clock = self._ext(reviewer)
        phone = {"class": "cell phone", "conf": 0.8, "bbox": [300, 200, 340, 260]}
        facts = []
        for _ in range(20):
            extra, events = ext.on_frame(textured(), [person(0.4, 0.3, 0.6, 0.8)], [phone])
            facts += extra.get("a_visual_facts", [])
            self.assertEqual(events, [])
            clock.t += 0.2
            if facts:
                break
            import time as _t
            _t.sleep(0.02)
        self.assertGreaterEqual(reviewer.calls, 1)
        self.assertTrue(all(f["status"] == "failed" for f in facts))

    def test_ac4_reviewer_exception_does_not_break_frame(self):
        ext, clock = self._ext(FakeReviewer(fail=True))
        phone = {"class": "cell phone", "conf": 0.8, "bbox": [300, 200, 340, 260]}
        for _ in range(5):
            ext.on_frame(textured(), [person(0.4, 0.3, 0.6, 0.8)], [phone])
            clock.t += 0.2

    def test_no_model_configured_skips_review(self):
        ext = AExtensions.from_env("camera-007", {"A_HEALTH_ENABLED": "0"})
        self.assertIsNotNone(ext)
        self.assertIsNone(ext._review)


class FakeVisionClient:
    """按 OpenAI 兼容客户端的 complete(messages) 接口回一份「手里拿着纸」的合法事实。"""

    def complete(self, messages):
        import json as _json
        return _json.dumps({"object_kind": "paper", "visibility": "clear", "holding": "in_hand", "activity": "none",
                            "evidence": [{"frame_index": 0, "region": "roi"}], "notes": ""})


class FakePaperDetector:
    def detect(self, frame):
        return [{"class": "book", "conf": 0.8, "bbox": list(self.box)}]


class CarryTest(unittest.TestCase):
    def test_paper_review_returning_after_exit_still_uses_the_original_crossing(self):
        rc=RuleContext(snapshot=dict(SNAPSHOT,rule_versions={**SNAPSHOT['rule_versions'],'S08':'s08-v1'}))
        ext=AExtensions('camera-007',context=None)
        ext.switches={name:name=='A_S08_CARRY_ENABLED' for name in ext.switches}
        base={**rc.scope,'source_id':'camera-007','binding_status':'bound','media_run_id':'late-run',
              'asset_id':None,'contract_version':'2.0.0','quality_flags':[],
              'evidence_refs':[{'source_id':'camera-007','evidence_type':'video'}]}
        crossing={**base,'observation_id':'exit','type':'line_crossed','start_ms':8000,'end_ms':8000,
                  'occurred_at':'2026-10-26T08:00:08.000Z','payload':{'track_id':'t','line_id':'door','direction':'out'}}
        self.assertFalse(ext._zone_clues(rc,[crossing],people=1))
        paper={**base,'observation_id':'paper','type':'visual_fact','start_ms':1000,'end_ms':1000,
            'occurred_at':'2026-10-26T08:00:01.000Z','payload':{'status':'ok','candidate':{'kind':'paper','holder_track_id':'t'},
                'facts':{'object_kind':'paper','visibility':'clear','holding':'in_hand','activity':'reading'}}}
        ext._facts.append(paper)
        heartbeat={**base,'observation_id':'heartbeat','type':'person_count','start_ms':9000,'end_ms':9000,
                   'occurred_at':'2026-10-26T08:00:09.000Z','payload':{'tracks':[],'count':0}}
        events=ext._zone_clues(rc,[heartbeat],people=0)
        self.assertEqual(len(events),1)
        clue,event_type=events[0]
        self.assertEqual(event_type,'document_carry_out')
        self.assertEqual(clue['occurred_at'],crossing['occurred_at'])
        self.assertIn('exit',clue['observation_ids'])
        self.assertFalse(ext._zone_clues(rc,[{**heartbeat,'end_ms':10000}],people=0))
        ext._facts.append({**paper,'observation_id':'left-on-desk','start_ms':11000,'end_ms':11000,
            'payload':{**paper['payload'],'facts':{**paper['payload']['facts'],'holding':'on_table'}}})
        self.assertFalse(ext._zone_clues(rc,[{**crossing,'observation_id':'exit-again','start_ms':12000,'end_ms':12000}],people=1))

    def test_ac6_paper_holder_crossing_exit_line(self):
        import time as _t
        from worker.visual_review import VisualReviewer, load_prompt
        clock = Clock()
        ctx = RuleContext(zone_config={"session_id": "sess-1", "project_id": "proj-1", "room_id": "room-1", "source_id": "camera-007",
                                       "config_version": 1, "zones": [],
                                       "entry_lines": [{"line_id": "door", "name": "门", "start": [0.5, 0.0], "end": [0.5, 1.0], "direction": "both"}]},
                          snapshot=dict(SNAPSHOT, rule_versions={**SNAPSHOT["rule_versions"], "S08": "s08-v1"}))
        detector = FakePaperDetector()
        ext = AExtensions("camera-007", context=CachedContext(FakeSource(ctx), "camera-007", refresh_seconds=1e9, clock=clock),
                          switches={"A_HEALTH_ENABLED": False, "A_S09_MOTION_ENABLED": False, "A_S05_ENABLED": False},
                          reviewer=VisualReviewer(client=FakeVisionClient(), model_name="fake"), prompt=load_prompt(ROOT / "prompts", "visual-review"),
                          model="fake", paper_detector=detector, clock=clock, wall=lambda: 1_790_000_000.0 + (clock.t - 1000.0))
        events = []
        for i in range(25):
            x = 0.15 + i * 0.025                                  # 从门线左侧（里）走到右侧（外）
            detector.box = ((x + 0.05) * W, 0.45 * H, (x + 0.12) * W, 0.55 * H)   # 纸张框在人框里，算此人持有
            _, new = ext.on_frame(textured(i), [person(x, 0.3, x + 0.2, 0.8)], [])
            events += new
            clock.t += 0.2
            _t.sleep(0.03)                                         # 让后台复核线程跑完
        carry = [e for e in events if e["event_type"] == "document_carry_out"]
        self.assertEqual(len(carry), 1, events)
        self.assertEqual(carry[0]["scene_id"], "S08")
        self.assertEqual(carry[0]["risk_level"], "low", "纸张候选不能证明是受控资料，风险最高 low")
        self.assertIn("人工核对", carry[0]["reason"])


class EnvTest(unittest.TestCase):
    def test_master_switch_off_returns_none(self):
        self.assertIsNone(AExtensions.from_env("camera-007", {"A_EXT_ENABLED": "0"}))

    def test_defaults_on(self):
        ext = AExtensions.from_env("camera-007", {})
        self.assertTrue(all(ext.switches.values()))

    def test_clue_to_event_mapping(self):
        base = {"scene_id": "S05", "risk_level": "none", "reason": "单路", "missing_facts": ["第二路视频"]}
        self.assertIsNone(clue_to_event({**base, "conclusion": "normal"}, "x"))
        event = clue_to_event({**base, "conclusion": "insufficient"}, "x")
        self.assertEqual((event["risk_level"], event["reason"]), ("low", "证据不足：单路（缺：第二路视频）"))
        event = clue_to_event({**base, "conclusion": "suspected", "risk_level": "medium"}, "x")
        self.assertEqual(event["risk_level"], "medium")
        self.assertEqual(A_EVENT_TYPE, "a_scene_event")


class NotImportedTest(unittest.TestCase):
    def test_ac10_duplicate_rules_not_imported(self):
        # device_rules 会被 document_rules 当工具库导入（身份解析），这里只要求它的规则不被调用：S01 / S02 / 7.4 音频规则不进进程
        code = ("import sys; import worker.a_extensions; "
                "bad=[m for m in ('worker.phone_rules','worker.order_rules') if m in sys.modules]; print(bad)")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, env={"PYTHONPATH": str(ROOT), "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")})
        self.assertEqual(out.stdout.strip(), "[]", out.stderr)
        source = (ROOT / "worker" / "a_extensions.py").read_text(encoding="utf-8")
        for name in ("phone_rules", "device_rules", "order_rules", "event_adapter", "clip_pipeline"):
            self.assertNotIn(f"from worker.{name}", source)
        self.assertNotIn("RULE_PHOTO", source)
        self.assertNotIn("DeviceRules", source)


if __name__ == "__main__":
    unittest.main()
