"""私有录音及片段回放，用隔离schema和临时文件测试。"""
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
import wave

from fastapi.testclient import TestClient
from test_poc_workflow import POCWorkflowTest


def wav(samples=32000, rate=16000):
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b'\x01\x02' * samples)
    return out.getvalue()


class AudioRecordingsTest(POCWorkflowTest):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'AUDIO_DATA_DIR': self.directory.name})
        self.env.start()

    def tearDown(self):
        self.env.stop(); self.directory.cleanup()
        super().tearDown()

    def upload(self, data=None, name='test.wav'):
        return self.client.post('/api/audio/recordings', files={'file': (name, wav() if data is None else data, 'audio/wav')})

    def make_ready(self, record):
        from app.audio_models import AudioRecording, AudioSegment
        with self.Session() as s:
            row = s.get(AudioRecording, record['recording_id'])
            normalized = Path(self.directory.name) / (row.recording_id + '.pcm.wav')
            normalized.write_bytes(wav())
            row.status, row.pcm_path = 'ready', normalized.name
            row.model, row.speaker_model = 'paraformer-test', 'campp-test'
            row.speaker_count, row.segment_count = 2, 2
            self.segment_id = str(uuid.uuid4())
            s.add(AudioSegment(segment_id=self.segment_id, recording_id=row.recording_id, ordinal=0, speaker='A', start_ms=250, end_ms=750, text='测试语句'))
            s.add(AudioSegment(segment_id=str(uuid.uuid4()), recording_id=row.recording_id, ordinal=1, speaker='B', start_ms=1000, end_ms=1500, text='另一语句'))
            s.commit()

    def test_audio_upload_metadata_and_exact_clip_private_playback(self):
        r = self.upload()
        self.assertEqual(r.status_code, 202, r.text)
        item = r.json()
        self.assertEqual(item['status'], 'queued')
        self.assertEqual(item['sha256'], hashlib.sha256(wav()).hexdigest())
        self.assertIsNone(item['playback_url'])
        self.assertEqual(item['duration_ms'], 2000)
        self.assertNotIn('original_path', item)
        self.make_ready(item)
        url = '/api/audio/recordings/' + item['recording_id']
        detail = self.client.get(url)
        self.assertEqual(detail.headers['cache-control'], 'no-store')
        self.assertEqual([seg['speaker'] for seg in detail.json()['segments']], ['A', 'B'])
        clip = self.client.get(url + '/segments/' + self.segment_id + '/playback')
        self.assertEqual(clip.status_code, 200, clip.text if clip.status_code != 200 else '')
        with wave.open(io.BytesIO(clip.content), 'rb') as audio:
            self.assertEqual(audio.getnframes(), 8000)
            self.assertEqual(audio.readframes(8000), b'\x01\x02' * 8000)
        full = self.client.get(url + '/playback')
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.content, wav())
        self.assertEqual(clip.headers['cache-control'], 'no-store')
        with TestClient(self.app) as anon:
            for path in ['/api/audio/recordings', url, url + '/playback', url + '/segments/' + self.segment_id + '/playback']:
                self.assertEqual(anon.get(path).status_code, 401)

    def test_audio_invalid_formats_size_duration_and_empty_rejected(self):
        for data in [b'', b'fake RIFF WAVE audio', wav(600 * 16000 + 16000)]:
            self.assertEqual(self.upload(data).status_code, 422)
        self.assertEqual(self.upload(name='recording.mp3').status_code, 422)
        self.assertEqual(self.upload(b'x' * (20 * 1024 * 1024 + 1)).status_code, 413)
        self.assertEqual(self.client.get('/api/audio/recordings').json()['items'], [])

    def test_audio_retry_only_failed_and_requires_csrf(self):
        from app.audio_models import AudioRecording
        record = self.upload().json()
        url = '/api/audio/recordings/' + record['recording_id']
        self.assertEqual(self.client.post(url + '/retry').status_code, 409)
        with self.Session() as s:
            row = s.get(AudioRecording, record['recording_id'])
            row.status, row.error = 'failed', '测试错误'
            s.commit()
        retry = self.client.post(url + '/retry')
        self.assertEqual(retry.status_code, 200)
        self.assertIsNone(retry.json()['error'])
        self.assertEqual(retry.json()['status'], 'queued')
        self.client.headers.pop('X-CSRF-Token')
        self.assertEqual(self.upload().status_code, 403)

    def test_audio_queued_missing_file_and_cross_recording_segment(self):
        a, b = self.upload().json(), self.upload().json()
        url = '/api/audio/recordings/' + a['recording_id']
        self.assertEqual(self.client.get(url + '/playback').status_code, 409)
        self.make_ready(a)
        self.assertEqual(self.client.get('/api/audio/recordings/' + b['recording_id'] + '/segments/' + self.segment_id + '/playback').status_code, 404)
        Path(self.directory.name, a['recording_id'] + '.pcm.wav').unlink()
        self.assertEqual(self.client.get(url + '/playback').status_code, 404)
        self.assertEqual(self.client.get('/api/audio/recordings?limit=1000').status_code, 422)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(AudioRecordingsTest(name) for name in loader.getTestCaseNames(AudioRecordingsTest) if name.startswith('test_audio_'))
