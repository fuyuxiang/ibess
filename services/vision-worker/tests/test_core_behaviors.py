import json
import unittest
import numpy as np
from worker.core_behaviors import BehaviorObserver, validate_review
from worker.entrance_identity import select_identity, MODEL_VERSION


class CameraFactsTest(unittest.TestCase):
    def test_minimax_reasoning_budget_does_not_consume_the_json_answer(self):
        import io
        from worker.visual_client import OpenAICompatibleVisionClient, VisualModelConfig
        calls=[]
        def opener(request, timeout):
            calls.append(json.loads(request.data))
            return io.BytesIO(json.dumps({'choices':[{'message':{'content':'{"facts":[]}'}}]}).encode())
        client=OpenAICompatibleVisionClient(VisualModelConfig('https://example.invalid/v1','MiniMax-M3.1-Flash','test-only'),opener=opener)
        self.assertEqual(client.complete([{'role':'user','content':'test'}]),'{"facts":[]}')
        self.assertEqual(calls[0]['reasoning_effort'],'low')
        self.assertGreaterEqual(calls[0]['max_completion_tokens'],4096)
        self.assertNotIn('max_tokens',calls[0])
        other=OpenAICompatibleVisionClient(VisualModelConfig('https://example.invalid/v1','other-model','test-only'),opener=opener)
        other.complete([{'role':'user','content':'test'}])
        self.assertEqual(calls[-1]['max_tokens'],400)
        self.assertNotIn('reasoning_effort',calls[-1])

    def test_slow_inference_reads_latest_frame_and_preserves_capture_time(self):
        import threading
        import time
        from worker.frame_capture import LatestFrameReader
        produced, finish = threading.Event(), threading.Event()
        class Camera:
            index=0
            def isOpened(self):return True
            def release(self):pass
            def read(self):
                self.index+=1
                if self.index>3:
                    produced.set();finish.wait(1)
                    return False,None
                return True,np.full((8,8,3),self.index,dtype=np.uint8)
        start=time.time()
        reader=LatestFrameReader('fake',opener=lambda _:Camera(),wait_seconds=.02)
        try:
            self.assertTrue(produced.wait(1))
            ok,frame=reader.read()
            self.assertTrue(ok);self.assertEqual(frame[0,0,0],3)
            self.assertGreaterEqual(reader.last_captured_at,start)
            self.assertFalse(reader.read()[0])
        finally:
            reader.release();finish.set()

    def test_visits_are_tracked_from_a_stable_home_seat(self):
        obs = BehaviorObserver()
        zones = [{'kind':'seat','seat_id':'a','zone_id':'za','polygon':[[0,0],[.4,0],[.4,1],[0,1]]},
                 {'kind':'seat','seat_id':'b','zone_id':'zb','polygon':[[.6,0],[1,0],[1,1],[.6,1]]}]
        track = {'track_id':'t1','bbox':[.1,.1,.3,.8]}
        for at in (0,1,2):
            obs.observe(at,[track],zones,[],('session','run','DETAIL'))
        _, visits, _ = obs.observe(3,[{**track,'bbox':[.7,.1,.9,.8]}],zones,[],('session','run','DETAIL'))
        self.assertEqual(visits[0]['home_seat_id'],'a')
        self.assertEqual(visits[0]['seat_id'],'b')
        _, visits, _ = obs.observe(4,[{**track,'bbox':[.7,.1,.9,.8]}],zones,[],('session','run','CLARIFY'))
        self.assertFalse(visits)

    def test_entrance_requires_real_crossing_not_person_present(self):
        obs=BehaviorObserver()
        lines=[{'line_id':'door','start':[.5,0],'end':[.5,1],'direction':'in'}]
        self.assertFalse(obs.observe(1,[{'track_id':'t','bbox':[.6,.1,.8,.5]}],[],lines,('s','r'))[2])
        crossing=obs.observe(2,[{'track_id':'t','bbox':[.2,.1,.4,.5]}],[],lines,('s','r'))[2]
        self.assertEqual(crossing[0]['track_id'],'t')

    def test_model_cannot_invent_participants_evidence_or_identity(self):
        samples=[{'at':at,'tracks':[{'track_id':'a'},{'track_id':'b'}]} for at in (1,2,4)]
        fact={'kind':'whispering','track_ids':['a','b'],'visibility':'clear','evidence_frames':[0,2],'object_visible':False,'confidence':.9}
        result=validate_review(json.dumps({'facts':[fact]}),samples)
        self.assertEqual(result[0]['duration_seconds'],3)
        for bad in ({**fact,'track_ids':['a','fake']},{**fact,'evidence_frames':[0,3]}, {**fact,'person_id':'invented'}):
            with self.assertRaises(ValueError):validate_review(json.dumps({'facts':[bad]}),samples)
        self.assertFalse(validate_review(json.dumps({'facts':[{**fact,'visibility':'unclear'}]}),samples))
        self.assertFalse(validate_review(json.dumps({'facts':[{**fact,'kind':'document_transfer'}]}),samples))

    def test_identity_requires_quality_match_roster_and_ambiguity_margin(self):
        probe=np.zeros(128,dtype=np.float32);probe[0]=1
        profile={'person_id':'p','model':MODEL_VERSION,'embedding':probe.tolist()}
        self.assertEqual(select_identity(probe,[profile],{'p'},'snapshot')['authorized'],True)
        self.assertEqual(select_identity(probe,[profile],set(),'snapshot')['authorized'],False)
        self.assertEqual(select_identity(probe,[profile],set(),None)['status'],'unverified')
        self.assertEqual(select_identity(probe,[profile,{**profile,'person_id':'other'}],set(),'snapshot')['status'],'unverified')

    def test_person_permission_is_not_applied_without_verified_identity(self):
        from worker.core_behaviors import authorized
        auth=[{'kind':'contact_allowed','scope':{'person_id':'person1'}}]
        self.assertFalse(authorized(auth,('contact_allowed',),'room',{None}))
        auth=[{'kind':'contact_allowed','scope':{'seat_id':'seat1'}}]
        self.assertTrue(authorized(auth,('contact_allowed',),'room',{'seat1'}))
        self.assertFalse(authorized(auth,('leave_approved',),'room',{'seat1'}))

    def test_camera_pipeline_preserves_capture_time_for_delayed_review(self):
        import time
        from worker.core_behaviors import CorePipeline
        from worker.a_context import RuleContext
        from types import SimpleNamespace
        now=time.time()
        rc=RuleContext(snapshot={'project_id':'p','room_id':'r','session_id':'s','stage_snapshot_id':'snap',
                    'phase_id':'DETAIL','phase_version':1},loaded_at=now,identity_verification_enabled=False)
        pipeline=CorePipeline('cam1',SimpleNamespace(get=lambda:rc))
        pipeline.review.outbox.put((now-5, {**rc.scope,'generation':('s','run','DETAIL',1,None,False),
            'base':{'bound':True,'quality':'clear','phase_id':'DETAIL','media_run_id':'run'}},
            [{'kind':'whispering'}], 'original.jpg', [{'at':now-5,'jpeg':b'capture'}]))
        updates=pipeline.on_frame(np.zeros((80,80,3),dtype=np.uint8),[],'run',now)
        self.assertEqual(updates[0]['occurred_at'],now-5)
        self.assertEqual(updates[0]['payload']['frame_key'],'original.jpg')
        self.assertEqual(updates[0]['capture_jpeg'],b'capture')

    def test_optional_identity_does_not_run_face_matching_or_stop_spatial_observations(self):
        import time
        from unittest.mock import Mock
        from types import SimpleNamespace
        from worker.core_behaviors import CorePipeline
        from worker.a_context import RuleContext
        now = time.time()
        rc = RuleContext(snapshot={'project_id':'p','room_id':'r','session_id':'s',
            'stage_snapshot_id':'snap','phase_id':'DETAIL','phase_version':1},
            zone_config={'zones':[{'kind':'seat','seat_id':'seat-1','zone_id':'z1',
                'polygon':[[0,0],[1,0],[1,1],[0,1]]}]},
            loaded_at=now, identity_verification_enabled=False)
        identity = Mock(status='ready')
        pipeline = CorePipeline('cam1', SimpleNamespace(get=lambda:rc), identity=identity)
        updates = pipeline.on_frame(np.zeros((80,80,3),dtype=np.uint8),
            [{'track_id':'t1','bbox':[.2,.2,.6,.8]}], 'run', now)
        identity.check.assert_not_called()
        self.assertEqual(updates[-1]['payload']['capabilities']['entrance'], 'not_enabled')
        self.assertTrue(updates[-1]['payload']['seats'])

    def test_delayed_entrance_result_keeps_original_frame_and_separate_observation(self):
        import time
        from worker.core_behaviors import CorePipeline
        from worker.a_context import RuleContext
        from types import SimpleNamespace
        now=time.time()
        rc=RuleContext(snapshot={'project_id':'p','room_id':'r','session_id':'s','stage_snapshot_id':'snap',
                    'phase_id':'DETAIL','phase_version':1},loaded_at=now)
        entry={'track_id':'t','status':'matched','person_id':'p1','authorized':False,
               'authorization_snapshot_id':'roster','occurred_at':now-5,'_capture_jpeg':b'original'}
        identity=SimpleNamespace(status='ready',check=lambda *args:[dict(entry)])
        pipeline=CorePipeline('cam1',SimpleNamespace(get=lambda:rc),identity=identity)
        updates=pipeline.on_frame(np.zeros((80,80,3),dtype=np.uint8),[],'run',now,'current.jpg')
        result=updates[0]
        self.assertEqual(result['occurred_at'],now-5)
        self.assertEqual(result['capture_jpeg'],b'original')
        self.assertTrue(result['payload']['identity_only'])
        self.assertIsNone(result['payload']['frame_key'])
        self.assertNotIn('_capture_jpeg',json.dumps(result['payload']))
        self.assertEqual(updates[-1]['occurred_at'],now)
        self.assertEqual(updates[-1]['payload']['entries'],[])
