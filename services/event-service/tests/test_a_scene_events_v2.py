"""5.01：event-service 消费 A 场景事件（a_scene_event）的单测，含经 B 的 handle_observation 的接线。

运行（services/event-service 下，本机用 SQLite 让 B 的 main 能导入）：
  DATABASE_URL=sqlite:///<临时文件> PYTHONPATH=. python -m pytest -q tests/test_a_scene_events_v2.py
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///" + str(Path(tempfile.gettempdir()) / "a_scene_events_v2.db"))
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "service"))

from service.a_scene_events import A_EVENT_TYPE, build_event, scene_allowed  # noqa: E402
from service.models import Event  # noqa: E402


def clue(**over):
    base = {
        "contract_version": "2.0.0", "project_id": "proj-1", "room_id": "room-1", "session_id": "sess-1", "source_id": "camera-007",
        "stage_snapshot_id": "snap-1", "phase_id": "DETAILED", "phase_version": 1, "clue_id": "clue:S03:s03-seat-absence:x",
        "binding_status": "bound", "scene_id": "S03", "rule_id": "s03-seat-absence", "rule_version": "s03-v1",
        "occurred_at": "2026-10-26T08:00:00.000Z", "media_run_id": "camera-007:live:1", "asset_id": None, "start_ms": 0, "end_ms": 12000,
        "person_id": None, "track_id": None, "seat_id": "seat-1", "conclusion": "suspected", "risk_level": "medium",
        "reason": "工位空置 12000 ms", "observation_ids": ["obs-1"], "missing_facts": [], "quality_flags": [],
        "evidence_refs": [{"project_id": "proj-1", "room_id": "room-1", "session_id": "sess-1", "source_id": "camera-007",
                           "media_run_id": "camera-007:live:1", "asset_id": None, "evidence_type": "video"}],
    }
    base.update(over)
    return base


def message(c=None, **over):
    payload = {"scene_id": "S03", "event_type": "seat_vacancy", "risk_level": "medium", "reason": "工位空置 12000 ms",
               "conclusion": "suspected", "clue": c or clue()}
    payload.update(over)
    return {"type": A_EVENT_TYPE, "source_id": "camera-007", "session_id": "sess-1", "occurred_at": "1790000000.0",
            "payload": json.dumps(payload, ensure_ascii=False)}


def build(msg, *, last_emit=None, now=1790000000.0, allowed=None):
    return build_event(msg, event_cls=Event, last_emit={} if last_emit is None else last_emit, now=now, allowed=allowed,
                       cooldown_for=lambda t: 120.0)


class BuildEventTest(unittest.TestCase):
    def test_valid_suspected_becomes_pending_event(self):
        evt = build(message())
        self.assertEqual((evt.event_type, evt.risk_level, evt.status, evt.camera_id, evt.seat_id),
                         ("seat_vacancy", "medium", "PENDING", "camera-007", "seat-1"))
        self.assertEqual(evt.skill_code, "s03-seat-absence")

    def test_insufficient_low_event(self):
        c = clue(conclusion="insufficient", risk_level="none", missing_facts=["第二路视频"], scene_id="S05", rule_id="s05-proximity",
                 rule_version="s05-v1", clue_id="clue:S05:x")
        evt = build(message(c, scene_id="S05", event_type="close_contact", risk_level="low", reason="证据不足：两人接近停留"))
        self.assertEqual((evt.event_type, evt.risk_level), ("close_contact", "low"))
        self.assertTrue(evt.reason.startswith("证据不足："))

    def test_invalid_clue_dropped(self):
        self.assertIsNone(build(message(clue(evidence_refs=[]))), "suspected 必须引用证据")
        self.assertIsNone(build(message(clue(scene_id="S05")), ), "线索场景与派发不一致")
        self.assertIsNone(build(message(risk_level="none")))
        self.assertIsNone(build({"type": A_EVENT_TYPE, "payload": "not json"}))

    def test_phase_filter_and_s10_always_on(self):
        self.assertIsNone(build(message(), allowed={"S01"}))
        self.assertIsNotNone(build(message(), allowed={"S03"}))
        self.assertFalse(scene_allowed("S10", {"S01"}), "与 B 的 observation_allowed 同口径：S10 也要在启用场景里")
        self.assertTrue(scene_allowed("S10", {"S10"}))
        self.assertTrue(scene_allowed("S03", None))

    def test_cooldown_does_not_suppress_another_seat_or_media_run(self):
        last={}
        self.assertIsNotNone(build(message(),last_emit=last,now=1000))
        self.assertIsNotNone(build(message(clue(seat_id='seat-2')),last_emit=last,now=1001))
        new=clue(media_run_id='new-run')
        new['evidence_refs'][0]['media_run_id']='new-run'
        self.assertIsNotNone(build(message(new),last_emit=last,now=1002))

    def test_cooldown_per_source_and_type(self):
        last = {}
        self.assertIsNotNone(build(message(), last_emit=last, now=1000.0))
        self.assertIsNone(build(message(), last_emit=last, now=1060.0))
        self.assertIsNotNone(build(message(), last_emit=last, now=1121.0))


class HandleObservationTest(unittest.TestCase):
    """经 B 的 handle_observation：a_scene_event 走新分支并提交；B 原有类型不受影响。"""

    def test_handle_observation_routes_a_event(self):
        import main
        committed = []

        async def fake_commit(r, evt, frame_key=None, scope=None):
            committed.append(evt)

        original = main.commit_and_publish
        main.commit_and_publish = fake_commit
        try:
            with patch.object(main,'observation_scope',return_value=None):
                asyncio.run(main.handle_observation(None, message(), {}))
                asyncio.run(main.handle_observation(None, {"type": "unknown_type", "payload": "{}"}, {}))
        finally:
            main.commit_and_publish = original
        self.assertEqual([e.event_type for e in committed], ["seat_vacancy"])


if __name__ == "__main__":
    unittest.main()
