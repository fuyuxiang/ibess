"""业务配置与事件关联回归，复用独立 PostgreSQL schema 测试环境。"""
import uuid
import unittest
import test_poc_workflow as workflow_test


class RegistryEventTest(workflow_test.POCWorkflowTest):
    def entity(self, kind, payload):
        response = self.client.post('/api/registry/' + kind, json=payload)
        self.assertIn(response.status_code, (200, 201), response.text)
        return response.json()

    def setup_business(self):
        project = self.entity('projects', {'code': 'BIZ-001', 'name': '业务关联测试项目', 'owner': '测试负责人', 'status': 'running'})
        room = self.entity('rooms', {'project_id': project['project_id'], 'code': 'A01', 'name': '配置评标室', 'location': '测试场所', 'mode': 'centralized', 'enabled': True})
        person = self.entity('persons', {'code': 'P001', 'name': '测试专家', 'unit': '测试单位', 'role': 'expert', 'enabled': True})
        self.entity('participants', {'project_id': project['project_id'], 'person_id': person['person_id'], 'role': 'expert'})
        seat = self.entity('seats', {'room_id': room['room_id'], 'code': '01', 'person_id': person['person_id'], 'enabled': True})
        return project, room, person, seat

    def configured_event(self, room, seat=None, **extra):
        payload = {'event_type': 'seat_absence', 'room_id': room['room_id'], 'note': '配置关联业务模拟'}
        if seat:
            payload['seat_id'] = seat['seat_id']
        response = self.client.post('/api/demo/events', json={**payload, **extra})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_event_binds_ids_and_preserves_names_after_configuration_rename(self):
        project, room, person, seat = self.setup_business()
        key = str(uuid.uuid4())
        event = self.configured_event(room, seat, request_id=key)
        self.assertEqual(event['project_id'], project['project_id'])
        self.assertEqual(event['room_id'], room['room_id'])
        self.assertEqual(event['seat_id'], seat['seat_id'])
        self.assertEqual(event['person_id'], person['person_id'])
        self.assertEqual(event['person_name'], '测试专家')
        self.assertEqual(event['seat_code'], '01')
        self.assertEqual(event['room'], '配置评标室')
        self.assertEqual(event['evidences'] if 'evidences' in event else [], [])
        for kind, entity, changes in [
            ('projects', project, {'name': '项目改名'}), ('rooms', room, {'name': '评标室改名'}),
            ('persons', person, {'name': '专家改名'}), ('seats', seat, {'code': '02'})]:
            identity = {'projects': 'project_id', 'rooms': 'room_id', 'persons': 'person_id', 'seats': 'seat_id'}[kind]
            response = self.client.put(f"/api/registry/{kind}/{entity[identity]}", json={**entity, **changes})
            self.assertEqual(response.status_code, 200, response.text)
        detail = self.client.get('/api/events/' + event['event_id']).json()
        self.assertEqual((detail['project_name'], detail['room'], detail['person_name'], detail['seat_code']), ('业务关联测试项目', '配置评标室', '测试专家', '01'))
        retry = self.configured_event(room, seat, request_id=key)
        self.assertEqual(retry['event_id'], event['event_id'])
        self.assertEqual(self.client.delete('/api/registry/seats/' + seat['seat_id']).status_code, 409)
        self.assertEqual(self.client.delete('/api/registry/rooms/' + room['room_id']).status_code, 409)

    def test_room_id_filter_distinguishes_same_name_rooms_and_old_unbound_events(self):
        project, room, person, seat = self.setup_business()
        other_project = self.entity('projects', {'code': 'BIZ-002', 'name': '另一个项目', 'owner': '第二项目负责人', 'status': 'draft'})
        other_room = self.entity('rooms', {**room, 'project_id': other_project['project_id']})
        own = self.configured_event(room, seat)
        self.configured_event(other_room)
        legacy = self.create(room=room['name'])
        for params in [{'room_id': room['room_id']}, {'project_id': project['project_id']}]:
            response = self.client.get('/api/events/query', params={**params, 'page_size': 1}).json()
            self.assertEqual(response['total'], 1)
            self.assertEqual(response['stats']['total'], 1)
            self.assertEqual(response['items'][0]['event_id'], own['event_id'])
        old = self.client.get('/api/events/' + legacy['event_id']).json()
        self.assertIsNone(old.get('project_id'))

    def test_invalid_context_closed_project_and_disabled_room_are_rejected(self):
        project, room, _, seat = self.setup_business()
        bad = self.client.post('/api/demo/events', json={'event_type': 'seat_absence', 'seat_id': seat['seat_id']})
        self.assertEqual(bad.status_code, 422)
        another = self.entity('rooms', {**room, 'code': 'A02', 'name': '第二室'})
        mismatch = self.client.post('/api/demo/events', json={'event_type': 'seat_absence', 'room_id': another['room_id'], 'seat_id': seat['seat_id']})
        self.assertEqual(mismatch.status_code, 422)
        self.client.put('/api/registry/projects/' + project['project_id'], json={**project, 'status': 'closed'})
        closed = self.client.post('/api/demo/events', json={'event_type': 'seat_absence', 'room_id': room['room_id']})
        self.assertEqual(closed.status_code, 422)
        self.client.put('/api/registry/projects/' + project['project_id'], json=project)
        self.client.put('/api/registry/rooms/' + room['room_id'], json={**room, 'enabled': False})
        disabled = self.client.post('/api/demo/events', json={'event_type': 'seat_absence', 'room_id': room['room_id']})
        self.assertEqual(disabled.status_code, 422)

    def test_idempotency_rejects_another_room_with_same_name_and_replays_after_project_closes(self):
        project, room, _, seat = self.setup_business()
        another = self.entity('rooms', {**room, 'code': 'A02'})
        key = str(uuid.uuid4())
        event = self.configured_event(room, seat, request_id=key)
        mismatch = self.client.post('/api/demo/events', json={'event_type': 'seat_absence', 'room_id': another['room_id'], 'note': '配置关联业务模拟', 'request_id': key})
        self.assertEqual(mismatch.status_code, 409)
        self.client.put('/api/registry/projects/' + project['project_id'], json={**project, 'status': 'closed'})
        retry = self.configured_event(room, seat, request_id=key)
        self.assertEqual(retry['event_id'], event['event_id'])

    def test_room_stats_group_stable_ids_across_rename_and_distinguish_same_name(self):
        _, room, _, seat = self.setup_business()
        self.configured_event(room, seat)
        response = self.client.put('/api/registry/rooms/' + room['room_id'], json={**room, 'name': '改名后的配置评标室'})
        self.assertEqual(response.status_code, 200)
        self.configured_event(room)
        other_project = self.entity('projects', {'code': 'BIZ-002', 'name': '另一项目', 'owner': '另一负责人', 'status': 'running'})
        other_room = self.entity('rooms', {**room, 'project_id': other_project['project_id'], 'name': '改名后的配置评标室'})
        self.configured_event(other_room)
        groups = self.client.get('/api/events/query').json()['stats']['rooms']
        self.assertEqual({item.get('room_id'): item['value'] for item in groups}, {room['room_id']: 2, other_room['room_id']: 1})


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(RegistryEventTest(name) for name in RegistryEventTest.__dict__ if name.startswith('test_'))
