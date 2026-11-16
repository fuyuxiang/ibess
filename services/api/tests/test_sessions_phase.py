"""Authenticated, versioned phase transition and current-phase compatibility."""
import unittest
from uuid import uuid4
import test_poc_workflow as workflow


class SessionPhaseApiTest(unittest.TestCase):
    setUpClass = classmethod(workflow.POCWorkflowTest.setUpClass.__func__)
    tearDownClass = classmethod(workflow.POCWorkflowTest.tearDownClass.__func__)
    setUp = workflow.POCWorkflowTest.setUp
    tearDown = workflow.POCWorkflowTest.tearDown

    def test_phase_transition_persists_current_phase_and_rejects_unknown_phase(self):
        project = self.client.post('/api/registry/projects', json={'code':uuid4().hex,'name':'阶段回归','owner':'测试','status':'running'}).json()
        room = self.client.post('/api/registry/rooms', json={'project_id':project['project_id'],'code':'01','name':'测试室','location':'测试','mode':'centralized','enabled':True}).json()
        created = self.client.post('/api/sessions', json={'name':'阶段回归','current_phase':'SIGNIN'}).json()
        sid = created['session_id']
        bound = self.client.put(f'/api/sessions/{sid}/binding', json={'project_id':project['project_id'],'room_id':room['room_id'],'source_id':'camera-001'})
        self.assertEqual(bound.status_code,200,bound.text)
        self.app.state.phase_control_required_consumers = ['event-service']
        self.addCleanup(setattr,self.app.state,'phase_control_required_consumers',None)
        body = {'phase':'DETAIL','expected_version':0,'enabled_scenes':['S04','S10'],
                'rule_versions':{'S04':'v1','S10':'v1'},'reason':'阶段回归'}
        response = self.client.patch(f'/api/sessions/{sid}/phase',json=body)
        self.assertEqual(response.status_code,201,response.text)
        self.assertEqual(self.client.get(f'/api/sessions/{sid}').json()['current_phase'],'DETAIL')
        config = self.client.get(f'/api/sessions/{sid}/phase-config')
        self.assertEqual(config.status_code,200,config.text)
        self.assertEqual(config.json()['phase_id'],'DETAIL')
        self.assertIn('S04',config.json()['enabled_scenes'])
        invalid = self.client.patch(f'/api/sessions/{sid}/phase',json={**body,'phase':'NOT_A_PHASE','expected_version':1})
        self.assertEqual(invalid.status_code,422,invalid.text)
