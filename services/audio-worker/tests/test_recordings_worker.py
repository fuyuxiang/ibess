"""队列claim、失败恢复、来源去重；隔离PG schema，真实音频标准化。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from pathlib import Path
import tempfile
import unittest

import numpy as np
import soundfile as sf
from sqlalchemy import create_engine, insert, select, text

from worker.recordings_worker import claim_recording, enqueue_source, normalize_audio, now, process_recording, publish_observations, tables


class RecordingsWorkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import uuid
        if not os.environ.get('WORKFLOW_TEST_DATABASE_URL'):
            raise unittest.SkipTest('Explicit dedicated PostgreSQL test database required')
        cls.admin = create_engine(os.environ['WORKFLOW_TEST_DATABASE_URL'])
        cls.schema = 'test_audio_worker_' + uuid.uuid4().hex
        with cls.admin.begin() as c:
            c.execute(text('CREATE SCHEMA ' + cls.schema))
        cls.engine = create_engine(os.environ['WORKFLOW_TEST_DATABASE_URL'], connect_args={'options':'-csearch_path='+cls.schema})
        # 使用与API同一套建表定义，镜像测试挂载API包而非复制表结构。
        from app.models import Base
        from app import audio_models
        Base.metadata.create_all(cls.engine)
        cls.recordings,cls.segments = tables(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        with cls.admin.begin() as c:
            c.execute(text('DROP SCHEMA '+cls.schema+' CASCADE'))
        cls.admin.dispose()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root/'source.wav'
        sf.write(str(self.source), np.sin(np.arange(32000)*0.03)*0.1, 16000, subtype='PCM_16')
        with self.engine.begin() as c:
            c.execute(text('TRUNCATE audio_recordings CASCADE'))

    def tearDown(self):
        self.temp.cleanup()

    def test_source_snapshot_deduplicates_and_concurrent_claim_has_one_winner(self):
        enqueue_source(self.engine,self.recordings,self.root,self.source,'audio-test')
        enqueue_source(self.engine,self.recordings,self.root,self.source,'audio-test')
        with ThreadPoolExecutor(max_workers=2) as executor:
            rows = list(executor.map(lambda _:claim_recording(self.engine,self.recordings),range(2)))
        self.assertEqual(sum(row is not None for row in rows),1)
        with self.engine.connect() as c:
            self.assertEqual(len(c.execute(select(self.recordings)).all()),1)

    def test_processing_saves_local_labels_and_pcm_with_original_samples(self):
        enqueue_source(self.engine,self.recordings,self.root,self.source,'audio-test')
        record = claim_recording(self.engine,self.recordings)
        class FakePipeline:
            model_name='test-asr';speaker_model='test-spk'
            def analyze(self,path):
                return {'text':'两段','sentence_info':[{'start':100,'end':600,'text':'第一段','spk':5},{'start':800,'end':1700,'text':'第二段','spk':7}]}
        rows=process_recording(self.engine,self.recordings,self.segments,self.root,record,FakePipeline())
        self.assertEqual([row['speaker'] for row in rows],['A','B'])
        with self.engine.connect() as c:
            saved=c.execute(select(self.recordings)).mappings().one()
            self.assertEqual(saved['status'],'ready')
            self.assertEqual(saved['speaker_count'],2)
            self.assertLessEqual(len(saved['pcm_path']),80)
            self.assertEqual(len(c.execute(select(self.segments)).all()),2)
        self.assertTrue((self.root/saved['pcm_path']).is_file())

    def test_digest_tamper_is_failed_not_completed_and_stale_claim_recovers(self):
        from sqlalchemy import update
        enqueue_source(self.engine,self.recordings,self.root,self.source,'audio-test')
        record=claim_recording(self.engine,self.recordings)
        (self.root/record['original_path']).write_bytes(b'tampered')
        self.assertIsNone(process_recording(self.engine,self.recordings,self.segments,self.root,record,None))
        with self.engine.begin() as c:
            self.assertEqual(c.scalar(select(self.recordings.c.status)),'failed')
            c.execute(update(self.recordings).values(status='processing',claim_at=now()-timedelta(minutes=20)))
        new=claim_recording(self.engine,self.recordings)
        self.assertNotEqual(new['claim_id'],record['claim_id'])

    def test_resampling_is_timeline_preserving(self):
        src=self.root/'stereo.wav';out=self.root/'resampled.wav'
        sf.write(str(src),np.zeros((44100,2)),44100,subtype='PCM_16')
        self.assertEqual(normalize_audio(src,out),1000)
        samples,rate=sf.read(str(out))
        self.assertEqual(rate,16000)
        self.assertEqual(len(samples),16000)

    def test_source_insert_failure_does_not_leave_orphan_original(self):
        from unittest.mock import patch
        with patch.object(self.engine,'begin',side_effect=RuntimeError('test database unavailable')):
            with self.assertRaises(RuntimeError):
                enqueue_source(self.engine,self.recordings,self.root,self.source,'audio-test')
        self.assertEqual(list(self.root.glob('*.original.wav')),[])

    def test_speech_duration_uses_media_time_and_does_not_span_separate_clips(self):
        class Sink:
            def __init__(self):self.messages=[]
            def xadd(self,stream,data,**kw):self.messages.append(data)
        sink=Sink()
        record={'source_id':'audio-test','source_type':'file','recording_id':'test'}
        rows=[{'start_ms':0,'end_ms':4000,'text':'测试','speaker':'A'}, {'start_ms':4100,'end_ms':7000,'text':'更多','speaker':'A'}]
        publish_observations(sink,record,rows)
        self.assertEqual(sum(m['type']=='long_talking' for m in sink.messages),1)
        sink.messages=[]
        publish_observations(sink,record,[{'start_ms':0,'end_ms':1000,'text':'短话','speaker':'A'}])
        self.assertEqual(sum(m['type']=='long_talking' for m in sink.messages),0)


if __name__=='__main__':unittest.main()
