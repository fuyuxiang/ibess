"""5.01：B 的 vision-worker 主循环接入 A 扩展后的回归（NC-1、NC-3、NC-4）。

用假的拉流、检测器、Redis 跑 B 的 main() 若干帧：开关全关时发布内容与 B 原有字段逐项一致；扩展抛异常时 B 照常发布；
扩展只能追加 B 没有的键。运行（services/vision-worker 下）：PYTHONPATH=. python -m pytest -q tests/test_main_a_hook_v2.py
"""
import json
import os
import sys
import types
import unittest
from unittest import mock

import numpy as np

if "ultralytics" not in sys.modules:                 # 本机不装 ultralytics：detector.py 只在 import 时要 YOLO 这个名字
    sys.modules["ultralytics"] = types.SimpleNamespace(YOLO=lambda *a, **k: None)

from worker import main as vw  # noqa: E402

B_KEYS = {"count", "max_conf", "frame_key", "phone_count", "phone_conf_max", "phone_boxes", "person_boxes", "count_qualified", "seats", "media_run_id", "person_tracks", "phone_postures"}
PERSONS = [{"class": "person", "conf": 0.9, "bbox": [100.0, 100.0, 200.0, 300.0]}]
PHONES = [{"class": "cell phone", "conf": 0.7, "bbox": [150.0, 120.0, 170.0, 150.0]}]


class Stop(Exception):
    pass


class FakeCap:
    def __init__(self, frames):
        self.left = frames

    def isOpened(self):
        return True

    def grab(self):
        return True

    def read(self):
        if self.left <= 0:
            raise Stop()
        self.left -= 1
        return True, np.random.default_rng(self.left).integers(60, 200, size=(240, 320, 3), dtype=np.uint8)

    def release(self):
        pass


class FakeRedis:
    def __init__(self):
        self.messages = []

    def xadd(self, stream, obs):
        self.messages.append(obs)

    def xtrim(self, *a, **k):
        pass


class FakeDetector:
    def __init__(self, *a, **k):
        pass

    def detect(self, frame):
        return [dict(p) for p in PERSONS], [dict(p) for p in PHONES]


def run_main(env, frames=3, ext=None):
    redis_client = FakeRedis()
    patches = [
        mock.patch.dict(os.environ, {"RTSP_URL": "rtsp://fake/cam1", "CAMERA_ID": "camera-001", **env}, clear=False),
        mock.patch.object(vw.cv2, "VideoCapture", lambda url: FakeCap(frames)),
        mock.patch.object(vw.redis, "from_url", lambda url: redis_client),
        mock.patch.object(vw, "init_minio", lambda: (None, None)),
        mock.patch.object(vw, "save_keyframe", lambda *a, **k: "keyframes/camera-001/x.jpg"),
        mock.patch.object(vw, "PersonDetector", FakeDetector),
        mock.patch.object(vw.time, "sleep", lambda s: None),
    ]
    if ext is not None:
        patches.append(mock.patch("worker.a_extensions.AExtensions.from_env", lambda source_id, env=None: ext))
    for p in patches:
        p.start()
    try:
        with self_raises():
            vw.main()
    finally:
        for p in reversed(patches):
            p.stop()
    return redis_client.messages


class self_raises:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is Stop


def detected(messages):
    return [json.loads(m["payload"]) for m in messages if m["type"] == "person_detected"]


class RaisingExt:
    def on_frame(self, *a):
        raise RuntimeError("boom")


class AddingExt:
    def on_frame(self, frame, persons, phones):
        return {"inside": True, "zone_name": "档案柜区", "count": 999}, [{"scene_id": "S03", "event_type": "seat_vacancy",
                                                                       "risk_level": "medium", "reason": "r", "conclusion": "suspected", "clue": {}}]


class MainHookTest(unittest.TestCase):
    def test_nc1_switch_off_publishes_only_b_fields(self):
        payloads = detected(run_main({"A_EXT_ENABLED": "0"}))
        self.assertEqual(len(payloads), 3)
        for payload in payloads:
            self.assertEqual(set(payload), B_KEYS)
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["phone_count"], 1)
            self.assertEqual(len(payload['person_tracks']),1)
            self.assertTrue(all(0<=v<=1 for v in payload['person_tracks'][0]['bbox']))

    def test_nc1_switch_off_no_a_events(self):
        messages = run_main({"A_EXT_ENABLED": "0"})
        self.assertEqual({m["type"] for m in messages}, {"person_detected"})

    def test_nc3_extension_exception_does_not_stop_b(self):
        payloads = detected(run_main({}, ext=RaisingExt()))
        self.assertEqual(len(payloads), 3)
        self.assertTrue(all(set(p) == B_KEYS for p in payloads))

    def test_extension_only_appends_new_keys_and_publishes_events(self):
        messages = run_main({}, ext=AddingExt())
        payloads = detected(messages)
        self.assertTrue(all(p["count"] == 1 for p in payloads), "不能覆盖 B 已有的键")
        self.assertTrue(all(p["inside"] is True and p["zone_name"] == "档案柜区" for p in payloads))
        a_events = [m for m in messages if m["type"] == "a_scene_event"]
        self.assertEqual(len(a_events), 3)
        self.assertEqual(json.loads(a_events[0]["payload"])["event_type"], "seat_vacancy")
        # 事件排在同一帧的 person_detected 之后
        self.assertEqual(messages[0]["type"], "person_detected")

    def test_invalid_tracking_boxes_are_skipped_and_raw_boxes_stay_unchanged(self):
        from worker.person_tracking import tracking_detections
        raw=[{'bbox':[-10,0,100,300],'conf':.9},{'bbox':[5,5,5,5]},{'bbox':[0,float('nan'),4,5]}]
        result=tracking_detections(raw,100,200)
        self.assertEqual(len(result),1)
        self.assertEqual(list(result[0]['bbox']),[0,0,1,1])
        self.assertEqual(raw[0]['bbox'],[-10,0,100,300])

    def test_nc4_default_env_initialises_without_database(self):
        payloads = detected(run_main({"DATABASE_URL": ""}))
        self.assertEqual(len(payloads), 3)
        self.assertTrue(all(B_KEYS <= set(p) for p in payloads))


class CoreHookTest(unittest.TestCase):
    def test_latest_reader_capture_timestamp_reaches_the_real_publisher(self):
        import time
        stamp=time.time()-.1
        class Latest(FakeCap):
            captured_at=stamp
            last_captured_at=stamp
        with mock.patch('worker.frame_capture.LatestFrameReader',side_effect=lambda _:Latest(1)):
            messages=run_main({'LATEST_FRAME_CAPTURE':'1','CORE_BEHAVIORS_ENABLED':'0','A_EXT_ENABLED':'0'},frames=1)
        person=next(m for m in messages if m['type']=='person_detected')
        self.assertEqual(person['occurred_at'],stamp)

    def test_delayed_camera_fact_publishes_capture_time_and_original_frame(self):
        from types import SimpleNamespace
        import cv2
        _, encoded=cv2.imencode('.jpg',np.zeros((80,80,3),dtype=np.uint8))
        core=SimpleNamespace(on_frame=lambda *a,**k:[{'occurred_at':123.5,'session_id':'captured-session',
            'capture_jpeg':encoded.tobytes(),'payload':{'bound':True,'review_only':True,'facts':[]}}])
        with mock.patch('worker.core_behaviors.CorePipeline.from_env',return_value=core):
            messages=run_main({'CORE_BEHAVIORS_ENABLED':'1','A_EXT_ENABLED':'0'},frames=1)
        behavior=next(m for m in messages if m['type']=='behavior_detected')
        self.assertEqual(behavior['occurred_at'],123.5)
        self.assertEqual(behavior['session_id'],'captured-session')
        self.assertEqual(json.loads(behavior['payload'])['frame_key'],'keyframes/camera-001/x.jpg')


if __name__ == "__main__":
    unittest.main()
