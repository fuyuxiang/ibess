"""真实本地模型测试，官方样例拼接，人数未知，不强制聚类为两人。"""
import json
import os
from pathlib import Path
import unittest
import soundfile as sf
from worker.diarization import normalize_segments
from worker.recordings_worker import SpeechPipeline


@unittest.skipUnless(os.environ.get("IBESS_REAL_AUDIO_TESTS") == "1", "Explicit GPU audio profile and authorized fixture required")
class RealDiarizationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pipeline=SpeechPipeline()

    def test_same_recording_repeat_stable_two_speakers_and_time_bounds(self):
        source=Path('/tests/two_speaker_fixture.wav')
        duration=round(sf.info(str(source)).duration*1000)
        outputs=[]
        for _ in range(2):
            raw=self.pipeline.analyze(source)
            rows=normalize_segments(raw,duration)
            labels=[row['speaker'] for row in rows]
            print('REAL_DIARIZATION',json.dumps({'duration_ms':duration,'speakers':sorted(set(labels)),
                  'segments':[(r['start_ms'],r['end_ms'],r['speaker']) for r in rows]},ensure_ascii=False),flush=True)
            self.assertEqual(set(labels),{'A','B'})
            self.assertTrue(all(0<=r['start_ms']<r['end_ms']<=duration for r in rows))
            outputs.append(labels)
        self.assertEqual(outputs[0],outputs[1])

    def test_single_speaker_not_forced_to_two_people(self):
        raw=self.pipeline.analyze(Path('/assets/asr_zh.wav'))
        duration=round(sf.info('/assets/asr_zh.wav').duration*1000)
        rows=normalize_segments(raw,duration)
        self.assertGreater(len(rows),0)
        self.assertEqual({row['speaker'] for row in rows},{'A'})


if __name__=='__main__':unittest.main()
