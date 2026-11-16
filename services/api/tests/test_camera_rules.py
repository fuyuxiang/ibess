"""Camera facts → configured operator → PostgreSQL → API alert → real MP4 playback.

Uses an isolated schema and synthetic camera facts, never real people or production media.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import test_poc_workflow as workflow


class CameraRulesIntegrationTest(unittest.TestCase):
    setUpClass = classmethod(workflow.POCWorkflowTest.setUpClass.__func__)
    tearDownClass = classmethod(workflow.POCWorkflowTest.tearDownClass.__func__)
    setUp = workflow.POCWorkflowTest.setUp
    tearDown = workflow.POCWorkflowTest.tearDown

    def test_all_six_rules_create_alerts_and_replay_preserved_capture_window(self):
        from app.models import EvalSession, Event, Evidence
        from app.phase_models import PhaseBinding, PhaseSnapshot
        from app.core.rule_catalog import ensure_camera_rules
        from app.core.supervision_evidence import event_range
        from app.core import media_preservation
        from app.routers import media_clips
        project = self.client.post('/api/registry/projects',json={'code':uuid4().hex,'name':'摄像头规则回归','owner':'测试','status':'running'}).json()
        room = self.client.post('/api/registry/rooms',json={'project_id':project['project_id'],'code':'01','name':'测试评标室','location':'测试','mode':'centralized','enabled':True}).json()
        sid, snapshot = str(uuid4()), str(uuid4())
        stamp = int(time.time())-180
        with self.Session.begin() as db:
            db.add(EvalSession(session_id=sid,name='识别回归',status='running',current_phase='DETAIL',start_at=datetime.utcfromtimestamp(stamp-30)))
            db.flush()
            db.add(PhaseBinding(session_id=sid,project_id=project['project_id'],room_id=room['room_id'],source_id='camera-001',created_at=datetime.fromtimestamp(stamp-30,timezone.utc)))
            db.add(PhaseSnapshot(stage_snapshot_id=snapshot,session_id=sid,project_id=project['project_id'],room_id=room['room_id'],source_id='camera-001',
                phase_id='DETAIL',phase_version=1,effective_at=datetime.fromtimestamp(stamp-30,timezone.utc),
                enabled_scenes=['S03','S04','S05','S08'],always_on_scenes=['S10'],assets=[],
                rule_versions={f'S{i:02}':'v2' for i in range(1,11)},exceptions=[],sync_status='applied',actor_id='test',reason='测试'))
        ensure_camera_rules(self.engine)
        services=Path(__file__).resolve().parents[2]
        sys.path.insert(0,str(services/'event-service'))
        sys.path.insert(0,str(services/'vision-worker'))
        from service import main as processor
        from service.core_behaviors import engine as rules
        from worker.core_behaviors import BehaviorObserver, validate_review
        from worker.a_context import DbContextSource
        channel=self.client.post(f'/api/supervision/sessions/{sid}/channels',json={'source_id':'camera-002','name':'入口辅助机位','kind':'video'})
        self.assertEqual(channel.status_code,201,channel.text)
        path=f'/api/sessions/{sid}/sources/camera-002/zones'
        auxiliary=self.client.put(path,json={'config_version':0,'zones':[],
            'entry_lines':[{'line_id':'entry','name':'入口','start':[.5,0],'end':[.5,1],'direction':'both'}]})
        self.assertEqual(auxiliary.status_code,200,auxiliary.text)
        source=DbContextSource(self.engine.url.render_as_string(hide_password=False))
        loaded=source.load('cam2')
        self.assertEqual(loaded.scope['session_id'],sid)
        self.assertEqual(loaded.zone_config['entry_lines'][0]['line_id'],'entry')
        source._engine.dispose()
        from app.models import Operator
        with self.Session.begin() as db:
            for code in ('frequent_absence','joint_absence','workstation_visit'):
                op=db.query(Operator).filter_by(code=code).one()
                op.parameters = {'count_threshold':2} if code=='frequent_absence' else {'absence_seconds':1} if code=='joint_absence' else {'dwell_seconds':1}
        redis=AsyncMock()
        observer=BehaviorObserver()
        zones=[{'kind':'seat','seat_id':'a','zone_id':'a','polygon':[[0,0],[.4,0],[.4,1],[0,1]]},
               {'kind':'seat','seat_id':'b','zone_id':'b','polygon':[[.6,0],[1,0],[1,1],[.6,1]]}]
        tracks=[{'track_id':'t1','bbox':[.1,.1,.3,.8]}, {'track_id':'t2','bbox':[.7,.1,.9,.8]}]
        def send(offset, **body):
            payload={'bound':True,'quality':'clear','phase_id':'DETAIL','phase_version':1,'media_run_id':'integration-run',
                     'seats':[],'visits':[],'entries':[],'facts':[],**body}
            asyncio.run(processor.handle_observation(redis,{'type':'behavior_detected','source_id':'camera-001','session_id':sid,
                'occurred_at':str(stamp+offset),'payload':json.dumps(payload)},{}))
        with patch.object(processor,'Session',self.Session):
            rules.states.clear();rules.generations.clear()
            processor.refresh_operator_cache(force=True)
            for offset in (0,1,2,3):
                seats,visits,_=observer.observe(stamp+offset,tracks,zones,[],('s','r'))
                send(offset,seats=seats,visits=visits)
            for offset, occupied in [(4,False),(5,False),(6,True),(7,True),(8,False),(9,False),(10,False)]:
                send(offset,seats=[{'seat_id':s,'occupied':occupied} for s in ('a','b')])
            visit={'track_id':'t1','home_seat_id':'a','seat_id':'b','zone_id':'b'}
            send(11,visits=[visit]);send(12,visits=[visit])
            samples=[{'at':stamp+i,'tracks':tracks} for i in (10,11,14)]
            facts=validate_review(json.dumps({'facts':[{'kind':kind,'track_ids':['t1','t2'],'visibility':'clear',
                'evidence_frames':[0,2],'object_visible':True,'confidence':.95} for kind in ('whispering','document_transfer')]}),samples)
            send(14,review_only=True,facts=facts,frame_key='test/original-14.jpg')
            send(15,identity_only=True,entries=[{'track_id':'outside','status':'matched','person_id':str(uuid4()),'authorized':False,'authorization_snapshot_id':'fixture'}],frame_key='test/entry-15.jpg')
        with self.Session() as db:
            events=db.query(Event).filter(Event.session_id==sid).all()
            self.assertEqual({e.event_type for e in events},{'whispering','document_transfer','workstation_visit','frequent_absence','joint_absence','unauthorized_entry'})
            whisper=next(e for e in events if e.event_type=='whispering')
            eid=whisper.event_id
            start,end=event_range(db,whisper)
            self.assertEqual(start,datetime.utcfromtimestamp(stamp+10))
            self.assertEqual(end,datetime.utcfromtimestamp(stamp+14))
            evidence=db.query(Evidence).filter_by(event_id=eid).one()
            self.assertEqual(evidence.object_key,'test/original-14.jpg')
            self.assertEqual(evidence.captured_at,datetime.utcfromtimestamp(stamp+14))
        self.assertGreaterEqual(redis.xadd.await_count,6)
        alert=self.client.get('/api/events/'+eid)
        self.assertEqual(alert.status_code,200,alert.text)
        self.assertEqual(alert.json()['event_type'],'whispering')
        # Real encoded media verifies authenticated HTTP byte-range playback, even after source rotation.
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'recordings';(root/'cam1').mkdir(parents=True)
            fixture=Path(directory)/'ten.mp4'
            subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=black:s=160x90:r=10','-t','10','-c:v','mpeg4',str(fixture)],check=True,timeout=20)
            for offset in range(-40,71,10):
                target=root/'cam1'/f'piece-{stamp+offset}.mp4';shutil.copyfile(fixture,target)
                os.utime(target,(time.time()-100,time.time()-100))
            with patch.object(media_clips,'RECORD_ROOT',root),patch.object(media_preservation,'HOLD_ROOT',Path(directory)/'held'):
                media_preservation.preserve_due_events(self.Session)
                detail=self.client.get('/api/events/'+eid).json()
                self.assertEqual(detail['evidence_status'],'READY')
                shutil.rmtree(root)
                recordings=self.client.get(f'/api/events/{eid}/recordings').json()
                self.assertTrue(all(x['preserved'] for x in recordings['items']))
                playback=self.client.get(recordings['items'][0]['playback_url'],headers={'Range':'bytes=0-15'})
                self.assertEqual(playback.status_code,206)
                self.assertIn(b'ftyp',playback.content)

    def test_registration_is_idempotent_and_preserves_user_switches(self):
        from app.core.rule_catalog import ensure_camera_rules, CORE_RULES
        from app.models import Operator
        ensure_camera_rules(self.engine)
        with self.Session.begin() as db:
            item=db.query(Operator).filter_by(code='whispering').one();item.enabled=False;item.parameters={'duration_seconds':8}
        ensure_camera_rules(self.engine)
        rows=self.client.get('/api/operators').json()
        self.assertEqual(len([x for x in rows if x['code'] in CORE_RULES]),6)
        item=next(x for x in rows if x['code']=='whispering')
        self.assertFalse(item['enabled']);self.assertEqual(item['parameters'],{'duration_seconds':8})
        self.assertEqual(item['recognition_sources'],[])

    def test_real_entrance_face_matches_local_gallery_and_checks_room_authorization(self):
        from app.business_models import BusinessPerson, BusinessProject, BusinessRoom
        from app.business_authz_models import BusinessAuthorizationSnapshot, BusinessAuthorizationEntry
        from app.face_models import FaceProfile
        from app.core.face_match import FaceEngine
        import cv2
        services=Path(__file__).resolve().parents[2]
        models=services.parent/'models/face'
        if not (models/'face_detection_yunet_2023mar.onnx').exists():
            self.skipTest('先运行 scripts/fetch_face_models.py 下载固定版本模型')
        sys.path.insert(0,str(services/'vision-worker'))
        from worker.entrance_identity import EntranceIdentity
        image=services/'api/tests/face_fixtures/grace_hopper.jpg'
        face=FaceEngine(models)
        sample=face.extract(image.read_bytes())
        person,project,room,snapshot=[str(uuid4()) for _ in range(4)]
        captured=time.time()
        with self.Session.begin() as db:
            db.add(BusinessPerson(person_id=person,code=uuid4().hex,name='公开来源测试人物',unit='公开测试资料',role='expert',enabled=True))
            db.add(BusinessProject(project_id=project,code=uuid4().hex,name='入口模型测试',owner='测试',status='running'))
            db.flush()
            db.add(BusinessRoom(room_id=room,project_id=project,code='01',name='测试室',location='测试',enabled=True,mode='centralized'))
            db.add(FaceProfile(person_id=person,version=str(uuid4()),model=face.model,registered_at=datetime.now(timezone.utc),embedding=sample.embedding.tolist(),photo=sample.photo))
            db.flush()
            db.add(BusinessAuthorizationSnapshot(snapshot_id=snapshot,project_id=project,room_id=room,information_visibility='public',status='active',created_by='test',created_at=datetime.fromtimestamp(captured-10,timezone.utc)))
        identity=EntranceIdentity(str(self.engine.url.render_as_string(hide_password=False)),str(models))
        scope={'session_id':'test','room_id':room,'project_id':project,'stage_snapshot_id':'snapshot','occurred_at':captured}
        frame=cv2.imread(str(image));track={'track_id':'entrance-track','bbox':[0,0,1,1]}
        identity.check(frame,[track],scope)
        deadline=time.monotonic()+10
        results=[]
        while not results and time.monotonic()<deadline:
            time.sleep(.05);results=identity.check(frame,[],scope)
        self.assertEqual(results[0]['status'],'matched',identity.status)
        self.assertEqual(results[0]['person_id'],person)
        self.assertFalse(results[0]['authorized'])
        with self.Session.begin() as db:
            db.add(BusinessAuthorizationEntry(entry_id=str(uuid4()),snapshot_id=snapshot,person_id=person,role='expert',identity_verified=True))
        identity.check(frame,[track],scope)
        deadline=time.monotonic()+10;results=[]
        while not results and time.monotonic()<deadline:
            time.sleep(.05);results=identity.check(frame,[],scope)
        self.assertTrue(results[0]['authorized'])
        identity.engine.dispose()
