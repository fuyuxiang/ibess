"""The simplified personnel flow retains explicit identity evidence, immutable versions and administrator boundaries."""
from datetime import datetime, timezone
import uuid
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_business_authz import BusinessAuthzTest


class RosterManagementTest(BusinessAuthzTest):
    @classmethod
    def setUpClass(cls):
        from app.schema import load_metadata
        load_metadata()
        super().setUpClass()
        from app.routers.rosters import router
        cls.app.include_router(router, prefix='/api')

    def ready_person(self, person_id, *, linked=False):
        from app.face_models import FaceProfile
        fact = self._fact('check_in', person_id)
        result = self._verify(fact)
        with self.Session.begin() as db:
            db.add(FaceProfile(person_id=person_id, version='v1', model='roster-test',
                registered_at=datetime.now(timezone.utc), embedding=[0.0]*128, photo=b'synthetic'))
            db.get(self.Result, result).model = 'roster-test'
            if not linked:
                db.query(self.Link).filter_by(fact_id=fact).delete()
        return fact, result

    def state(self, session_id=None):
        response = self.client.get('/api/rosters/'+self.room_id, params={'session_id':session_id} if session_id else {})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def scoped_session(self, enabled=False):
        from app.models import EvalSession
        from app.phase_models import PhaseBinding
        from app.session_workflow_models import SessionWorkflow
        sid = str(uuid.uuid4())
        with self.Session.begin() as db:
            db.add(EvalSession(session_id=sid,name='名单可选场次',status='draft'));db.flush()
            db.add(PhaseBinding(session_id=sid,project_id=self.project_id,room_id=self.room_id,source_id='camera-001'))
            db.add(SessionWorkflow(session_id=sid,identity_verification_enabled=enabled))
        return sid

    def test_optional_roster_saves_unverified_people_without_fabricating_identity(self):
        from app.face_models import FaceProfile
        from app.session_workflow_models import SessionWorkflow
        sid = self.scoped_session()
        fact = self._fact('check_in',self.p1_id)
        before = self.state(sid)
        saved = self.confirm(before, session_id=sid)
        self.assertEqual(saved.status_code,201,saved.text)
        self.assertFalse(saved.json()['identity_verification_enabled'])
        self.assertFalse(saved.json()['items'][0]['verified'])
        with self.Session() as db:
            snapshot = db.query(self.Snapshot).filter_by(room_id=self.room_id).one()
            self.assertFalse(snapshot.entries[0].identity_verified)
            self.assertIsNone(snapshot.entries[0].verification_result_id)
            self.assertEqual(snapshot.entries[0].basis_fact_id,fact)
            self.assertIsNone(db.get(FaceProfile,self.p1_id))
        old = saved.json()
        with self.Session.begin() as db:
            db.get(SessionWorkflow,sid).identity_verification_enabled=True
        self.assertEqual(self.confirm(old,session_id=sid).status_code,409)
        self.assertEqual(self.confirm(self.state(sid),session_id=sid).status_code,422)
        with self.Session() as db:
            self.assertEqual(db.query(self.Snapshot).filter_by(room_id=self.room_id).count(),1)

    def test_scoped_roster_rejects_other_room_and_closed_session_writes(self):
        from app.models import EvalSession
        from app.phase_models import PhaseBinding
        sid = self.scoped_session()
        fact = self._fact('check_in',self.p1_id)
        before = self.state(sid)
        with self.Session.begin() as db:
            db.query(EvalSession).filter_by(session_id=sid).one().status='closed'
        self.assertEqual(self.confirm(before,session_id=sid).status_code,409)
        self.assertEqual(self.client.delete('/api/facts/'+fact,params={'session_id':sid}).status_code,409)
        self.assertEqual(self.client.post('/api/facts',json={'session_id':sid,'project_id':self.project_id,
            'room_id':self.room_id,'fact_type':'check_in','person_id':self.p2_id}).status_code,409)
        with self.Session.begin() as db:
            other = self.Room(project_id=self.project_id,code=uuid.uuid4().hex[:8],name='其他评标室',location='测试',enabled=True)
            db.add(other);db.flush()
            db.query(PhaseBinding).filter_by(session_id=sid).one().room_id=other.room_id
        self.assertEqual(self.client.get('/api/rosters/'+self.room_id,params={'session_id':sid}).status_code,422)

    def test_draft_personnel_writes_cannot_change_another_running_session(self):
        from app.models import EvalSession
        sid, active = self.scoped_session(), self.scoped_session()
        fact = self._fact('check_in',self.p1_id)
        with self.Session.begin() as db:
            db.query(EvalSession).filter_by(session_id=active).one().status='running'
        self.assertEqual(self.confirm(self.state(sid),session_id=sid).status_code,409)
        self.assertEqual(self.client.delete('/api/facts/'+fact,params={'session_id':sid}).status_code,409)
        self.assertEqual(self.client.post('/api/facts',json={'session_id':sid,'project_id':self.project_id,
            'room_id':self.room_id,'fact_type':'check_in','person_id':self.p2_id}).status_code,409)

    def test_optional_mode_retains_face_result_and_previous_authorization_evidence(self):
        from app.face_models import FaceProfile
        sid = self.scoped_session()
        _, result = self.ready_person(self.p1_id,linked=True)
        previous = self._generate()['snapshot_id']
        saved = self.confirm(self.state(sid),session_id=sid)
        self.assertEqual(saved.status_code,201,saved.text)
        self.assertEqual(sum(row['current'] for row in saved.json()['history']),1)
        with self.Session() as db:
            old = db.get(self.Snapshot,previous)
            self.assertEqual(old.status,'superseded')
            self.assertEqual(old.entries[0].verification_result_id,result)
            self.assertIsNotNone(db.get(self.Result,result))
            self.assertEqual(db.get(FaceProfile,self.p1_id).version,'v1')
            current = db.query(self.Snapshot).filter_by(room_id=self.room_id,status='active').one()
            self.assertEqual(current.entries[0].verification_result_id,result)

    def confirm(self, state, selections=(), **extra):
        return self.client.post('/api/rosters/'+self.room_id+'/confirm', json={
            'expected_version': state['version'], 'information_visibility': 'public',
            'selections': [{'person_id': p, 'verification_result_id': r} for p, r in selections], **extra})

    def test_requires_explicit_evidence_then_saves_links_and_authorization_together(self):
        fact, result = self.ready_person(self.p1_id)
        before = self.state()
        self.assertFalse(before['items'][0]['verified'])
        self.assertEqual(self.confirm(before).status_code, 422)
        with self.Session() as db:
            self.assertEqual(db.query(self.Link).filter_by(fact_id=fact).count(), 0)
            self.assertEqual(db.query(self.Snapshot).filter_by(room_id=self.room_id).count(), 0)
        saved = self.confirm(before, [(self.p1_id, result)])
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertTrue(saved.json()['items'][0]['verified'])
        self.assertFalse(saved.json()['needs_confirmation'])
        self.assertEqual(self.confirm(before, [(self.p1_id, result)]).status_code, 409)
        with self.Session() as db:
            row = db.query(self.Snapshot).filter_by(room_id=self.room_id).one()
            self.assertEqual(row.entries[0].verification_result_id, result)
            self.assertEqual(row.created_by, 'wyh')

    def test_a_later_invalid_person_rolls_back_every_link_and_retains_previous_version(self):
        first_fact, first_result = self.ready_person(self.p1_id)
        self.assertEqual(self.confirm(self.state(), [(self.p1_id, first_result)]).status_code, 201)
        second_fact = self._fact('check_in', self.p2_id)
        bad = self.confirm(self.state(), [(self.p2_id, first_result)])
        self.assertEqual(bad.status_code, 422, bad.text)
        with self.Session() as db:
            self.assertEqual(db.query(self.Snapshot).filter_by(room_id=self.room_id).count(), 1)
            self.assertEqual(db.query(self.Link).filter_by(fact_id=second_fact).count(), 0)
            original = db.query(self.Snapshot).filter_by(room_id=self.room_id).one()
            self.assertEqual(original.status, 'active')
            self.assertEqual(original.entries[0].basis_fact_id, first_fact)
        self._fact('check_out', self.p1_id)
        self._fact('check_out', self.p2_id)
        empty = self.confirm(self.state())
        self.assertEqual(empty.status_code, 201, empty.text)
        with self.Session() as db:
            rows = db.query(self.Snapshot).filter_by(room_id=self.room_id).all()
            self.assertEqual(len(rows), 2)
            self.assertEqual(sorted(len(r.entries) for r in rows), [0, 1])
            self.assertEqual(sum(r.status == 'active' for r in rows), 1)

    def test_attendance_or_reference_change_rejects_stale_confirmation(self):
        from app.face_models import FaceProfile
        _, result = self.ready_person(self.p1_id)
        old = self.state()
        self._fact('check_in', self.p2_id)
        self.assertEqual(self.confirm(old, [(self.p1_id, result)]).status_code, 409)
        newer = self.state()
        with self.Session.begin() as db:
            db.get(FaceProfile, self.p1_id).version = 'v2'
        self.assertEqual(self.confirm(newer, [(self.p1_id, result)]).status_code, 409)
        self.assertFalse(self.state()['items'][0]['verifications'])

    def test_confidential_policy_failure_does_not_leave_links_or_new_versions(self):
        fact, result = self.ready_person(self.p1_id)
        state = self.state()
        response = self.confirm(state, [(self.p1_id, result)], information_visibility='confidential')
        self.assertEqual(response.status_code, 422, response.text)
        with self.Session() as db:
            self.assertEqual(db.query(self.Link).filter_by(fact_id=fact).count(), 0)
            self.assertEqual(db.query(self.Snapshot).filter_by(room_id=self.room_id).count(), 0)
        saved = self.confirm(state, [(self.p1_id, result)], information_visibility='confidential',
            confidentiality_policy_id='site-policy', recipients=[self.p1_id])
        self.assertEqual(saved.status_code, 201, saved.text)
        with self.Session() as db:
            row = db.query(self.Snapshot).filter_by(room_id=self.room_id).one()
            self.assertTrue(row.entries[0].recipient_authorized)
            self.assertEqual(row.confidentiality_policy_id, 'site-policy')

    def test_cross_room_results_and_duplicate_or_outside_grants_are_rejected(self):
        from app.face_models import PersonVerificationLink
        _, result_id = self.ready_person(self.p1_id)
        with self.Session.begin() as db:
            other = self.Room(project_id=self.project_id, code='OTHER-'+uuid.uuid4().hex[:6], name='其他评标室', location='测试楼', enabled=True)
            db.add(other); db.flush()
            link = PersonVerificationLink(link_id=str(uuid.uuid4()), person_id=self.p1_id,
                token_hash=uuid.uuid4().hex*2, reference_version='v1', room_id=other.room_id,
                created_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc), attempts=0, max_attempts=5)
            db.add(link); db.flush()
            db.get(self.Result, result_id).link_id = link.link_id
        state = self.state()
        self.assertFalse(state['items'][0]['verifications'])
        self.assertEqual(self.confirm(state, [(self.p1_id, result_id)]).status_code, 422)
        self.assertEqual(self.confirm(state, recipients=[self.p2_id]).status_code, 422)
        self.assertEqual(self.confirm(state, [(self.p1_id, result_id)]*2).status_code, 422)

    def test_reference_cannot_be_replaced_while_confirmation_is_writing(self):
        from sqlalchemy import text
        from sqlalchemy.exc import OperationalError
        from app.application.authorization_roster import save_authorization
        _, result = self.ready_person(self.p1_id)
        def check_lock(*args, **kwargs):
            with self.Session() as other:
                other.execute(text("SET LOCAL lock_timeout = '100ms'"))
                with self.assertRaises(OperationalError):
                    other.execute(text('SELECT person_id FROM business_persons WHERE person_id = :id FOR UPDATE'),
                                  {'id': self.p1_id})
            return save_authorization(*args, **kwargs)
        with patch('app.routers.rosters.save_authorization', side_effect=check_lock):
            response = self.confirm(self.state(), [(self.p1_id, result)])
        self.assertEqual(response.status_code, 201, response.text)

    def test_a_legacy_wrong_person_link_cannot_confirm_the_new_roster(self):
        _, result = self.ready_person(self.p1_id, linked=True)
        with self.Session.begin() as db:
            db.get(self.Result, result).person_id = self.p2_id
        state = self.state()
        self.assertFalse(state['items'][0]['verified'])
        self.assertFalse(state['items'][0]['verifications'])
        self.assertEqual(self.confirm(state).status_code, 422)

    def test_each_non_admin_role_is_rejected_by_endpoint_and_existing_auth_policy(self):
        from app.auth import role_path_allowed
        from app.db import get_session
        from app.routers.rosters import router
        for role in ('viewer', 'operator', 'supervisor'):
            app = FastAPI(); app.include_router(router, prefix='/api')
            @app.middleware('http')
            async def account(request, call_next, selected=role):
                request.state.admin = {'username': 'existing-test-role', 'role': selected}
                return await call_next(request)
            def sessions():
                with self.Session() as db:
                    yield db
            app.dependency_overrides[get_session] = sessions
            client = TestClient(app)
            path = '/api/rosters/'+self.room_id
            self.assertEqual(client.get(path).status_code, 403)
            self.assertEqual(client.post(path+'/confirm', json={'expected_version':'a'*64,'information_visibility':'public'}).status_code, 403)
            self.assertFalse(role_path_allowed(role, path, 'GET'))
            self.assertFalse(role_path_allowed(role, path+'/confirm', 'POST'))
