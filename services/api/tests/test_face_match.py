"""独立 schema 验证人脸登记生命周期，不修改正式人员资料。"""
import unittest
from unittest.mock import patch

from test_poc_workflow import POCWorkflowTest


class FaceMatchTest(POCWorkflowTest):
    def setUp(self):
        super().setUp()
        from app.core.face_match import FaceSample
        from app.routers.faces import ready_engine as get_face_engine
        import numpy as np
        self.get_engine = get_face_engine
        self.sample = FaceSample(np.ones(128, dtype=np.float32) / np.sqrt(128), b'jpeg-test')
        self.fake = unittest.mock.Mock()
        self.fake.model = 'test-model-v1'
        self.fake.threshold = 0.363
        self.fake.extract.return_value = self.sample
        self.fake.compare.return_value = 0.8
        self.app.dependency_overrides[get_face_engine] = lambda: self.fake
        r = self.client.post('/api/registry/persons', json={
            'code': 'FACE-TEST', 'name': '测试人员', 'unit': '测试', 'role': 'expert', 'enabled': True,
        })
        self.assertEqual(r.status_code, 201, r.text)
        self.person = r.json()
        self.url = '/api/faces/persons/' + self.person['person_id']

    def tearDown(self):
        self.app.dependency_overrides.pop(self.get_engine, None)
        super().tearDown()

    def upload(self, method, suffix='', data=b'test-image'):
        return getattr(self.client, method)(self.url + suffix, files={'file': ('face.png', data, 'image/png')})

    def test_face_lifecycle_and_private_response(self):
        self.assertFalse(self.client.get(self.url).json()['registered'])
        self.assertEqual(self.upload('post', '/verify').status_code, 409)
        r = self.upload('put')
        self.assertEqual(r.status_code, 200, r.text)
        a = r.json()
        self.assertTrue(a['registered'])
        self.assertNotIn('embedding', a)
        self.assertNotIn('photo', a)
        photo = self.client.get(a['photo_url'])
        self.assertEqual(photo.content, b'jpeg-test')
        self.assertEqual(photo.headers['cache-control'], 'no-store')
        verified = self.upload('post', '/verify')
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertTrue(verified.json()['matched'])
        self.assertFalse(verified.json()['liveness_checked'])
        self.assertEqual(verified.json()['reference_version'], a['version'])
        self.fake.compare.return_value = 0.1
        self.assertFalse(self.upload('post', '/verify').json()['matched'])
        b = self.upload('put').json()
        self.assertNotEqual(a['version'], b['version'])
        self.assertEqual(self.client.delete(self.url).status_code, 200)
        self.assertFalse(self.client.get(self.url).json()['registered'])
        self.assertEqual(self.client.get(a['photo_url']).status_code, 404)

    def test_face_invalid_input_cannot_overwrite_and_limits_apply(self):
        from app.core.face_match import FaceMatchError, MAX_FILE_BYTES
        a = self.upload('put').json()
        self.fake.extract.side_effect = FaceMatchError(422, '未检测到人脸')
        self.assertEqual(self.upload('put').status_code, 422)
        self.assertEqual(self.client.get(self.url).json()['version'], a['version'])
        self.assertEqual(self.upload('put', data=b'x' * (MAX_FILE_BYTES + 1)).status_code, 413)

    def test_face_authentication_and_csrf_are_required(self):
        from fastapi.testclient import TestClient
        with TestClient(self.app) as anonymous:
            for url in ['/api/faces/status', self.url, self.url + '/photo']:
                self.assertEqual(anonymous.get(url).status_code, 401)
        self.client.headers.pop('X-CSRF-Token')
        self.assertEqual(self.upload('put').status_code, 403)

    def test_face_model_change_and_disabled_person(self):
        self.upload('put')
        self.fake.model = 'test-model-v2'
        self.assertEqual(self.upload('post', '/verify').status_code, 409)
        r = self.client.put('/api/registry/persons/' + self.person['person_id'], json={
            **{k: self.person[k] for k in ['code','name','unit','role']}, 'enabled': False,
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.upload('put').status_code, 422)
        self.assertEqual(self.upload('post', '/verify').status_code, 422)
        self.assertEqual(self.client.delete(self.url).status_code, 200)

    def test_face_reference_changed_during_inference(self):
        from app.face_models import FaceProfile
        a = self.upload('put').json()
        def replace(_):
            with self.Session() as s:
                profile = s.get(FaceProfile, self.person['person_id'])
                profile.version = 'concurrent-change'
                s.commit()
            return self.sample
        self.fake.extract.side_effect = replace
        self.assertEqual(self.upload('post', '/verify').status_code, 409)
        self.assertNotEqual(self.client.get(self.url).json()['version'], a['version'])

    def test_face_person_delete_cascades_profile(self):
        from app.face_models import FaceProfile
        self.upload('put')
        self.assertEqual(self.client.delete('/api/registry/persons/' + self.person['person_id']).status_code, 200)
        with self.Session() as s:
            self.assertIsNone(s.get(FaceProfile, self.person['person_id']))

    def test_face_register_response_survives_immediate_delete(self):
        from app.face_models import FaceProfile
        from sqlalchemy import delete
        original_commit = self.Session.class_.commit
        def concurrent_delete(session):
            is_registration = any(isinstance(row, FaceProfile) for row in list(session.new) + list(session.dirty))
            original_commit(session)
            if is_registration:
                with self.Session() as other:
                    other.execute(delete(FaceProfile).where(FaceProfile.person_id == self.person['person_id']))
                    original_commit(other)
        with patch.object(self.Session.class_, 'commit', concurrent_delete):
            response = self.upload('put')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['registered'])
        self.assertFalse(self.client.get(self.url).json()['registered'])


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(FaceMatchTest):
        if name.startswith('test_face_'):
            suite.addTest(FaceMatchTest(name))
    return suite
