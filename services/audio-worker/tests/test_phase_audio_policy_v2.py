import copy
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worker.phase_audio_policy import schedule_ready_recording


class AudioPolicyTests(unittest.TestCase):
    def test_only_ready_recording_schedules_s06_and_s07_without_mutating_audio(self):
        recording = {"recording_id": "recording-1", "status": "ready", "duration_ms": 60000}
        segments = [{"segment_id": "seg-1", "speaker": "A", "start_ms": 1000, "end_ms": 5000, "text": "按标准核对。"}]
        scheduled = []

        result = schedule_ready_recording(
            "recording-1", recording_loader=lambda _id: recording, segment_loader=lambda _id: segments,
            metadata_builder=lambda _record, scene: {"scene_id": scene, "recording_id": "recording-1"},
            window_builder=lambda values, metadata, duration_ms: [{**metadata, "segments": values, "duration_ms": duration_ms}],
            enqueue=lambda request: scheduled.append(copy.deepcopy(request)) or f"analysis-{len(scheduled)}",
        )

        self.assertEqual(["analysis-1", "analysis-2"], result)
        self.assertEqual(["S06", "S07"], [item["scene_id"] for item in scheduled])
        self.assertEqual("ready", recording["status"])

    def test_non_ready_recording_does_not_touch_segments_or_enqueue(self):
        recording = {"recording_id": "recording-1", "status": "processing", "duration_ms": 60000}
        segments = [{"segment_id": "seg-1"}]
        calls = []

        result = schedule_ready_recording(
            "recording-1", recording_loader=lambda _id: recording, segment_loader=lambda _id: calls.append("segments") or segments,
            metadata_builder=lambda *_: calls.append("metadata") or {}, window_builder=lambda *_: calls.append("windows") or [],
            enqueue=lambda _: calls.append("enqueue"),
        )

        self.assertEqual([], result)
        self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
