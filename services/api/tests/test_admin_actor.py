"""登录身份必须由服务端决定，历史处置不能被前端参数伪造。"""
import unittest
import test_poc_workflow as workflow_test


class AdminActorTest(workflow_test.POCWorkflowTest):
    def test_forged_reviewer_is_replaced_with_authenticated_admin(self):
        event = self.create(note='管理员留痕集成测试')
        response = self.decide(event, reviewer_id='forged-reviewer')
        self.assertEqual(response.status_code, 200, response.text)
        detail = self.client.get('/api/events/' + event['event_id']).json()
        self.assertEqual(detail['decisions'][0]['reviewer_id'], 'test_admin')
        self.assertEqual(detail['audit'][0]['actor'], 'test_admin')
        self.assertEqual(detail['audit'][-1]['actor'], 'test_admin')
        exported = self.client.get('/api/events/' + event['event_id'] + '/export')
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(exported.json()['event']['audit'][-1]['actor'], 'test_admin')


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(AdminActorTest(name) for name in AdminActorTest.__dict__ if name.startswith('test_'))
