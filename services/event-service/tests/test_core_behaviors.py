import json
import unittest
from service.core_behaviors import CoreRules
from service.operators import OperatorConfig, evaluate_operator


def config(code, **parameters):
    return OperatorConfig(code, 'v2', True, ('behavior_detected',), code, 'medium', parameters)


def observation(at=100, **values):
    payload = {'bound': True, 'quality': 'clear', 'phase_id': 'DETAIL', 'phase_version': 1,
               'media_run_id': 'run-1', 'seats': [], 'visits': [], 'entries': [], 'facts': []}
    payload.update(values)
    return {'type': 'behavior_detected', 'source_id': 'cam1', 'session_id': 'session1', 'occurred_at': at}, payload


class CoreRulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = CoreRules()

    def run_rule(self, code, at=100, params=None, **payload):
        data, body = observation(at, **payload)
        return self.rules.evaluate(config(code, **(params or {})), data, body)

    def test_whisper_requires_temporal_evidence_independent_phase_and_real_object_for_transfer(self):
        fact = {'kind': 'whispering', 'track_ids': ['t1', 't2'], 'visibility': 'clear',
                'evidence_frames': [0, 3], 'duration_seconds': 4}
        self.assertEqual(len(self.run_rule('whispering', facts=[fact], review_only=True)), 1)
        self.assertFalse(self.run_rule('whispering', 101, facts=[fact], review_only=True, phase_id='CLARIFY'))
        self.assertFalse(self.run_rule('whispering', 102, facts=[{**fact, 'visibility': 'unclear'}], review_only=True))
        self.assertFalse(self.run_rule('document_transfer', facts=[{**fact, 'kind': 'document_transfer'}], review_only=True))
        self.assertTrue(self.run_rule('document_transfer', 101, facts=[{**fact, 'kind': 'document_transfer', 'object_visible': True}], review_only=True))

    def test_delayed_review_is_not_suppressed_by_current_frame_heartbeat(self):
        self.run_rule('whispering', 110)
        fact = {'kind': 'whispering', 'track_ids': ['t1','t2'], 'visibility': 'clear', 'evidence_frames':[0,2], 'duration_seconds':4}
        self.assertTrue(self.run_rule('whispering', 105, facts=[fact], review_only=True))

    def test_workstation_dwell_excludes_own_seat_and_permission(self):
        visit = {'track_id':'t1', 'home_seat_id':'a', 'seat_id':'b', 'zone_id':'b'}
        self.assertFalse(self.run_rule('workstation_visit', visits=[visit]))
        self.run_rule('workstation_visit', 103, visits=[visit])
        found = self.run_rule('workstation_visit', 106, visits=[visit])
        self.assertEqual(found[0][1].detail['started_at'], 100)
        self.assertFalse(self.run_rule('workstation_visit', 107, visits=[{**visit,'authorized':True}]))

    def test_frequent_absence_requires_transitions_not_initially_empty_seat(self):
        seat = {'seat_id':'a','occupied':False}
        self.assertFalse(self.run_rule('frequent_absence', seats=[seat], params={'count_threshold':2}))
        for at, occupied in [(101,True),(102,False),(103,True),(104,False)]:
            found = self.run_rule('frequent_absence', at, seats=[{**seat,'occupied':occupied}], params={'count_threshold':2})
        self.assertEqual(found[0][1].detail['departure_count'], 2)
        repeated=self.run_rule('frequent_absence',104,seats=[seat],params={'count_threshold':2})
        self.assertEqual(repeated[0][1].detail['departure_count'],2)
        self.assertFalse(self.run_rule('frequent_absence', 105, seats=[seat], params={'count_threshold':2}, phase_version=2))

    def test_joint_departure_and_permissions_and_sampling_gap(self):
        seats = [{'seat_id':s,'occupied':True} for s in ('a','b')]
        self.run_rule('joint_absence', seats=seats)
        missing = [{**s,'occupied':False} for s in seats]
        self.run_rule('joint_absence', 101, seats=missing)
        self.run_rule('joint_absence', 104, seats=missing)
        found = self.run_rule('joint_absence', 107, seats=missing)
        self.assertEqual(found[0][1].detail['seat_ids'], ['a','b'])
        self.assertFalse(self.run_rule('joint_absence', 108, seats=[{**s,'authorized':True} for s in missing]))
        self.assertFalse(self.run_rule('joint_absence', 120, seats=missing))

    def test_entrance_unknown_is_not_unauthorized(self):
        entry = {'track_id':'t1','status':'unverified','authorized':False}
        self.assertFalse(self.run_rule('unauthorized_entry', entries=[entry]))
        found = self.run_rule('unauthorized_entry', 101, entries=[{**entry,'status':'matched','person_id':'p1','authorization_snapshot_id':'auth1'}])
        self.assertEqual(found[0][1].detail['person_id'], 'p1')
        self.assertFalse(self.run_rule('unauthorized_entry', 102, entries=[{**entry,'status':'matched','person_id':'p1','authorization_snapshot_id':'auth1','authorized':True}]))

    def test_joint_exit_uses_door_crossings_and_excludes_permission(self):
        self.assertFalse(self.run_rule('joint_absence', crossings=[{'track_id':'a','direction':'out'}]))
        found=self.run_rule('joint_absence', 101, crossings=[{'track_id':'b','direction':'out'}])
        self.assertEqual(found[0][1].detail['basis'],'exit_crossings')
        self.assertFalse(self.run_rule('joint_absence',102,crossings=[{'track_id':'a','direction':'in'}]))
        self.assertFalse(self.run_rule('joint_absence',103,crossings=[{'track_id':'c','direction':'out','authorized':True}]))

    def test_phone_action_needs_visible_device_and_capture_target(self):
        fact={'kind':'phone_capture','track_ids':['t1'],'visibility':'clear','evidence_frames':[0,2],
              'duration_seconds':3,'object_visible':True}
        self.assertFalse(self.run_rule('phone_capture',facts=[fact],review_only=True))
        self.assertTrue(self.run_rule('phone_capture',101,facts=[{**fact,'target_visible':True}],review_only=True))
        self.assertFalse(self.run_rule('phone_call',facts=[{**fact,'kind':'phone_call','object_visible':False}],review_only=True))

    def test_workstation_zone_binding_does_not_suppress_the_matching_candidate(self):
        c=config('workstation_visit',dwell_seconds=1)
        c=OperatorConfig(**{**c.__dict__,'zone_id':'target'})
        data,payload=observation(visits=[{'track_id':'t','home_seat_id':'a','seat_id':'b','zone_id':'target'}])
        self.assertFalse(self.rules.evaluate(c,data,payload))
        self.assertTrue(self.rules.evaluate(c,{**data,'occurred_at':102},payload))
        self.assertFalse(self.rules.evaluate(c,{**data,'source_id':'other','occurred_at':104},payload))

    def test_spatial_crowd_and_single_person_intrusion(self):
        def old(code, payload):
            c = OperatorConfig(code,'v2',True,('person_detected',),code,'medium',{})
            return evaluate_operator(c, {'type':'person_detected'},payload)
        self.assertIsNone(old('crowd_gathering', {'count':5,'person_boxes':[[0,0,.1,.2],[.4,0,.5,.2],[.8,0,.9,.2]]}))
        self.assertIsNotNone(old('crowd_gathering', {'person_boxes':[[.2,.2,.3,.5],[.25,.2,.35,.5],[.3,.2,.4,.5]]}))
        crowd=OperatorConfig('crowd_gathering','v2',True,('person_detected',),'crowd_gathering','medium',{})
        self.assertIsNone(evaluate_operator(crowd,{'type':'person_detected','phase_id':'CLARIFY'},
            {'person_boxes':[[.2,.2,.3,.5],[.25,.2,.35,.5],[.3,.2,.4,.5]]}))
        self.assertIsNotNone(old('zone_intrusion', {'inside':True,'person_count':1}))
        p = {'phone_boxes':[[.3,.7,.4,.8]], 'person_boxes':[[.1,.1,.8,.9]],'phone_postures':['hand']}
        self.assertIsNone(old('phone_call',p))
        self.assertIsNone(old('phone_capture',p))
        self.assertIsNotNone(old('phone_detected',p))

    def test_parameter_contract_matches_api(self):
        from pathlib import Path
        import runpy
        from service.rule_parameters import PARAMETERS
        api = Path(__file__).resolve().parents[2]/'api/app/core/rule_parameters.py'
        self.assertEqual(PARAMETERS, runpy.run_path(str(api))['PARAMETERS'])
