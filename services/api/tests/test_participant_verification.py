"""限时人员核验授权及结果持久化；每次使用独立 PostgreSQL schema。"""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from test_face_match import FaceMatchTest


class ParticipantVerificationTest(FaceMatchTest):
    def setUp(self):
        super().setUp()
        self.upload('put')
        self.public = TestClient(self.app)
        self.public.headers['X-Requested-With'] = 'XMLHttpRequest'
        self.engine_patch = patch('app.routers.participant_verification.get_face_engine', return_value=self.fake)
        self.engine_patch.start()
        self.admin_links = self.url + '/verification-links'

    def tearDown(self):
        self.engine_patch.stop()
        self.public.close()
        super().tearDown()

    def link(self, **body):
        response = self.client.post(self.admin_links, json=body)
        self.assertEqual(response.status_code, 200)
        value = response.json()
        self.public.headers['Authorization'] = 'Bearer ' + value['verification_url'].split('#')[1]
        return value

    def submit(self, **extra):
        return self.public.post('/api/participant-verification/submit', files={'file': ('candidate.png', b'probe', 'image/png')}, **extra)

    def context(self):
        return self.public.get('/api/participant-verification/context')

    def test_participant_bound_token_cannot_access_admin_or_change_identity(self):
        self.assertEqual(self.context().status_code, 404)
        link = self.link()
        info = self.context()
        self.assertEqual(info.status_code, 200)
        self.assertEqual(info.json()['person']['name'], self.person['name'])
        self.assertTrue(info.json()['can_verify'])
        self.assertNotIn('photo_url', info.json())
        for path in ['/api/faces/status', self.url, self.url + '/photo', '/api/registry/config', self.admin_links]:
            self.assertEqual(self.public.get(path).status_code, 401)
        self.assertEqual(self.public.put(self.url).status_code, 401)
        r = self.submit(data={'person_id': 'someone-else'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['person_name'], self.person['name'])
        self.assertEqual(r.json()['link_id'], link['link_id'])
        self.assertEqual(self.submit().status_code, 409)
        self.assertEqual(self.context().json()['status'], 'completed')

    def test_participant_token_is_hashed_lists_do_not_disclose_it(self):
        from app.face_models import PersonVerificationLink
        link = self.link()
        token = link['verification_url'].split('#')[1]
        with self.Session() as s:
            row = s.get(PersonVerificationLink, link['link_id'])
            self.assertNotEqual(row.token_hash, token)
            self.assertEqual(len(row.token_hash), 64)
        listed = self.client.get(self.admin_links)
        self.assertEqual(listed.status_code, 200)
        self.assertNotIn(token, listed.text)
        self.assertNotIn('token_hash', listed.text)
        self.assertEqual(self.client.get(self.url + '/verification-results').json()['items'], [])

    def test_participant_attempts_and_invalid_images_are_recorded_without_probe(self):
        from app.core.face_match import FaceMatchError
        self.fake.compare.return_value = 0.1
        self.link()
        r = self.submit()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['status'], 'unmatched')
        self.assertEqual(self.context().json()['remaining_attempts'], 4)
        self.fake.extract.side_effect = FaceMatchError(422, '未检测到人脸')
        self.assertEqual(self.submit().status_code, 422)
        self.fake.extract.side_effect = None
        for _ in range(3):
            self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.submit().status_code, 429)
        context = self.context().json()
        self.assertEqual(context['status'], 'exhausted')
        self.assertFalse(context['can_verify'])
        results = self.client.get(self.url + '/verification-results').json()['items']
        self.assertEqual(len(results), 5)
        self.assertEqual(sum(r['status'] == 'invalid' for r in results), 1)
        self.assertTrue(all('photo' not in r and 'embedding' not in r for r in results))

    def test_participant_revocation_expiration_regeneration_and_bottom_photo_change(self):
        from app.face_models import PersonVerificationLink
        a = self.link()
        b = self.link()
        token_b = self.public.headers['Authorization']
        self.public.headers['Authorization'] = 'Bearer ' + a['verification_url'].split('#')[1]
        self.assertEqual(self.context().status_code, 410)
        self.public.headers['Authorization'] = token_b
        self.assertEqual(self.client.delete('/api/faces/verification-links/' + b['link_id']).status_code, 200)
        self.assertEqual(self.submit().status_code, 410)
        c = self.link()
        with self.Session() as s:
            s.get(PersonVerificationLink, c['link_id']).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            s.commit()
        self.assertEqual(self.context().status_code, 410)
        d = self.link()
        self.upload('put')
        self.assertEqual(self.context().status_code, 409)
        self.assertEqual(self.submit().status_code, 409)

    def test_participant_origin_headers_csrf_and_method_scope(self):
        self.link()
        self.assertEqual(self.submit(headers={'Origin': 'https://wrong.example'}).status_code, 403)
        self.public.headers.pop('X-Requested-With')
        self.assertEqual(self.submit().status_code, 403)
        self.assertEqual(self.public.post('/api/participant-verification/context').status_code, 401)
        self.assertEqual(self.public.get('/api/participant-verification/submit').status_code, 401)
        self.client.headers.pop('X-CSRF-Token')
        self.assertEqual(self.client.post(self.admin_links, json={}).status_code, 403)

    def test_participant_generation_requires_reference_and_valid_business_binding(self):
        r = self.client.post(self.admin_links, json={'room_id': '00000000-0000-0000-0000-000000000000'})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.client.post(self.admin_links, json={'expires_in_minutes': 2000}).status_code, 422)
        project = self.client.post('/api/registry/projects', json={'code':'BIND','name':'核验项目','owner':'测试','status':'running'}).json()
        self.assertEqual(self.client.post(self.admin_links, json={'project_id': project['project_id']}).status_code, 422)
        self.client.post('/api/registry/participants', json={'project_id':project['project_id'],'person_id':self.person['person_id'],'role':'expert'})
        room = self.client.post('/api/registry/rooms', json={'project_id':project['project_id'],'code':'R','name':'核验室','location':'测试','mode':'remote','enabled':True}).json()
        self.link(project_id=project['project_id'], room_id=room['room_id'])
        self.assertEqual(self.context().json()['project_name'], '核验项目')
        self.assertEqual(self.context().json()['room_name'], '核验室')
        self.client.delete(self.url)
        self.assertEqual(self.client.post(self.admin_links, json={}).status_code, 409)
        self.assertEqual(self.context().status_code, 409)

    def test_participant_disabled_person_invalidates_link(self):
        self.link()
        r = self.client.put('/api/registry/persons/' + self.person['person_id'], json={
            **{k:self.person[k] for k in ['code','name','unit','role']}, 'enabled':False,
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.context().status_code, 410)
        self.assertEqual(self.submit().status_code, 410)
        self.assertEqual(self.client.post(self.admin_links, json={}).status_code, 422)

    def test_participant_model_unavailable_does_not_consume_attempt(self):
        from app.core.face_match import FaceMatchError
        self.link()
        with patch('app.routers.participant_verification.get_face_engine', side_effect=FaceMatchError(503, '模型未就绪')):
            info = self.context().json()
            self.assertFalse(info['can_verify'])
            self.assertFalse(info['model_ready'])
            self.assertEqual(self.submit().status_code, 503)
        self.assertEqual(self.context().json()['remaining_attempts'], 5)

    def test_participant_success_is_serialized_and_records_survive_person_delete(self):
        from concurrent.futures import ThreadPoolExecutor
        from app.face_models import PersonVerificationResult
        link = self.link()
        authorization = self.public.headers['Authorization']
        def run():
            with TestClient(self.app) as client:
                return client.post('/api/participant-verification/submit',
                    headers={'Authorization':authorization, 'X-Requested-With':'XMLHttpRequest'},
                    files={'file':('probe.png', b'probe', 'image/png')}).status_code
        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(lambda _: run(), range(2)))
        self.assertEqual(sorted(statuses), [200, 409])
        self.assertEqual(len(self.client.get(self.url + '/verification-results').json()['items']), 1)
        self.assertEqual(self.client.delete('/api/registry/persons/' + self.person['person_id']).status_code, 200)
        with self.Session() as s:
            result = s.scalar(select(PersonVerificationResult))
            self.assertIsNone(result.person_id)
            self.assertIsNone(result.link_id)
            self.assertEqual(result.person_name, self.person['name'])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(ParticipantVerificationTest(name) for name in loader.getTestCaseNames(ParticipantVerificationTest) if name.startswith('test_participant_'))
