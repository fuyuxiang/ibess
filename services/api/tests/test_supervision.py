"""真实 PostgreSQL + 鉴权 API 的监管闭环回归，在随机 schema 中执行。"""
import io
import json
import tempfile
import unittest
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import test_poc_workflow as workflow_test


class SupervisionTest(workflow_test.POCWorkflowTest):
    def detail(self, event):
        response = self.client.get('/api/events/' + event['event_id'])
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def operation(self, event, action, **values):
        detail = self.detail(event)
        return self.client.post('/api/supervision/events/' + event['event_id'] + '/actions', json={
            'action':action, 'note':'已核查原始片段及业务许可', 'expected_status':detail['workflow_status'],
            'expected_version':detail['workflow_version'], 'request_id':str(uuid.uuid4()), **values})

    def successful(self, event, action, **values):
        response = self.operation(event, action, **values)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def business(self):
        p=self.client.post('/api/registry/projects',json={'code':uuid.uuid4().hex,'name':'回归项目','owner':'测试单位','status':'running'}).json()
        r=self.client.post('/api/registry/rooms',json={'project_id':p['project_id'],'code':'01','name':'真实绑定评标室','location':'测试区','mode':'centralized','enabled':True}).json()
        return p,r

    def bound_event(self, source='camera-001'):
        from app.models import Event, EvalSession
        from app.phase_models import PhaseBinding
        p,r=self.business()
        sid=str(uuid.uuid4())
        with self.Session.begin() as db:
            db.add(EvalSession(session_id=sid,name='隔离场次',status='running',current_phase='DETAIL'));db.flush()
            db.add(PhaseBinding(session_id=sid,project_id=p['project_id'],room_id=r['room_id'],source_id='camera-001'))
            event=Event(event_id=str(uuid.uuid4()),session_id=sid,camera_id=source,event_type='phone_call',skill_code='phone_call',risk_level='high',reason='回归实际视频线索',status='PENDING',occurred_at=datetime.utcnow(),phase_id='DETAIL')
            db.add(event)
            eid=event.event_id
        return {'event_id':eid},p,r,sid

    def test_full_confirm_assign_feedback_return_review_archive(self):
        event=self.create()
        self.successful(event,'confirm')
        self.assertEqual(self.operation(event,'archive').status_code,409)
        due=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()
        self.successful(event,'assign',assignee='现场值守岗',due_at=due)
        self.successful(event,'feedback',note='已现场提醒并复查，异常行为停止')
        self.successful(event,'return',note='请补充现场核查时段')
        self.successful(event,'feedback',note='补充核查完成，录像片段已复核')
        self.successful(event,'archive',note='已复核处理结果与关联证据')
        detail=self.detail(event)
        self.assertEqual((detail['workflow_status'],detail['workflow_version']),('ARCHIVED',6))
        self.assertEqual(detail['assignee'],'现场值守岗')
        self.assertTrue(detail['archived_at'].endswith('Z'))
        self.assertEqual(detail['status'],'CLOSED')
        self.assertEqual([d['action'] for d in detail['decisions']],['confirm','assign','feedback','return','feedback','archive'])
        self.assertTrue(all(d['reviewer_id']=='test_admin' for d in detail['decisions']))
        self.assertEqual(self.operation(event,'confirm').status_code,409)

    def test_insufficient_stays_in_confirmation_queue_and_dismissal_requires_basis(self):
        event=self.create()
        self.successful(event,'supplement',note='请核对授权离席许可')
        self.assertEqual(self.detail(event)['workflow_status'],'SUPPLEMENT')
        self.assertEqual(self.operation(event,'dismiss').status_code,422)
        self.successful(event,'dismiss',resolution='authorized_exception',note='已核实为授权休息时段')
        detail=self.detail(event)
        self.assertEqual((detail['workflow_status'],detail['resolution']),('ARCHIVED','authorized_exception'))

    def test_idempotent_retry_and_conflicting_request_or_version(self):
        event=self.create()
        key=str(uuid.uuid4())
        first=self.successful(event,'confirm',request_id=key)
        replay=self.operation(event,'confirm',expected_status='PENDING',expected_version=0,request_id=key)
        self.assertEqual(replay.status_code,200,replay.text)
        self.assertTrue(replay.json()['duplicate'])
        self.assertEqual(replay.json()['workflow_version'],first['workflow_version'])
        self.assertEqual(self.operation(event,'confirm',request_id=key,note='不同载荷').status_code,409)
        self.assertEqual(self.operation(event,'assign',expected_version=0,assignee='值守岗').status_code,409)
        self.assertEqual(len(self.detail(event)['decisions']),1)
        old=self.client.post(f"/api/events/{event['event_id']}/decisions",json={'action':'close','note':'绕过复核'})
        self.assertEqual(old.status_code,409)

    def test_validation_actor_and_due_date(self):
        event=self.create()
        self.assertEqual(self.operation(event,'confirm',note='   ').status_code,422)
        self.assertEqual(self.operation(event,'confirm',reviewer_id='spoofed').status_code,422)
        self.successful(event,'confirm')
        self.assertEqual(self.operation(event,'assign',assignee=' ').status_code,422)
        self.assertEqual(self.operation(event,'assign',assignee='值守岗',due_at='2020-01-01T00:00:00Z').status_code,422)
        self.assertEqual(self.operation(event,'assign',assignee='值守岗',due_at='2028-01-01T00:00:00').status_code,422)

    def test_query_separates_technical_and_behavior_counts_before_pagination(self):
        a,b=self.create(),self.create('seat_absence')
        self.create('video_offline')
        self.successful(a,'confirm')
        result=self.client.get('/api/supervision/events',params={'page_size':1}).json()
        self.assertEqual(result['total'],2,result)
        self.assertEqual(result['summary'],{'confirm':1,'process':1,'archive':0})
        self.assertEqual(result['items'][0]['event_id'],b['event_id'])
        faults=self.client.get('/api/supervision/events',params={'category':'technical'}).json()
        self.assertEqual(faults['total'],1)
        self.assertEqual(faults['items'][0]['category'],'technical')
        for params in ({'category':'bogus'},{'workflow_status':'bogus'},{'date_from':'2026-11-06','date_to':'2026-11-05'}):
            self.assertEqual(self.client.get('/api/supervision/events',params=params).status_code,422)

    def test_actual_events_are_scoped_by_session_and_matching_camera(self):
        event,p,r,sid=self.bound_event(source='cam1')
        detail=self.detail(event)
        self.assertEqual((detail['project_id'],detail['room_id'],detail['room']),(p['project_id'],r['room_id'],r['name']))
        for params in ({'project_id':p['project_id']},{'room_id':r['room_id']},{'session_id':sid},{'keyword':p['name']}):
            response=self.client.get('/api/supervision/events',params=params)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['total'],1,response.text)
        from app.models import Event
        with self.Session.begin() as db:
            db.query(Event).filter(Event.event_id==event['event_id']).one().camera_id='camera-002'
        self.assertIsNone(self.detail(event)['project_id'])
        self.assertEqual(self.client.get('/api/supervision/events',params={'room_id':r['room_id']}).json()['total'],0)
        sources=self.client.get('/api/supervision/sources').json()['items']
        self.assertEqual(sources[0]['hls_url'],'/hls/cam1/index.m3u8')

    def test_closed_legacy_event_is_not_silently_archived(self):
        event=self.create()
        self.assertEqual(self.decide(event,'close').status_code,200)
        self.assertEqual(self.detail(event)['workflow_status'],'LEGACY_CLOSED')
        self.assertEqual(self.client.get('/api/supervision/events',params={'queue':'archive'}).json()['total'],0)
        self.successful(event,'dismiss',resolution='normal',note='核查历史片段，确认为正常讨论')

    def test_export_has_workflow_and_honest_missing_media_manifest(self):
        event=self.create()
        self.successful(event,'dismiss',resolution='false_positive')
        response=self.client.get(f"/api/events/{event['event_id']}/evidence-package")
        self.assertEqual(response.status_code,200,response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            detail=json.loads(archive.read('event.json'))
            self.assertEqual(detail['workflow_status'],'ARCHIVED')
            self.assertEqual(detail['resolution'],'false_positive')
            self.assertTrue(any(a['action']=='evidence_export' for a in detail['audit']))
            self.assertIn('integrity.json',archive.namelist())
            import hashlib
            for line in archive.read('manifest.sha256').decode().splitlines():
                digest,name=line.split('  ',1)
                self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(),digest)

    def test_event_recordings_never_fall_back_to_unrelated_time(self):
        from app.routers import media_clips
        event,_,_,_=self.bound_event()
        from app.models import Event
        stamp=int(datetime.now(timezone.utc).timestamp())
        with self.Session.begin() as db:
            db.query(Event).filter(Event.event_id==event['event_id']).one().occurred_at=datetime.utcfromtimestamp(stamp)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'cam1').mkdir()
            (root/'cam1'/f'piece-{stamp-5000}.mp4').write_bytes(b'test')
            with patch.object(media_clips,'RECORD_ROOT',root),patch.object(media_clips,'_duration',return_value=(10,False)):
                response=self.client.get(f"/api/events/{event['event_id']}/recordings")
                self.assertEqual(response.status_code,404,response.text)
                # Begins before the ±45s window, but actually overlaps it.
                (root/'cam1'/f'piece-{stamp-50}.mp4').write_bytes(b'test')
                (root/'cam1'/f'piece-{stamp-3}.mp4').write_bytes(b'test')
                response=self.client.get(f"/api/events/{event['event_id']}/recordings")
                self.assertEqual(response.status_code,200,response.text)
                result=response.json()
                self.assertEqual(result['total'],2)
                self.assertTrue(result['event_covered'])
                self.assertEqual(result['items'][1]['seek_seconds'],3)

    def test_completed_semantic_results_intake_once_with_original_segments(self):
        from app.audio_models import AudioRecording, AudioSegment
        from app.semantic_models import SemanticAnalysis
        from app.core.supervision import sync_semantic_events
        from app.supervision_models import SemanticEventLink
        now=datetime.now(timezone.utc)
        rid,aid=str(uuid.uuid4()),str(uuid.uuid4())
        with self.Session.begin() as db:
            db.add(AudioRecording(recording_id=rid,title='测试原音',source_id='audio-01',source_type='upload',status='ready',duration_ms=10000,created_at=now,sha256='0'*64,original_path='test.wav'));db.flush()
            db.add(AudioSegment(segment_id=str(uuid.uuid4()),recording_id=rid,ordinal=0,speaker='A',start_ms=0,end_ms=5000,text='仅为回归构造的语句'))
            db.add(SemanticAnalysis(analysis_id=aid,recording_id=rid,task_key=uuid.uuid4().hex,scene_id='S06',window_start_ms=0,window_end_ms=5000,status='ready',request_snapshot={'source_id':'audio-01','model_name':'test','session_id':'unbound'},result={'conclusion':'insufficient','reason':'缺少许可事实','evidence':[],'missing_facts':['clarification_permission']},created_at=now,updated_at=now,completed_at=now))
        with self.Session.begin() as db:
            self.assertEqual(len(sync_semantic_events(db)),1)
        with self.Session.begin() as db:
            self.assertEqual(len(sync_semantic_events(db)),0)
            link=db.get(SemanticEventLink,aid)
            eid=link.event_id
        detail=self.detail({'event_id':eid})
        self.assertEqual((detail['source_type'],detail['workflow_status']),('audio','SUPPLEMENT'))
        self.assertEqual(detail['semantic']['recording_id'],rid)
        self.assertEqual(len(detail['semantic']['segments']),1)
        self.assertIsNone(detail['project_id'])

    def test_supervision_requires_authenticated_session_and_csrf(self):
        event=self.create()
        from fastapi.testclient import TestClient
        with TestClient(self.app) as client:
            self.assertEqual(client.get('/api/supervision/events').status_code,401)
            self.assertEqual(client.post(f"/api/supervision/events/{event['event_id']}/actions",json={}).status_code,401)
        old=self.client.headers.pop('X-CSRF-Token')
        try:
            self.assertEqual(self.operation(event,'confirm').status_code,403)
        finally:
            self.client.headers['X-CSRF-Token']=old

    def test_concurrent_confirmation_has_one_winner_and_one_audit(self):
        from concurrent.futures import ThreadPoolExecutor
        event=self.create()
        from fastapi.testclient import TestClient
        def submit(_):
            with TestClient(self.app,headers=dict(self.client.headers),cookies=dict(self.client.cookies)) as client:
                return client.post(f"/api/supervision/events/{event['event_id']}/actions",json={
                    'action':'confirm','note':'并发核查','expected_status':'PENDING','expected_version':0,'request_id':str(uuid.uuid4())}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(submit,range(2)))
        self.assertEqual(sorted(result),[200,409])
        detail=self.detail(event)
        self.assertEqual(detail['workflow_version'],1)
        self.assertEqual(len(detail['decisions']),1)

    def test_rule_changes_have_audit_and_reject_stale_configuration(self):
        response=self.client.post('/api/operators',json={'code':'crowd_gathering','name':'回归规则','version':'v1','output_event_type':'crowd_gathering','parameters':{'count_threshold':3}})
        self.assertEqual(response.status_code,201,response.text)
        operator=response.json()
        path='/api/operators/'+operator['operator_id']
        changed=self.client.patch(path,json={'name':'调整后的规则','parameters':{'count_threshold':5},'expected_updated_at':operator['updated_at']})
        self.assertEqual(changed.status_code,200,changed.text)
        stale=self.client.patch(path,json={'name':'过期写入','expected_updated_at':operator['updated_at']})
        self.assertEqual(stale.status_code,409,stale.text)
        enabled=self.client.post(path+'/enable',params={'expected_updated_at':changed.json()['updated_at']})
        self.assertEqual(enabled.status_code,200,enabled.text)
        audit=self.client.get(path+'/audit').json()
        self.assertEqual([a['action'] for a in audit],['enable','configure'])
        self.assertTrue(all(a['actor']=='test_admin' for a in audit))
        self.assertEqual(audit[1]['before']['parameters'],{'count_threshold':3})
        self.assertEqual(audit[1]['after']['parameters'],{'count_threshold':5})

    def test_stage_summary_does_not_mix_other_sessions(self):
        event,p,r,sid=self.bound_event()
        from app.models import EvalSession, Event
        other=str(uuid.uuid4())
        with self.Session.begin() as db:
            db.add(EvalSession(session_id=other,name='其他场次',status='running',current_phase='DETAIL'))
            db.add(Event(event_id=str(uuid.uuid4()),session_id=other,camera_id='camera-002',event_type='phone_call',phase_id='DETAIL',risk_level='high',status='PENDING',occurred_at=datetime.utcnow()))
        response=self.client.get('/api/stages/summary',params={'session_id':sid})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['total']['events'],1,response.text)
        listed=self.client.get('/api/events',params={'session_id':sid})
        self.assertEqual(listed.status_code,200,listed.text)
        self.assertEqual([e['event_id'] for e in listed.json()],[event['event_id']])

    def test_s10_lowercase_rule_is_classified_as_monitoring_fault(self):
        event,_,_,_=self.bound_event()
        from app.models import Event
        with self.Session.begin() as db:
            row=db.query(Event).filter(Event.event_id==event['event_id']).one()
            row.event_type='s10-witness-gap';row.skill_code='s10-witness-gap'
        self.assertEqual(self.detail(event)['category'],'technical')
        self.assertEqual(self.client.get('/api/supervision/events').json()['total'],0)
        self.assertEqual(self.client.get('/api/supervision/events',params={'category':'technical'}).json()['total'],1)

    def test_private_media_uses_same_origin_url_and_supports_video_ranges(self):
        from types import SimpleNamespace
        from app.models import Evidence
        from app.routers import evidence as router
        event=self.create()
        with self.Session.begin() as db:
            row=Evidence(event_id=event['event_id'],evidence_type='video',object_key='private/test.mp4')
            db.add(row);db.flush();eid=row.id
        class Body(io.BytesIO):
            def release_conn(self):pass
        class Storage:
            def stat_object(self,*args):return SimpleNamespace(size=10)
            def get_object(self,*args,offset=0,length=10):return Body(b'0123456789'[offset:offset+length])
        url=f'/api/evidence/objects/{eid}/content'
        self.assertEqual(self.detail(event)['evidences'][0]['url'],url)
        with patch.object(router,'_minio_client',return_value=(Storage(),'test')):
            full=self.client.get(url)
            self.assertEqual((full.status_code,full.content),(200,b'0123456789'))
            part=self.client.get(url,headers={'Range':'bytes=2-5'})
            self.assertEqual((part.status_code,part.content),(206,b'2345'))
            self.assertEqual(part.headers['content-range'],'bytes 2-5/10')
            suffix=self.client.get(url,headers={'Range':'bytes=-3'})
            self.assertEqual(suffix.content,b'789')
            self.assertEqual(self.client.get(url,headers={'Range':'bytes=20-30'}).status_code,416)
            self.assertEqual(self.client.get(url,headers={'Range':'bytes=0-1,3-4'}).status_code,416)

    def test_ffprobe_measures_the_actual_recording_instead_of_assuming_ten_seconds(self):
        import shutil,subprocess
        from app.routers.media_clips import _duration
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            self.skipTest('真实时长验证需要ffmpeg/ffprobe，API镜像已包含')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'actual.mp4'
            subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=black:s=160x90:r=10','-t','1.5','-c:v','mpeg4',str(path)],check=True,timeout=20)
            stat=path.stat()
            duration,estimated=_duration(str(path),stat.st_size,stat.st_mtime)
            self.assertFalse(estimated)
            self.assertAlmostEqual(duration,1.5,delta=0.15)

    def test_raw_person_detection_is_not_a_violation_task(self):
        event,_,_,_=self.bound_event()
        from app.models import Event
        with self.Session.begin() as db:
            row=db.query(Event).filter(Event.event_id==event['event_id']).one()
            row.risk_level='info';row.event_type='person_detected'
        self.assertEqual(self.detail(event)['category'],'observation')
        self.assertEqual(self.client.get('/api/supervision/events',params={'category':'all'}).json()['total'],0)
        self.assertEqual(self.operation(event,'confirm').status_code,422)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(SupervisionTest(name) for name in SupervisionTest.__dict__ if name.startswith('test_'))


if __name__=='__main__':
    unittest.main()
