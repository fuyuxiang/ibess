"""评审问题的跨服务回归；真实 PostgreSQL、真实录像文件，随机 schema。"""
import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from sqlalchemy import text
from test_supervision import SupervisionTest


class SupervisionRepairsTest(SupervisionTest):
    def worker(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'event-service'))
        from service import main
        main.Session = self.Session
        main.operator_cache, main.operator_cache_at = {},0
        main.phase_scene_cache.clear();main.SUSTAIN.clear();main.LAST_KEYFRAME.clear()
        return main

    def test_real_worker_resolves_default_session_and_respects_configured_seat_rule(self):
        from app.models import Event
        event,p,r,sid = self.bound_event()
        with self.Session.begin() as db:
            db.query(Event).filter_by(event_id=event['event_id']).delete()
        created = self.client.post('/api/operators',json={'code':'seat_absence','name':'离席','version':'v7','input_types':['person_detected'],
            'output_event_type':'seat_absence','risk_level':'high','parameters':{'absence_seconds':60}}).json()
        self.assertEqual(self.client.post('/api/operators/'+created['operator_id']+'/enable').status_code,200)
        worker = self.worker();worker.refresh_operator_cache(force=True)
        bus = SimpleNamespace(xadd=AsyncMock(),xtrim=AsyncMock())
        observation = {'type':'person_detected','source_id':'cam1','session_id':'session-001','occurred_at':str(time.time()),
            'payload':json.dumps({'count':0,'seats':[{'name':'A1','absent_sec':90}]})}
        asyncio.run(worker.handle_observation(bus,observation,{}))
        result = self.client.get('/api/supervision/events',params={'session_id':sid,'room_id':r['room_id'],'camera_id':'camera-001'}).json()
        self.assertEqual(result['total'],1,result)
        self.assertEqual((result['items'][0]['skill_version'],result['items'][0]['risk_level']),('v7','high'))
        self.assertEqual(self.client.get('/api/operators').json()[0]['application_status'],'applied')
        scope_path = '/api/operators/'+created['operator_id']+'/bindings'
        binding = self.client.post(scope_path,json={'camera_id':'camera-001'})
        self.assertEqual(binding.status_code,201,binding.text)
        self.assertEqual(self.client.get('/api/operators').json()[0]['application_status'],'pending')
        worker.refresh_operator_cache(force=True)
        self.assertEqual(self.client.get('/api/operators').json()[0]['application_status'],'applied')
        self.assertEqual(self.client.delete(scope_path+'/'+binding.json()['binding_id']).status_code,200)
        self.assertEqual(self.client.get('/api/operators').json()[0]['application_status'],'pending')
        self.assertEqual(self.client.post('/api/operators/'+created['operator_id']+'/disable').status_code,200)
        worker.refresh_operator_cache(force=True)
        asyncio.run(worker.handle_observation(bus,observation,{}))
        self.assertEqual(self.client.get('/api/supervision/events').json()['total'],1)
        # Snapshot created in the worker transaction survives registry edits.
        response = self.client.put('/api/registry/rooms/'+r['room_id'],json={**r,'name':'新室名'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.detail(result['items'][0])['room'],r['name'])

    def test_ambiguous_camera_never_silently_associates_another_project(self):
        from app.core.supervision_scope import resolve_scope
        from app.models import EvalSession
        from app.phase_models import PhaseBinding
        _,p,r,sid = self.bound_event()
        with self.Session.begin() as db:
            other = str(uuid.uuid4())
            db.add(EvalSession(session_id=other,name='同源另一场次',status='running'));db.flush()
            db.add(PhaseBinding(session_id=other,project_id=p['project_id'],room_id=r['room_id'],source_id='cam1'));db.flush()
            self.assertIsNone(resolve_scope(db,'camera-001',datetime.utcnow(),'session-001'))
            self.assertEqual(resolve_scope(db,'cam1',datetime.utcnow(),sid)['session_id'],sid)

    def test_unfrozen_video_context_is_captured_before_registry_rename(self):
        event,p,r,_ = self.bound_event()
        self.successful(event,'dismiss',resolution='normal',acknowledge_missing_evidence=True,evidence_note='测试录像未接入，已现场核实正常')
        self.client.put('/api/registry/projects/'+p['project_id'],json={**p,'name':'新项目名'})
        self.client.put('/api/registry/rooms/'+r['room_id'],json={**r,'name':'新室名'})
        detail = self.detail(event)
        self.assertEqual((detail['project_name'],detail['room']),(p['name'],r['name']))
        self.assertEqual(detail['archives'][0]['snapshot']['project_name'],p['name'])

    def semantic(self, *, source='audio-01', start=0, known=True, version='v1'):
        from app.audio_models import AudioRecording
        from app.semantic_models import SemanticAnalysis
        rid,aid = str(uuid.uuid4()),str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        with self.Session.begin() as db:
            db.add(AudioRecording(recording_id=rid,title='独立录音',source_id='upload',source_type='upload',status='ready',duration_ms=180000,
                created_at=now,sha256='0'*64,original_path='test.wav'));db.flush()
            db.add(SemanticAnalysis(analysis_id=aid,recording_id=rid,task_key=uuid.uuid4().hex,scene_id='S06',window_start_ms=start,
                window_end_ms=start+60000,status='ready',request_snapshot={'source_id':source,'occurred_at':'2026-11-01T00:00:00Z',
                'time_basis':'capture_start' if known else 'unknown','model_version':version},
                result={'conclusion':'suspected','reason':'待核查','evidence':[{'start_ms':start+1000,'end_ms':start+2000,'segment_id':'x','quote':'原话'}]},
                created_at=now,updated_at=now,completed_at=now))
        return aid

    def test_semantic_bad_row_is_isolated_full_version_and_time_are_preserved(self):
        from app.core.supervision import sync_semantic_events
        from app.supervision_models import SemanticEventLink,SupervisionIntakeFailure
        bad = self.semantic(source=['invalid'])
        first = self.semantic(version='version-'+'a'*80)
        second = self.semantic(start=120000)
        unknown = self.semantic(known=False)
        with self.Session.begin() as db:
            self.assertEqual(len(sync_semantic_events(db)),3)
            self.assertIsNotNone(db.get(SupervisionIntakeFailure,bad))
            events = {aid:db.get(SemanticEventLink,aid).event_id for aid in (first,second,unknown)}
        details = {aid:self.detail({'event_id':eid}) for aid,eid in events.items()}
        self.assertEqual(details[first]['model_version'],'version-'+'a'*80)
        self.assertEqual(details[first]['occurred_at'],'2026-11-01T00:00:01Z')
        self.assertEqual(details[second]['occurred_at'],'2026-11-01T00:02:01Z')
        self.assertIsNone(details[unknown]['occurred_at'])
        self.assertIsNotNone(details[unknown]['created_at'])
        with self.Session.begin() as db:
            self.assertEqual(sync_semantic_events(db),[])

    def test_cleanup_preserves_semantic_and_regulatory_records(self):
        from app.core.supervision import sync_semantic_events
        from app.models import Event
        self.semantic()
        with self.Session.begin() as db:
            linked = sync_semantic_events(db)[0].event_id
            info_id = str(uuid.uuid4())
            db.add(Event(event_id=info_id,session_id='unbound',camera_id='cam1',event_type='person_detected',risk_level='info'))
            db.flush()
            db.query(Event).update({'created_at':datetime.utcnow()-timedelta(days=8)})
        worker = self.worker()
        with patch.object(worker.asyncio,'sleep',side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(worker.cleanup_old_data())
        with self.Session() as db:
            self.assertIsNotNone(db.query(Event).filter_by(event_id=linked).first())
            self.assertIsNone(db.query(Event).filter_by(event_id=info_id).first())

    def test_capture_time_and_business_snapshot_are_frozen_when_analysis_is_created(self):
        from app.audio_models import AudioRecording,AudioSegment
        from app.semantic_models import SemanticAnalysis
        from app.core.supervision import sync_semantic_events
        from test_semantic_analyses_v2 import SemanticAnalysisApiTests
        p,r = self.business()
        rid = str(uuid.uuid4())
        with self.Session.begin() as db:
            db.add(AudioRecording(recording_id=rid,title='录音',source_id='upload',source_type='upload',status='ready',duration_ms=180000,
                created_at=datetime.now(timezone.utc),sha256='0'*64,original_path='fixture.wav'));db.flush()
            db.add(AudioSegment(segment_id='seg-1',recording_id=rid,ordinal=0,start_ms=121000,end_ms=122000,text='按标准逐项核对。'))
        self.assertEqual(self.client.post('/api/media-bindings',json={'recording_id':rid,'room_id':r['room_id']}).status_code,201)
        capture='2026-11-01T00:00:00Z'
        saved=self.client.put(f'/api/audio/recordings/{rid}/capture-time',json={'started_at':capture})
        self.assertEqual(saved.status_code,200,saved.text)
        fixture=SemanticAnalysisApiTests('runTest');fixture.recording_id=rid
        body=fixture.request_body(project_id=p['project_id'],room_id=r['room_id'],source_id='upload',window_start_ms=120000,window_end_ms=180000,
            segments=[{'segment_id':'seg-1','speaker':'A','start_ms':121000,'end_ms':122000,'text':'按标准逐项核对。'}])
        response=self.client.post(f'/api/audio/recordings/{rid}/semantic-analyses',json=body)
        self.assertEqual(response.status_code,202,response.text)
        aid=response.json()['analysis_id']
        self.client.put('/api/registry/projects/'+p['project_id'],json={**p,'name':'改名后的项目'})
        with self.Session.begin() as db:
            analysis=db.get(SemanticAnalysis,aid)
            analysis.status='ready';analysis.completed_at=datetime.now(timezone.utc)
            analysis.result={'conclusion':'suspected','reason':'待核查','evidence':[{'start_ms':121000,'end_ms':122000,'segment_id':'seg-1','quote':'按标准'}]}
            event=sync_semantic_events(db)[0];eid=event.event_id
        detail=self.detail({'event_id':eid})
        self.assertEqual(detail['project_name'],p['name'])
        self.assertEqual(detail['occurred_at'],'2026-11-01T00:02:01Z')
        changed=self.client.put(f'/api/audio/recordings/{rid}/capture-time',json={'started_at':'2026-11-02T00:00:00Z'})
        self.assertEqual(changed.status_code,200)
        self.assertEqual(self.detail({'event_id':eid})['occurred_at'],'2026-11-01T00:02:01Z')

    def test_invalid_parameters_rejected_before_save_or_enable(self):
        response = self.client.post('/api/operators',json={'code':'crowd_gathering','name':'聚集','output_event_type':'crowd_gathering','parameters':{'count_threshold':3}})
        operator = response.json()
        for value in ('abc',True,0,-1,1.5,1001):
            bad = self.client.patch('/api/operators/'+operator['operator_id'],json={'parameters':{'count_threshold':value}})
            self.assertEqual(bad.status_code,422,(value,bad.text))
        self.assertEqual(self.client.get('/api/operators/'+operator['operator_id']).json()['parameters'],{'count_threshold':3})
        unsupported = self.client.post('/api/operators',json={'code':'not_deployed','name':'未部署','output_event_type':'custom'}).json()
        self.assertEqual(self.client.post('/api/operators/'+unsupported['operator_id']+'/enable').status_code,422)

    def test_missing_evidence_archive_and_correction_keep_original_versions(self):
        event,_,_,_ = self.bound_event()
        self.successful(event,'confirm');self.successful(event,'assign',assignee='值守岗');self.successful(event,'feedback',note='已现场核查')
        self.assertEqual(self.operation(event,'archive').status_code,422)
        self.successful(event,'archive',acknowledge_missing_evidence=True,evidence_note='录像尚未接入，依据现场核查记录归档')
        self.assertEqual(self.detail(event)['archives'][0]['resolution'],'confirmed')
        self.successful(event,'reopen',note='发现新授权材料，需要更正原认定')
        self.assertEqual((self.detail(event)['workflow_status'],self.detail(event)['resolution']),('SUPPLEMENT',None))
        self.successful(event,'dismiss',resolution='authorized_exception',acknowledge_missing_evidence=True,evidence_note='以授权登记和现场核实记录为依据')
        archives = self.detail(event)['archives']
        self.assertEqual([a['resolution'] for a in archives],['confirmed','authorized_exception'])
        result = self.client.get('/api/supervision/events',params={'queue':'archive','resolution':'authorized_exception'}).json()
        self.assertEqual(result['total'],1)
        self.assertEqual(self.client.get('/api/supervision/events',params={'resolution':'confirmed'}).json()['total'],0)

    def test_recording_window_probes_only_neighbour_and_window_segments(self):
        from app.routers import media_clips
        from app.models import Event
        event,_,_,_ = self.bound_event()
        stamp = int(time.time())
        with self.Session.begin() as db:
            db.query(Event).filter_by(event_id=event['event_id']).one().occurred_at = datetime.utcfromtimestamp(stamp)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);(root/'cam1').mkdir()
            for offset in range(-4000,41,10):
                (root/'cam1'/f'piece-{stamp+offset}.mp4').write_bytes(b'fixture')
            with patch.object(media_clips,'RECORD_ROOT',root),patch.object(media_clips,'_duration',return_value=(10,False)) as probe:
                result = self.client.get(f"/api/events/{event['event_id']}/recordings")
                self.assertEqual(result.status_code,200,result.text)
                self.assertEqual(result.json()['total'],10)
                self.assertLessEqual(probe.call_count,12)

    def test_multi_camera_preservation_survives_rotation_and_exports_verified_media(self):
        if not shutil.which('ffmpeg'):
            self.skipTest('需要已包含在API镜像中的ffmpeg')
        from app.routers import media_clips
        from app.core import media_preservation
        from app.models import Event
        event,_,_,_ = self.bound_event(source='camera-002')
        stamp = int(time.time())-180
        with self.Session.begin() as db:
            db.query(Event).filter_by(event_id=event['event_id']).one().occurred_at = datetime.utcfromtimestamp(stamp)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'recordings';(root/'cam2').mkdir(parents=True)
            saved=Path(directory)/'preserved'
            fixture=Path(directory)/'ten.mp4'
            subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=black:s=160x90:r=10','-t','10','-c:v','mpeg4',str(fixture)],check=True,timeout=20)
            for offset in range(-50,41,10):
                target=root/'cam2'/f'piece-{stamp+offset}.mp4';shutil.copyfile(fixture,target)
                os.utime(target,(time.time()-100,time.time()-100))
            with patch.object(media_clips,'RECORD_ROOT',root),patch.object(media_preservation,'HOLD_ROOT',saved):
                media_preservation.preserve_due_events(self.Session)
                self.assertEqual(self.detail(event)['evidence_status'],'READY')
                shutil.rmtree(root)
                result=self.client.get(f"/api/events/{event['event_id']}/recordings")
                self.assertEqual(result.status_code,200,result.text)
                self.assertTrue(all(item['preserved'] for item in result.json()['items']))
                playback=self.client.get(result.json()['items'][0]['playback_url'],headers={'Range':'bytes=0-15'})
                self.assertEqual(playback.status_code,206)
                self.assertIn(b'ftyp',playback.content)
                self.successful(event,'confirm');self.successful(event,'assign',assignee='值守岗');self.successful(event,'feedback');self.successful(event,'archive')
                package=self.client.get(f"/api/events/{event['event_id']}/evidence-package")
                self.assertEqual(package.status_code,200)
                with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
                    checks=json.loads(archive.read('integrity.json'))['items']
                    self.assertEqual(sum(i['status']=='verified' and i['file'].endswith('.mp4') for i in checks),10)
                next(saved.rglob('*.mp4')).write_bytes(b'tampered')
                self.assertEqual(self.detail(event)['evidence_status'],'UNAVAILABLE')
                package=self.client.get(f"/api/events/{event['event_id']}/evidence-package")
                with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
                    self.assertTrue(any(i['status']=='hash_mismatch' for i in json.loads(archive.read('integrity.json'))['items']))


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(SupervisionRepairsTest(name) for name in SupervisionRepairsTest.__dict__ if name.startswith('test_'))


if __name__=='__main__':unittest.main()
