"""联合监管工作区：隔离 PostgreSQL、实际鉴权与真实媒体文件回归。"""
import io
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
import wave
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from sqlalchemy import text
from test_supervision import SupervisionTest


class WorkspaceTest(SupervisionTest):
    def bound_event(self,source='camera-001'):
        result=super().bound_event(source)
        from app.phase_models import PhaseBinding
        with self.Session.begin() as db:
            db.query(PhaseBinding).filter_by(session_id=result[3]).one().created_at=datetime.now(timezone.utc)-timedelta(hours=1)
        return result

    def mutation(self,event,path,**values):
        body={'expected_version':self.detail(event)['workflow_version'],'request_id':str(uuid.uuid4()),'note':'核查原片并登记依据',**values}
        return self.client.request('PUT' if path=='video-window' else 'POST',f"/api/supervision/events/{event['event_id']}/{path}",json=body)

    def channel(self,sid,source='camera-002',kind='video'):
        response=self.client.post(f'/api/supervision/sessions/{sid}/channels',json={'source_id':source,'name':'补充来源','kind':kind})
        self.assertEqual(response.status_code,201,response.text)
        from app.supervision_models import SupervisionChannel
        with self.Session.begin() as db:
            db.query(SupervisionChannel).filter_by(channel_id=response.json()['channel_id']).one().created_at=datetime.utcnow()-timedelta(hours=1)
        return response.json()

    def recording(self,room,start,directory):
        from app.audio_models import AudioRecording,AudioSegment
        rid=str(uuid.uuid4());file=Path(directory)/(rid+'.wav')
        with wave.open(str(file),'wb') as out:
            out.setnchannels(1);out.setsampwidth(2);out.setframerate(8000);out.writeframes(b'\0\0'*8000*10)
        with self.Session.begin() as db:
            db.add(AudioRecording(recording_id=rid,title='核查原音',source_id='upload',source_type='upload',status='ready',duration_ms=10000,
                created_at=datetime.now(timezone.utc),sha256=hashlib.sha256(file.read_bytes()).hexdigest(),original_path=file.name));db.flush()
            segment=str(uuid.uuid4());db.add(AudioSegment(segment_id=segment,recording_id=rid,ordinal=0,start_ms=1000,end_ms=5000,text='机器原始转写',speaker='A'))
        self.assertEqual(self.client.post('/api/media-bindings',json={'recording_id':rid,'room_id':room}).status_code,201)
        if start:
            response=self.client.put(f'/api/audio/recordings/{rid}/capture-time',json={'started_at':start.isoformat()+'Z'})
            self.assertEqual(response.status_code,200,response.text)
        return rid,segment

    def login_team(self,username,role):
        created=self.client.post('/api/auth/team',json={'username':username,'display_name':username,'password':'IntegrationTeam12345','role':role})
        self.assertEqual(created.status_code,201,created.text)
        client=TestClient(self.app,headers={'X-Requested-With':'XMLHttpRequest'})
        logged=client.post('/api/auth/login',json={'username':username,'password':'IntegrationTeam12345'})
        self.assertEqual(logged.status_code,200,logged.text)
        client.headers['X-CSRF-Token']=logged.json()['csrf_token']
        self.addCleanup(client.close)
        return client,created.json()

    def test_auxiliary_scope_is_unique_and_session_summary_is_deduplicated(self):
        from app.core.supervision_scope import resolve_scope
        from app.models import EvalSession
        from app.phase_models import PhaseBinding
        event,p,r,sid=self.bound_event();self.channel(sid)
        self.channel(sid,'mic-main','audio')
        with self.Session() as db:
            self.assertEqual(resolve_scope(db,'cam2',datetime.utcnow(),sid)['session_id'],sid)
        dup=self.client.post(f'/api/supervision/sessions/{sid}/channels',json={'source_id':'cam2','name':'别名'})
        self.assertEqual(dup.status_code,409)
        other=str(uuid.uuid4())
        with self.Session.begin() as db:
            db.add(EvalSession(session_id=other,name='第二场次',status='running'));db.flush()
            db.add(PhaseBinding(session_id=other,project_id=p['project_id'],room_id=r['room_id'],source_id='camera-003'))
        conflict=self.client.post(f'/api/supervision/sessions/{other}/channels',json={'source_id':'cam2','name':'重复物理源'})
        self.assertEqual(conflict.status_code,409,conflict.text)
        self.assertEqual(self.client.get('/api/supervision/summary').json()['sessions'],2)
        self.assertEqual(self.client.get('/api/supervision/sessions',params={'page_size':1}).json()['total'],2)

    def test_manual_mark_is_timezone_scoped_idempotent_and_keeps_complete_span(self):
        event,_,_,sid=self.bound_event();self.channel(sid)
        end=datetime.now(timezone.utc)-timedelta(seconds=1);start=end-timedelta(minutes=3)
        body={'request_id':str(uuid.uuid4()),'session_id':sid,'source_id':'camera-002','started_at':start.isoformat(),'ended_at':end.isoformat(),'reason':'人工发现工位连续空置'}
        first=self.client.post('/api/supervision/events/manual',json=body)
        self.assertEqual(first.status_code,201,first.text)
        again=self.client.post('/api/supervision/events/manual',json=body)
        self.assertEqual(again.json()['event_id'],first.json()['event_id'])
        self.assertTrue(again.json()['duplicate'])
        detail=self.detail(first.json())
        self.assertEqual((detail['origin'],detail['workflow_status']),('manual','PENDING'))
        self.assertEqual(detail['behavior']['end_basis'],'manual')
        window=detail['context_evidence']['videos'][0]
        self.assertGreater((datetime.fromisoformat(window['ended_at'].replace('Z','+00:00'))-datetime.fromisoformat(window['started_at'].replace('Z','+00:00'))).total_seconds(),269)
        self.assertEqual(self.client.post('/api/supervision/events/manual',json={**body,'reason':'不同内容复用请求'}).status_code,409)
        self.assertEqual(self.client.post('/api/supervision/events/manual',json={**body,'request_id':str(uuid.uuid4()),'started_at':start.replace(tzinfo=None).isoformat()}).status_code,422)
        self.assertEqual(self.client.get(f"/api/events/{event['event_id']}/recordings",params={'source_id':'camera-003'}).status_code,422)

    def test_joint_audio_freezes_capture_time_annotations_preserve_raw_and_archive_is_readonly(self):
        from app.audio_models import AudioSegment
        from app.models import Event
        event,_,r,sid=self.bound_event()
        at=datetime.utcnow()-timedelta(seconds=60)
        with self.Session.begin() as db:db.query(Event).filter_by(event_id=event['event_id']).one().occurred_at=at
        with tempfile.TemporaryDirectory() as directory:
            rid,segment=self.recording(r['room_id'],at-timedelta(seconds=2),directory)
            unknown,_=self.recording(r['room_id'],None,directory)
            self.assertEqual([a['recording_id'] for a in self.detail(event)['context_evidence']['audio_candidates']],[rid])
            self.assertEqual(self.mutation(event,'audio-links',recording_id=unknown).status_code,422)
            linked=self.mutation(event,'audio-links',recording_id=rid)
            self.assertEqual(linked.status_code,200,linked.text)
            self.client.put(f'/api/audio/recordings/{rid}/capture-time',json={'started_at':(at+timedelta(days=1)).isoformat()+'Z'})
            self.assertEqual(self.detail(event)['context_evidence']['audio'][0]['capture_started_at'],(at-timedelta(seconds=2)).isoformat()+'Z')
            annotated=self.mutation(event,'annotations',recording_id=rid,segment_id=segment,kind='transcript_correction',correction='人工核听勘误')
            self.assertEqual(annotated.status_code,201,annotated.text)
            with self.Session() as db:self.assertEqual(db.get(AudioSegment,segment).text,'机器原始转写')
            self.assertEqual(self.detail(event)['context_evidence']['annotations'][0]['original']['text'],'机器原始转写')
            self.successful(event,'dismiss',resolution='normal',acknowledge_missing_evidence=True,evidence_note='视频尚未接入，依据现场核实归档')
            self.assertEqual(self.mutation(event,'remove-link',kind='audio',reference=rid).status_code,409)
            archived=self.detail(event)['archives'][0]['snapshot']
            self.assertEqual(archived['context_evidence']['annotations'][0]['correction'],'人工核听勘误')

    def test_unlink_is_audited_and_does_not_remove_original_source(self):
        event,_,_,sid=self.bound_event();self.channel(sid)
        context=self.detail(event)['context_evidence'];w=context['videos'][0]
        linked=self.mutation(event,'video-window',source_id='camera-002',started_at=w['started_at'],ended_at=w['ended_at'])
        self.assertEqual(linked.status_code,200,linked.text)
        self.assertEqual(self.mutation(event,'remove-link',kind='video',reference='camera-001').status_code,422)
        self.assertEqual(self.mutation(event,'remove-link',kind='video',reference='camera-002').status_code,200)
        detail=self.detail(event)
        self.assertEqual([v['source_id'] for v in detail['context_evidence']['videos']],['camera-001'])
        self.assertTrue(any(a['action']=='evidence_unlink' for a in detail['audit']))

    def test_team_roles_receive_own_task_before_feedback_and_revoked_session_stops_access(self):
        event,_,_,_=self.bound_event()
        viewer,view=self.login_team('workspace_view','viewer');operator,account=self.login_team('workspace_operator','operator')
        self.assertEqual(viewer.get('/api/supervision/events').status_code,200)
        self.assertEqual(viewer.get(f"/api/events/{event['event_id']}/evidence-package").status_code,403)
        self.assertEqual(viewer.post('/api/supervision/events/manual',json={}).status_code,403)
        self.assertEqual(operator.post('/api/operators',json={}).status_code,403)
        self.successful(event,'confirm');self.successful(event,'assign',assignee='workspace_operator')
        def action():
            d=self.detail(event)
            return operator.post(f"/api/supervision/events/{event['event_id']}/actions",json={'action':'feedback','note':'已核查现场情况','request_id':str(uuid.uuid4()),'expected_status':d['workflow_status'],'expected_version':d['workflow_version']})
        self.assertEqual(action().status_code,403)
        d=self.detail(event);body={'request_id':str(uuid.uuid4()),'note':'本人接收现场处置任务','expected_version':d['workflow_version']}
        received=operator.post(f"/api/supervision/events/{event['event_id']}/receive",json=body)
        self.assertEqual(received.status_code,200,received.text)
        self.assertTrue(operator.post(f"/api/supervision/events/{event['event_id']}/receive",json=body).json()['duplicate'])
        self.assertEqual(self.detail(event)['task_receipt']['received_by'],'workspace_operator')
        self.assertEqual(action().status_code,200)
        disabled=self.client.patch('/api/auth/team/'+account['account_id'],json={'active':False})
        self.assertEqual(disabled.status_code,200,disabled.text)
        self.assertEqual(operator.get('/api/supervision/events').status_code,401)

    def test_zero_event_report_keeps_unknown_intervals_and_is_immutable_escaped_and_exportable(self):
        from app.models import Event,EvalSession
        from app.supervision_models import SupervisionCoverageSample
        event,p,r,sid=self.bound_event()
        start=datetime.utcnow()-timedelta(minutes=10)
        with self.Session.begin() as db:
            db.query(Event).filter_by(event_id=event['event_id']).delete()
            db.query(EvalSession).filter_by(session_id=sid).one().start_at=start
            db.add(SupervisionCoverageSample(session_id=sid,source_id='camera-001',observed_at=start,
                states={'stream_status':'online','analysis_status':'running','recording_status':'recent'}))
        response=self.client.post(f'/api/supervision/sessions/{sid}/reports')
        self.assertEqual(response.status_code,201,response.text)
        report=response.json();snapshot=report['snapshot']
        self.assertEqual(snapshot['events'],[])
        intervals=snapshot['coverage'][0]['intervals']
        self.assertEqual([i['status'] for i in intervals],['online','unknown'])
        self.assertEqual(intervals[0]['seconds'],60)
        self.client.put('/api/registry/projects/'+p['project_id'],json={**p,'name':'<script>unsafe</script>'})
        self.assertEqual(self.client.get('/api/supervision/reports/'+report['report_id']).json()['snapshot'],snapshot)
        newer=self.client.post(f'/api/supervision/sessions/{sid}/reports').json()
        exported=self.client.get('/api/supervision/reports/'+newer['report_id']+'/download')
        self.assertEqual(exported.status_code,200,exported.text)
        self.assertIn('&lt;script&gt;unsafe&lt;/script&gt;',exported.text)
        self.assertNotIn('<script>unsafe</script>',exported.text)
        self.assertEqual(len(self.client.get(f'/api/supervision/sessions/{sid}/reports').json()),2)

    def test_semantic_session_is_resolved_from_capture_time_even_with_multiple_sources(self):
        from test_semantic_analyses_v2 import SemanticAnalysisApiTests
        from app.semantic_models import SemanticAnalysis
        event,p,r,sid=self.bound_event();self.channel(sid)
        with tempfile.TemporaryDirectory() as directory:
            rid,segment=self.recording(r['room_id'],datetime.utcnow()-timedelta(seconds=10),directory)
            fixture=SemanticAnalysisApiTests('runTest');fixture.recording_id=rid
            body=fixture.request_body(project_id=p['project_id'],room_id=r['room_id'],session_id='untrusted-session',
                segments=[{'segment_id':segment,'speaker':'A','start_ms':1000,'end_ms':5000,'text':'机器原始转写'}],target_segment_ids=[segment])
            response=self.client.post(f'/api/audio/recordings/{rid}/semantic-analyses',json=body)
            self.assertEqual(response.status_code,202,response.text)
            with self.Session() as db:
                self.assertEqual(db.get(SemanticAnalysis,response.json()['analysis_id']).request_snapshot['session_id'],sid)

    def test_actual_worker_aggregates_seats_separately_and_clears_only_observed_normal_seat(self):
        from app.models import Event
        from app.supervision_models import SupervisionEventSpan
        event,_,_,sid=self.bound_event()
        with self.Session.begin() as db:db.query(Event).filter_by(event_id=event['event_id']).delete()
        config=self.client.post('/api/operators',json={'code':'seat_absence','name':'工位观察','version':'v9','input_types':['person_detected'],
            'output_event_type':'seat_absence','risk_level':'high','parameters':{'absence_seconds':60}}).json()
        self.client.post('/api/operators/'+config['operator_id']+'/enable')
        for zone in ('A1','A2'):
            bound=self.client.post('/api/operators/'+config['operator_id']+'/bindings',json={'zone_id':zone,'session_id':sid})
            self.assertEqual(bound.status_code,201,bound.text)
        sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'event-service'))
        from service import main as worker
        worker.Session=self.Session;worker.operator_cache={};worker.operator_cache_at=0
        worker.phase_scene_cache.clear();worker.SUSTAIN.clear();worker.LAST_KEYFRAME.clear();worker.refresh_operator_cache(force=True)
        bus=SimpleNamespace(xadd=AsyncMock(),xtrim=AsyncMock());at=time.time()-20
        def observe(offset,first):
            asyncio.run(worker.handle_observation(bus,{'type':'person_detected','source_id':'cam1','session_id':sid,'occurred_at':str(at+offset),
                'payload':json.dumps({'count':1,'media_run_id':'test-run','seats':[{'name':'A1','absent_sec':first},{'name':'A2','absent_sec':100+offset}]})},{}))
        observe(0,90);observe(5,95);observe(10,0);observe(15,'invalid-frame-value')
        rows=self.client.get('/api/supervision/events',params={'session_id':sid}).json()['items']
        self.assertEqual(len(rows),2,rows)
        with self.Session() as db:
            entries={e.zone_id:db.get(SupervisionEventSpan,e.event_id) for e in db.query(Event).filter_by(session_id=sid,skill_code='seat_absence')}
            self.assertEqual(entries['A1'].trigger_count,2);self.assertFalse(entries['A1'].active)
            self.assertEqual(entries['A1'].end_basis,'observed_clear')
            self.assertEqual(entries['A2'].trigger_count,3);self.assertTrue(entries['A2'].active)

    def test_persistent_episode_deduplicates_restarts_and_clear_extends_preservation(self):
        from app.models import Event
        from app.supervision_models import SupervisionEventSpan,SupervisionMediaHold
        sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'event-service'))
        from service.supervision_integration import episode_key,persist_episode,finish_observed_episodes
        from service.models import Event as WorkerEvent
        event,_,_,sid=self.bound_event();at=datetime.utcnow()-timedelta(seconds=20)
        def observe(t,subject='A1'):
            ev=WorkerEvent(event_id=str(uuid.uuid4()),session_id=sid,camera_id='cam1',event_type='seat_absence',skill_code='seat_absence',skill_version='v1',phase_id='DETAIL',zone_id=subject,risk_level='high',occurred_at=t,status='PENDING')
            ev._episode={'group':episode_key(ev,subject,'run-one'),'started_at':t-timedelta(seconds=120),'detail':{'subject':subject}}
            with self.Session.begin() as db:
                target,new=persist_episode(db,ev);return target.event_id,new
        first,new=observe(at);self.assertTrue(new)
        self.assertEqual(observe(at+timedelta(seconds=5)),(first,False))
        self.assertEqual(observe(at+timedelta(seconds=5)),(first,False))
        self.assertEqual(observe(at-timedelta(seconds=1)),(first,False))
        second,_=observe(at+timedelta(seconds=5),'A2');self.assertNotEqual(second,first)
        with self.Session.begin() as db:
            span=db.get(SupervisionEventSpan,first);self.assertEqual(span.trigger_count,2)
            self.assertEqual(span.started_at,at-timedelta(seconds=120))
            db.add(SupervisionMediaHold(event_id=first,status='READY',manifest=[]))
        clear=at+timedelta(seconds=8)
        finish_observed_episodes(self.Session,{'source_id':'camera-001','session_id':sid,'occurred_at':str(clear.replace(tzinfo=timezone.utc).timestamp())},set(),{'seat_absence'},{('seat_absence','A1')})
        with self.Session() as db:
            self.assertEqual(db.get(SupervisionEventSpan,first).ended_at,clear)
            self.assertFalse(db.get(SupervisionEventSpan,first).active)
            self.assertEqual(db.get(SupervisionMediaHold,first).status,'PENDING')
            self.assertTrue(db.get(SupervisionEventSpan,second).active)
        third,new=observe(clear+timedelta(seconds=1));self.assertTrue(new);self.assertNotEqual(third,first)

    def test_multicamera_complete_range_is_preserved_and_exports_all_verified_originals(self):
        if not shutil.which('ffmpeg'):self.skipTest('需要ffmpeg')
        from app.models import Event
        from app.supervision_models import SupervisionEventSpan
        from app.routers import media_clips,audio_recordings
        from app.core import media_preservation
        event,_,r,sid=self.bound_event();self.channel(sid)
        stamp=int(time.time())-400;start=datetime.utcfromtimestamp(stamp)
        with self.Session.begin() as db:
            db.query(Event).filter_by(event_id=event['event_id']).one().occurred_at=start
            db.add(SupervisionEventSpan(event_id=event['event_id'],started_at=start,last_seen_at=start+timedelta(seconds=80),ended_at=start+timedelta(seconds=80),active=False,trigger_count=2,end_basis='observed_clear'))
        w=self.detail(event)['context_evidence']['videos'][0]
        self.assertEqual(self.mutation(event,'video-window',source_id='camera-002',started_at=w['started_at'],ended_at=w['ended_at']).status_code,200)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'recordings';saved=Path(directory)/'preserved';fixture=Path(directory)/'ten.mp4'
            subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=black:s=160x90:r=10','-t','10','-c:v','mpeg4',str(fixture)],check=True,timeout=20)
            for camera in ('cam1','cam2'):
                (root/camera).mkdir(parents=True)
                for offset in range(-50,121,10):
                    file=root/camera/f'piece-{stamp+offset}.mp4';shutil.copyfile(fixture,file);os.utime(file,(time.time()-100,)*2)
            rid,segment=self.recording(r['room_id'],start-timedelta(seconds=1),directory)
            self.assertEqual(self.mutation(event,'audio-links',recording_id=rid).status_code,200)
            with patch.object(media_clips,'RECORD_ROOT',root),patch.object(media_preservation,'HOLD_ROOT',saved),patch.dict(os.environ,{'AUDIO_DATA_DIR':directory}):
                media_preservation.preserve_due_events(self.Session)
                self.assertEqual(self.detail(event)['evidence_status'],'READY')
                shutil.rmtree(root)
                for source in ('camera-001','camera-002'):
                    result=self.client.get(f"/api/events/{event['event_id']}/recordings",params={'source_id':source})
                    self.assertEqual(result.status_code,200,result.text);self.assertEqual(result.json()['total'],18)
                package=self.client.get(f"/api/events/{event['event_id']}/evidence-package")
                self.assertEqual(package.status_code,200,package.text[:100])
                with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
                    checks=json.loads(archive.read('integrity.json'))['items']
                    self.assertEqual(sum(i['status']=='verified' and i['file'].endswith('.mp4') for i in checks),36)
                    self.assertEqual(archive.read('audio/original.wav'),(Path(directory)/(rid+'.wav')).read_bytes())


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(WorkspaceTest(name) for name in WorkspaceTest.__dict__ if name.startswith('test_'))


if __name__=='__main__':unittest.main()
