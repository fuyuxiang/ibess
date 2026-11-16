"""声学事件分析回归（7.4 S09）：合成音频验证突发检测 + API 契约。"""
import os
import sys
import unittest
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _write_wav(path: Path, segments, rate=16000):
    """segments: [(时长ms, 幅度)]，幅度为0时静音。"""
    data = []
    for duration_ms, amplitude in segments:
        count = int(rate * duration_ms / 1000)
        if amplitude == 0:
            data.append(np.zeros(count, dtype=np.int16))
        else:
            tones = np.sin(2 * np.pi * 440 * np.arange(count) / rate) * amplitude
            data.append((tones * 32767).astype(np.int16))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(np.concatenate(data).tobytes())


class AcousticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.acoustic import analyze_wav
        cls.analyze = staticmethod(analyze_wav)
        cls.tmp = Path(os.environ.get("TMPDIR", "/tmp"))

    def _wav(self, segments):
        path = self.tmp / f"acoustic_{uuid.uuid4().hex}.wav"
        _write_wav(path, segments)
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def test_01_quiet_audio_no_bursts(self):
        result = self.analyze(self._wav([(2000, 0.05)] * 3))
        self.assertEqual(result["summary"]["burst_count"], 0)
        self.assertEqual(result["summary"]["conclusion"], "quiet")

    def test_02_burst_detected(self):
        # 正常说话2秒 + 高幅喊叫1.5秒 + 正常2秒
        result = self.analyze(self._wav([(2000, 0.05), (1500, 0.9), (2000, 0.05)]))
        self.assertGreaterEqual(result["summary"]["burst_count"], 1)
        burst = result["bursts"][0]
        self.assertGreaterEqual(burst["duration_ms"], 600)
        self.assertGreater(burst["ratio"], 2.5)
        self.assertTrue(2000 <= burst["start_ms"] <= 2800)

    def test_03_short_spike_ignored(self):
        # 0.3秒小突发低于最短持续要求
        result = self.analyze(self._wav([(2000, 0.05), (300, 0.9), (2000, 0.05)]))
        self.assertEqual(result["summary"]["burst_count"], 0)

    def test_04_api_contract(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker
        from app.audio_models import AudioRecording
        from app.db import get_session
        from app.models import Base
        from app.routers import acoustic_events

        url = os.environ.get("WORKFLOW_TEST_DATABASE_URL")

        if not url:

            raise unittest.SkipTest("请显式配置 WORKFLOW_TEST_DATABASE_URL，使用专用测试数据库")
        admin_engine = create_engine(url)
        schema = "test_acoustic_" + uuid.uuid4().hex
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        try:
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine)
            recording_id = str(uuid.uuid4())
            wav = self._wav([(1000, 0.05), (1200, 0.9), (1000, 0.05)])
            with Session() as session:
                session.add(AudioRecording(recording_id=recording_id, title="声学测试", source_id="audio-upload",
                                           source_type="upload", status="ready", sha256="d" * 64,
                                           duration_ms=3200, segment_count=0,
                                           original_path=wav.name, created_at=datetime.now(timezone.utc)))
                session.commit()
            app = FastAPI()
            app.include_router(acoustic_events.router, prefix="/api")

            def override():
                with Session() as session:
                    yield session

            app.dependency_overrides[get_session] = override
            acoustic_events.AUDIO_DIR = wav.parent
            client = TestClient(app)
            response = client.post(f"/api/audio/recordings/{recording_id}/acoustic-analysis",
                                   headers={"X-Requested-With": "XMLHttpRequest"})
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertGreaterEqual(body["summary"]["burst_count"], 1)
            self.assertIn("note", body)
            missing = client.post(f"/api/audio/recordings/{uuid.uuid4()}/acoustic-analysis",
                                  headers={"X-Requested-With": "XMLHttpRequest"})
            self.assertEqual(missing.status_code, 404)
        finally:
            engine.dispose()
            with admin_engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin_engine.dispose()


if __name__ == "__main__":
    unittest.main()
