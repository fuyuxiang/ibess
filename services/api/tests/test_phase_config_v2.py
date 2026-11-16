"""2.1存取逻辑：真实PG临时schema，不导入主应用或修改正式记录。"""
import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import importlib.util
import os
import secrets
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker


class PhaseConfigTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / 'app/phase_models.py').exists(), '2.1持久模型尚未实现')
        self.assertTrue((root / 'app/phase_config.py').exists(), '2.1存取模块尚未实现')
        self.app.state.phase_control_required_consumers=['media-controller','event-dispatcher']
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_hex(32)
        with self.Session.begin() as db:
            db.add(self.AuthSession(token_hash=hashlib.sha256(token.encode('ascii')).hexdigest(), account_id=1,
                expires_at=datetime.now(timezone.utc)+timedelta(hours=1), csrf_token=csrf))
        self.client = TestClient(self.app, headers={'X-Requested-With':'XMLHttpRequest','X-CSRF-Token':csrf})
        self.client.cookies.set('eval_admin_session',token)

    def tearDown(self):
        if hasattr(self, 'client'): self.client.close()

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        # 缺实现时仅由setUp产生预期失败，不接数据库。
        if not all((root / f'app/{n}.py').exists() for n in ('phase_models', 'phase_config')):
            return
        from app import phase_config
        from app.business_models import BusinessProject, BusinessRoom
        from app.models import Base, EvalSession
        from app.phase_models import PhaseBinding, PhaseSnapshot
        from app import phase_control
        from app.phase_control_models import PhaseControlOutbox, PhaseControlAck
        from app.auth import AdminAuthMiddleware
        from app.auth_models import AdminAccount, AdminAudit, AdminSession
        from app.db import get_session
        from app.routers.sessions import router
        cls.logic, cls.Project, cls.Room = phase_config, BusinessProject, BusinessRoom
        cls.EvalSession, cls.Binding, cls.Snapshot = EvalSession, PhaseBinding, PhaseSnapshot
        cls.AuthSession = AdminSession
        cls.Control,cls.Outbox=phase_control,PhaseControlOutbox
        url = os.environ.get('PHASE_TEST_DATABASE_URL')
        if not url:
            raise RuntimeError('PHASE_TEST_DATABASE_URL必须显式指定PostgreSQL测试连接')
        cls.admin = create_engine(url)
        if cls.admin.dialect.name != 'postgresql':
            raise RuntimeError('不得使用SQLite替代PostgreSQL验收')
        cls.schema = 'test_phase_v2_' + uuid4().hex
        with cls.admin.begin() as c:
            c.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        try:
            cls.engine = create_engine(url, connect_args={'options': f'-csearch_path={cls.schema} -clock_timeout=5000 -cstatement_timeout=15000'})
            cls.tables = [BusinessProject.__table__, BusinessRoom.__table__, EvalSession.__table__, PhaseBinding.__table__, PhaseSnapshot.__table__, PhaseControlOutbox.__table__, PhaseControlAck.__table__, AdminAccount.__table__, AdminAudit.__table__, AdminSession.__table__]
            Base.metadata.create_all(cls.engine, tables=cls.tables)
            cls.Session = sessionmaker(bind=cls.engine)
            with cls.Session.begin() as db:
                db.add(AdminAccount(account_id=1,username='test_admin',display_name='Test admin',password_hash='not-a-usable-test-password'))
            cls.app=FastAPI()
            cls.app.add_middleware(AdminAuthMiddleware,session_factory=cls.Session)
            cls.app.include_router(router,prefix='/api')
            def override_db():
                with cls.Session() as db: yield db
            cls.app.dependency_overrides[get_session]=override_db
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'engine'):
            cls.engine.dispose()
        if hasattr(cls, 'schema'):
            with cls.admin.begin() as c:
                c.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
            cls.admin.dispose()

    def setup_business(self):
        p, r, s = uuid4().hex, uuid4().hex, uuid4().hex
        with self.Session.begin() as db:
            db.add(self.Project(project_id=p, code=p, name='Constructed project', owner='Test', status='running'))
            db.flush()
            db.add(self.Room(room_id=r, project_id=p, code=r, name='Constructed room', location='Test', enabled=True))
            db.add(self.EvalSession(session_id=s, name='Constructed session', status='running', current_phase='review'))
        return p, r, s

    def bind(self, p, r, s):
        with self.Session.begin() as db:
            return self.logic.bind_session(db, session_id=s, project_id=p, room_id=r, source_id='camera-test')

    def append(self, s, **changes):
        args = dict(session_id=s, phase_id='DETAIL', expected_version=0, enabled_scenes=['S03'], rule_versions={'S03': 's03-v1', 'S10': 's10-v1'}, actor_id='test-admin', reason='Explicit initial migration', effective_at=datetime(2026, 10, 14, 2, tzinfo=timezone.utc))
        args.update(changes)
        with self.Session.begin() as db:
            return self.logic.append_snapshot(db, **args)

    def assert_error(self, code, action):
        with self.assertRaises(self.logic.PhaseConfigError) as caught:
            action()
        self.assertEqual(code, caught.exception.code)

    def test_explicit_binding_and_first_snapshot_persist(self):
        p, r, s = self.setup_business()
        self.bind(p, r, s)
        first = self.append(s)
        self.assertEqual((p, r, s), tuple(first[k] for k in ('project_id', 'room_id', 'session_id')))
        self.assertEqual(1, first['phase_version'])
        self.assertEqual(['S10'], first['always_on_scenes'])
        self.assertEqual('pending', first['sync_status'])
        with self.Session() as db:
            self.assertEqual(first, self.logic.get_latest_config(db, s))
            self.assertEqual('review', db.scalar(select(self.EvalSession).where(self.EvalSession.session_id == s)).current_phase)

    def test_unbound_or_legacy_phase_never_defaults_to_opening(self):
        p, r, s = self.setup_business()
        with self.Session() as db:
            self.assertIsNone(self.logic.get_latest_config(db, s))
        self.assert_error('BINDING_REQUIRED', lambda: self.append(s))
        self.bind(p, r, s)
        self.assert_error('INVALID_PHASE', lambda: self.append(s, phase_id='review'))
        with self.Session() as db:
            self.assertEqual([], self.logic.list_timeline(db, s))

    def test_room_project_mismatch_and_missing_ids(self):
        p, r, s = self.setup_business()
        other, _, _ = self.setup_business()
        self.assert_error('ROOM_PROJECT_MISMATCH', lambda: self.bind(other, r, s))
        self.assert_error('SESSION_NOT_FOUND', lambda: self.bind(p, r, uuid4().hex))

    def test_binding_cannot_be_silently_changed(self):
        p, r, s = self.setup_business()
        self.bind(p, r, s)
        self.bind(p, r, s)
        p2, r2, _ = self.setup_business()
        self.assert_error('BINDING_CONFLICT', lambda: self.bind(p2, r2, s))

    def test_versions_history_and_exact_time_boundary(self):
        p, r, s = self.setup_business(); self.bind(p, r, s)
        first = self.append(s)
        switch = datetime(2026, 10, 14, 2, 1, tzinfo=timezone.utc)
        second = self.append(s, expected_version=1, phase_id='REPORT', enabled_scenes=[], rule_versions={'S10':'s10-v1'}, effective_at=switch)
        with self.Session() as db:
            self.assertEqual([1,2], [x['phase_version'] for x in self.logic.list_timeline(db, s)])
            self.assertEqual(first, self.logic.get_snapshot(s, switch-timedelta(microseconds=1), db=db))
            self.assertEqual(second, self.logic.get_snapshot(s, switch, db=db))
            self.assertIsNone(self.logic.get_snapshot(s, datetime(2026,10,14,1,tzinfo=timezone.utc), db=db))

    def test_stale_version_and_backward_effective_time(self):
        p, r, s = self.setup_business(); self.bind(p, r, s); self.append(s)
        self.assert_error('VERSION_CONFLICT', lambda: self.append(s, expected_version=0))
        self.assert_error('TIME_CONFLICT', lambda: self.append(s, expected_version=1))

    def test_closed_session_rejects_writes(self):
        p, r, s = self.setup_business(); self.bind(p, r, s); self.append(s)
        with self.Session.begin() as db:
            db.scalar(select(self.EvalSession).where(self.EvalSession.session_id==s)).status='closed'
        self.assert_error('SESSION_CLOSED', lambda: self.append(s, expected_version=1))
        self.assert_error('SESSION_CLOSED', lambda: self.bind(p, r, s))

    def test_scene_versions_and_utc_validation(self):
        p, r, s = self.setup_business(); self.bind(p, r, s)
        for changes, code in (({'phase_id':'unknown'},'INVALID_PHASE'), ({'rule_versions':{'S03':'v1'}},'INVALID_CONFIG'), ({'expected_version':True},'INVALID_VERSION'), ({'effective_at':datetime(2026,10,14,2)},'INVALID_TIME'), ({'enabled_scenes':['NORMAL_READING']},'INVALID_CONFIG'), ({'assets':[{'slot':'video','source_id':'other-camera','asset_id':'a'}]},'SOURCE_MISMATCH')):
            with self.subTest(changes=changes): self.assert_error(code, lambda: self.append(s, **changes))

    def test_two_sessions_are_isolated(self):
        p, r, s = self.setup_business(); self.bind(p, r, s); self.append(s)
        p2, r2, s2 = self.setup_business(); self.bind(p2, r2, s2)
        self.append(s2, phase_id='DISCIPLINE', enabled_scenes=['S04'], rule_versions={'S04':'v1','S10':'v1'})
        with self.Session() as db:
            self.assertEqual('DETAIL', self.logic.get_latest_config(db,s)['phase_id'])
            self.assertEqual('DISCIPLINE', self.logic.get_latest_config(db,s2)['phase_id'])

    def test_concurrent_same_expected_version_only_one_wins(self):
        p, r, s = self.setup_business(); self.bind(p, r, s); self.append(s)
        def change(_):
            try:
                self.append(s, expected_version=1, effective_at=datetime(2026,10,14,2,1,tzinfo=timezone.utc))
                return 'ok'
            except self.logic.PhaseConfigError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(change,range(2)))
        self.assertCountEqual(['ok','VERSION_CONFLICT'], results)
        with self.Session() as db: self.assertEqual(2,len(self.logic.list_timeline(db,s)))

    def test_transaction_rollback_does_not_publish_snapshot(self):
        p, r, s = self.setup_business(); self.bind(p, r, s)
        with self.Session() as db:
            self.logic.append_snapshot(db,session_id=s,phase_id='SIGNIN',expected_version=0,enabled_scenes=[],rule_versions={'S10':'v1'},actor_id='test-admin',reason='Test rollback')
            db.rollback()
        with self.Session() as db: self.assertIsNone(self.logic.get_latest_config(db,s))

    def test_old_snapshot_cannot_be_edited_or_deleted_by_orm(self):
        p, r, s = self.setup_business(); self.bind(p, r, s); first=self.append(s)
        with self.Session() as db:
            row=db.get(self.Snapshot,first['stage_snapshot_id']); row.phase_id='SIGNIN'
            with self.assertRaises(ValueError): db.flush()
            db.rollback()
        with self.Session() as db:
            db.delete(db.get(self.Snapshot,first['stage_snapshot_id']))
            with self.assertRaises(ValueError): db.flush()
            db.rollback()
        with self.Session() as db: self.assertEqual(first,self.logic.get_latest_config(db,s))

    def test_api_config_matches_public_contract_and_audit_is_separate(self):
        p, r, s = self.setup_business(); self.bind(p, r, s); config=self.append(s)
        path=Path(__file__).resolve().parents[3] / 'tests/contracts/contract_validator.py'
        if not path.is_file():
            self.skipTest('当前代码分发不含独立契约测试资料')
        spec=importlib.util.spec_from_file_location('phase_contract_validator',path)
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        self.assertEqual([],module.ContractValidator().validate_object('phase-config',config))
        with self.Session() as db:
            history=self.logic.list_timeline(db,s)[0]
            self.assertEqual('test-admin',history['actor_id'])
            self.assertEqual('Explicit initial migration',history['reason'])

    def test_malformed_configuration_is_rejected_before_sql(self):
        p, r, s = self.setup_business(); self.bind(p, r, s)
        cases=[{'assets':{}},{'assets':[{'slot':'video','source_id':'camera-test','asset_id':'a','command':'shell'}]},
               {'exceptions':{}},{'exceptions':[{'fact_id':'f','fact_version':True}]},
               {'rule_versions':{'S03':'v1','S10':'v1','NOT_A_SCENE':'v1'}},
               {'enabled_scenes':[{}]}, {'sync_status':'applied'}]
        for changes in cases:
            with self.subTest(changes=changes):self.assert_error('INVALID_CONFIG',lambda:self.append(s,**changes))
        self.assert_error('INVALID_AUDIT',lambda:self.append(s,actor_id='a'*129))

    def test_disabled_room_and_inactive_binding_reject_writes(self):
        p,r,s=self.setup_business()
        with self.Session.begin() as db:db.get(self.Room,r).enabled=False
        self.assert_error('ROOM_DISABLED',lambda:self.bind(p,r,s))
        with self.Session.begin() as db:db.get(self.Room,r).enabled=True
        self.bind(p,r,s)
        with self.Session.begin() as db:db.scalar(select(self.Binding).where(self.Binding.session_id==s)).active=False
        self.assert_error('BINDING_INACTIVE',lambda:self.append(s))

    def test_json_inputs_and_read_results_cannot_mutate_history(self):
        p,r,s=self.setup_business();self.bind(p,r,s)
        assets=[{'slot':'video','source_id':'camera-test','asset_id':'asset-1'}]
        first=self.append(s,assets=assets)
        assets[0]['asset_id']='edited-after-write'
        first['assets'][0]['asset_id']='edited-return'
        with self.Session() as db:
            value=self.logic.get_latest_config(db,s)
            value['assets'][0]['asset_id']='edited-read'
            self.assertEqual('asset-1',self.logic.get_latest_config(db,s)['assets'][0]['asset_id'])

    def test_cached_session_cannot_bypass_closed_state(self):
        p,r,s=self.setup_business();self.bind(p,r,s)
        with self.Session() as db:
            cached=db.scalar(select(self.EvalSession).where(self.EvalSession.session_id==s))
            self.assertEqual('running',cached.status)
            with self.Session.begin() as other:other.scalar(select(self.EvalSession).where(self.EvalSession.session_id==s)).status='closed'
            self.assert_error('SESSION_CLOSED',lambda:self.logic.append_snapshot(db,session_id=s,phase_id='SIGNIN',expected_version=0,enabled_scenes=[],rule_versions={'S10':'v1'},actor_id='test',reason='cached status'))

    def test_read_missing_session_does_not_silently_return_empty(self):
        missing=uuid4().hex
        with self.Session() as db:
            self.assert_error('SESSION_NOT_FOUND',lambda:self.logic.get_latest_config(db,missing))
            self.assert_error('SESSION_NOT_FOUND',lambda:self.logic.list_timeline(db,missing))

    def api_bind(self,p,r,s):
        return self.client.put(f'/api/sessions/{s}/binding',json={'project_id':p,'room_id':r,'source_id':'camera-test'})

    def api_config(self,s,**changes):
        body={'phase':'DETAIL','expected_version':0,'enabled_scenes':['S03'],
            'rule_versions':{'S03':'s03-v1','S10':'s10-v1'},'reason':'Explicit first configuration',
            'effective_at':'2026-10-14T02:00:00Z','assets':[],'exceptions':[]}
        body.update(changes)
        return self.client.put(f'/api/sessions/{s}/phase-config',json=body)

    def test_api_unconfigured_read_is_explicit_and_has_no_side_effects(self):
        p,r,s=self.setup_business()
        response=self.client.get(f'/api/sessions/{s}/phase-config')
        self.assertEqual(409,response.status_code,response.text)
        self.assertEqual('PHASE_NOT_CONFIGURED',response.json()['detail']['code'])
        self.assertEqual([],self.client.get(f'/api/sessions/{s}/timeline').json()['items'])
        response=self.api_config(s)
        self.assertEqual(409,response.status_code,response.text)
        self.assertEqual('BINDING_REQUIRED',response.json()['detail']['code'])
        with self.Session() as db:
            self.assertEqual('review',db.scalar(select(self.EvalSession).where(self.EvalSession.session_id==s)).current_phase)

    def test_api_bind_save_read_and_audit(self):
        p,r,s=self.setup_business()
        binding=self.api_bind(p,r,s)
        self.assertEqual(200,binding.status_code,binding.text)
        self.assertEqual(binding.json(),self.api_bind(p,r,s).json())
        response=self.api_config(s)
        self.assertEqual(201,response.status_code,response.text)
        self.assertEqual(1,response.json()['phase_version'])
        self.assertEqual(['S10'],response.json()['always_on_scenes'])
        self.assertEqual('pending',response.json()['sync_status'])
        self.assertEqual(response.json(),self.client.get(f'/api/sessions/{s}/phase-config').json())
        history=self.client.get(f'/api/sessions/{s}/timeline').json()['items']
        self.assertEqual('test_admin',history[0]['actor_id'])
        self.assertEqual('Explicit first configuration',history[0]['reason'])
        self.assertEqual('DETAIL',self.client.get(f'/api/sessions/{s}').json()['current_phase'])

    def test_api_patch_conflict_and_history(self):
        p,r,s=self.setup_business();self.api_bind(p,r,s);self.api_config(s)
        body={'phase':'REPORT','expected_version':0,'reason':'Switch to report',
            'effective_at':'2026-10-14T02:01:00Z','enabled_scenes':[],'rule_versions':{'S10':'s10-v1'}}
        response=self.client.patch(f'/api/sessions/{s}/phase',json=body)
        self.assertEqual(409,response.status_code,response.text)
        self.assertEqual('VERSION_CONFLICT',response.json()['detail']['code'])
        body['expected_version']=1
        response=self.client.patch(f'/api/sessions/{s}/phase',json=body)
        self.assertEqual(201,response.status_code,response.text)
        self.assertEqual(2,response.json()['phase_version'])
        self.assertEqual([1,2],[i['phase_version'] for i in self.client.get(f'/api/sessions/{s}/timeline').json()['items']])
        with self.Session() as db:
            rows=db.scalars(select(self.Outbox).where(self.Outbox.session_id==s).order_by(self.Outbox.phase_version)).all()
            self.assertEqual([1,2],[row.phase_version for row in rows])
            self.assertEqual(response.json()['stage_snapshot_id'],rows[-1].stage_snapshot_id)

    def test_api_bad_input_and_forged_actor_rejected_without_writes(self):
        p,r,s=self.setup_business();self.api_bind(p,r,s)
        for changes in ({'phase':'review'},{'expected_version':True},{'expected_version':'0'},
                        {'enabled_scenes':['NORMAL_READING']},{'actor_id':'forged'},
                        {'effective_at':'2026-10-14T10:00:00+08:00'},{'reason':'   '},
                        {'always_on_scenes':[]},{'sync_status':'applied'},{'rule_versions':{'S03':'v1'}}):
            with self.subTest(changes=changes):
                response=self.api_config(s,**changes)
                self.assertEqual(422,response.status_code,response.text)
        response=self.client.patch(f'/api/sessions/{s}/phase',json={'phase':'DISCIPLINE'})
        self.assertEqual(422,response.status_code,response.text)
        self.assertEqual([],self.client.get(f'/api/sessions/{s}/timeline').json()['items'])

    def test_api_authorization_csrf_and_origin(self):
        p,r,s=self.setup_business()
        with TestClient(self.app) as anonymous:
            self.assertEqual(401,anonymous.get(f'/api/sessions/{s}/phase-config').status_code)
            self.assertEqual(401,anonymous.put(f'/api/sessions/{s}/binding',json={}).status_code)
        body={'project_id':p,'room_id':r,'source_id':'camera-test'}
        response=self.client.put(f'/api/sessions/{s}/binding',json=body,headers={'X-CSRF-Token':'wrong'})
        self.assertEqual(403,response.status_code,response.text)
        response=self.client.put(f'/api/sessions/{s}/binding',json=body,headers={'Origin':'http://other-origin'})
        self.assertEqual(403,response.status_code,response.text)

    def test_api_not_found_mismatch_and_closed(self):
        p,r,s=self.setup_business();p2,r2,s2=self.setup_business()
        response=self.api_bind(p2,r,s)
        self.assertEqual(422,response.status_code,response.text)
        self.assertEqual(404,self.client.get(f'/api/sessions/{uuid4().hex}/phase-config').status_code)
        self.api_bind(p,r,s);self.api_config(s)
        with self.Session.begin() as db:
            db.scalar(select(self.EvalSession).where(self.EvalSession.session_id==s)).status='closed'
        response=self.api_config(s,expected_version=1,effective_at='2026-10-14T02:01:00Z')
        self.assertEqual(409,response.status_code,response.text)
        self.assertEqual('SESSION_CLOSED',response.json()['detail']['code'])
        response=self.api_bind(p,r,s)
        self.assertEqual(409,response.status_code,response.text)

    def test_api_write_failure_rolls_back_config_and_session_phase(self):
        p,r,s=self.setup_business();self.api_bind(p,r,s)
        with patch('app.routers.sessions.DbSession.commit',side_effect=OperationalError(
                'private-sql-sentinel',{},RuntimeError('private-connection-sentinel'))):
            response=self.api_config(s)
        self.assertEqual(503,response.status_code,response.text)
        self.assertEqual('3',response.headers['retry-after'])
        self.assertNotIn('private-',response.text)
        with self.Session() as db:
            self.assertIsNone(self.logic.get_latest_config(db,s))
            self.assertEqual('review',db.scalar(select(self.EvalSession).where(self.EvalSession.session_id==s)).current_phase)
        self.assertEqual(201,self.api_config(s).status_code)

    def test_api_closed_session_cannot_reopen_via_start(self):
        p,r,s=self.setup_business();self.api_bind(p,r,s);self.api_config(s)
        response=self.client.post(f'/api/sessions/{s}/stop')
        self.assertEqual(200,response.status_code,response.text)
        response=self.client.post(f'/api/sessions/{s}/start')
        self.assertEqual(409,response.status_code,response.text)
        self.assertEqual('closed',self.client.get(f'/api/sessions/{s}').json()['status'])

    def test_after_binding_disabled_business_context_cannot_append(self):
        for closed_project in (False,True):
            p,r,s=self.setup_business();self.bind(p,r,s)
            with self.Session.begin() as db:
                if closed_project:db.get(self.Project,p).status='closed'
                else:db.get(self.Room,r).enabled=False
            self.assert_error('PROJECT_CLOSED' if closed_project else 'ROOM_DISABLED',lambda:self.append(s))

    def test_api_conflicting_requests_and_failed_writes_leave_no_extra_history(self):
        p,r,s=self.setup_business();self.api_bind(p,r,s);self.api_config(s)
        p2,r2,_=self.setup_business()
        self.assertEqual(409,self.api_bind(p2,r2,s).status_code)
        body={'phase':'REPORT','expected_version':1,'enabled_scenes':[],'rule_versions':{'S10':'s10-v1'},
            'effective_at':'2026-10-14T02:01:00Z','reason':'Concurrent API phase change'}
        def call(_):
            with TestClient(self.app,headers=dict(self.client.headers),cookies=dict(self.client.cookies)) as client:
                return client.patch(f'/api/sessions/{s}/phase',json=body).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:statuses=list(pool.map(call,range(2)))
        self.assertCountEqual([201,409],statuses)
        history=self.client.get(f'/api/sessions/{s}/timeline').json()['items']
        self.assertEqual([1,2],[v['phase_version'] for v in history])
        with self.Session() as db:
            self.assertEqual(2,len(db.scalars(select(self.Outbox).where(self.Outbox.session_id==s)).all()))

    def test_api_all_six_phases_keep_s10_and_disable_cache(self):
        p,r,s=self.setup_business();self.api_bind(p,r,s)
        start=datetime(2026,10,14,2,tzinfo=timezone.utc)
        for version,phase in enumerate(self.logic.PHASES):
            response=self.api_config(s,phase=phase,expected_version=version,
                effective_at=(start+timedelta(minutes=version)).isoformat(),enabled_scenes=[],rule_versions={'S10':'v1'})
            self.assertEqual(201,response.status_code,response.text)
            self.assertEqual(version+1,response.json()['phase_version'])
            self.assertEqual(['S10'],response.json()['always_on_scenes'])
            self.assertEqual('no-store',response.headers['cache-control'])
        current=self.client.get(f'/api/sessions/{s}/phase-config')
        self.assertEqual('no-store',current.headers['cache-control'])
        self.assertEqual(6,len(self.client.get(f'/api/sessions/{s}/timeline').json()['items']))

    def test_api_saves_snapshot_phase_and_notification_in_one_transaction(self):
        p,r,s=self.setup_business();self.api_bind(p,r,s)
        response=self.api_config(s)
        self.assertEqual(201,response.status_code,response.text)
        snapshot_id=response.json()['stage_snapshot_id']
        with self.Session() as db:
            row=db.scalar(select(self.Outbox).where(self.Outbox.stage_snapshot_id==snapshot_id))
            self.assertIsNotNone(row,'保存成功必须有持久控制通知')
            self.assertEqual(s,row.session_id)
            self.assertEqual(['event-dispatcher','media-controller'],row.required_consumers)
            self.assertEqual('pending',row.delivery_status)
            self.assertEqual(0,row.attempts)
            self.assertEqual('pending',self.Control.get_control_status(db,s)['sync_status'])
        response=self.api_config(s,expected_version=0)
        self.assertEqual(409,response.status_code,response.text)
        with self.Session() as db:
            self.assertEqual(1,len(db.scalars(select(self.Outbox).where(self.Outbox.session_id==s)).all()))

    def test_outbox_insert_failure_rolls_back_all_phase_changes(self):
        p,r,s=self.setup_business();self.api_bind(p,r,s)
        with patch.object(self.Control,'enqueue_snapshot',side_effect=OperationalError('secret SQL',{},RuntimeError('secret connection'))):
            response=self.api_config(s)
        self.assertEqual(503,response.status_code,response.text)
        self.assertNotIn('secret',response.text)
        with self.Session() as db:
            self.assertIsNone(self.logic.get_latest_config(db,s))
            self.assertEqual([],db.scalars(select(self.Outbox).where(self.Outbox.session_id==s)).all())
            self.assertEqual('review',db.scalar(select(self.EvalSession).where(self.EvalSession.session_id==s)).current_phase)
        self.assertEqual(201,self.api_config(s).status_code)

    def test_commit_failure_rolls_back_already_flushed_outbox(self):
        p,r,s=self.setup_business();self.api_bind(p,r,s)
        with patch('app.routers.sessions.DbSession.commit',side_effect=OperationalError('private SQL',{},RuntimeError('private'))):
            response=self.api_config(s)
        self.assertEqual(503,response.status_code,response.text)
        with self.Session() as db:
            self.assertIsNone(self.logic.get_latest_config(db,s))
            self.assertEqual([],db.scalars(select(self.Outbox).where(self.Outbox.session_id==s)).all())

    def test_server_consumers_cannot_be_shortened_by_request_body(self):
        p,r,s=self.setup_business();self.api_bind(p,r,s)
        response=self.api_config(s,required_consumers=['media-controller'])
        self.assertEqual(422,response.status_code,response.text)
        self.app.state.phase_control_required_consumers=['media-controller','event-dispatcher','vision-worker']
        response=self.api_config(s)
        self.assertEqual(201,response.status_code,response.text)
        with self.Session() as db:
            row=db.scalar(select(self.Outbox).where(self.Outbox.session_id==s))
            self.assertEqual(['event-dispatcher','media-controller','vision-worker'],row.required_consumers)

    def test_unconfigured_consumer_set_does_not_save_fake_applied_config(self):
        p,r,s=self.setup_business();self.api_bind(p,r,s)
        for consumers in (None,[],['same','same'],[{}],['  ']):
            self.app.state.phase_control_required_consumers=consumers
            with self.subTest(consumers=consumers):
                response=self.api_config(s)
                self.assertEqual(503,response.status_code,response.text)
                self.assertEqual('CONTROL_CONSUMERS_NOT_CONFIGURED',response.json()['detail']['code'])
        with self.Session() as db:self.assertIsNone(self.logic.get_latest_config(db,s))

    def test_phase_write_does_not_connect_to_redis_or_run_publisher(self):
        p,r,s=self.setup_business();self.api_bind(p,r,s)
        with patch.object(self.Control,'publish_pending',side_effect=AssertionError('不得在HTTP事务中发布')):
            response=self.api_config(s)
        self.assertEqual(201,response.status_code,response.text)
        with self.Session() as db:
            row=db.scalar(select(self.Outbox).where(self.Outbox.session_id==s))
            self.assertIsNotNone(row)
            self.assertEqual('pending',row.delivery_status)

    def test_control_enqueue_configuration_error_rolls_back_as_503(self):
        p,r,s=self.setup_business();self.api_bind(p,r,s)
        with patch.object(self.Control,'enqueue_snapshot',side_effect=self.Control.PhaseControlError('INVALID_CONSUMERS','private config sentinel')):
            response=self.api_config(s)
        self.assertEqual(503,response.status_code,response.text)
        self.assertNotIn('private',response.text)
        with self.Session() as db:self.assertIsNone(self.logic.get_latest_config(db,s))


def run_isolated_pg():
    """只创建/清理本次独立PG；不读业务容器配置或使用业务卷。"""
    name = 'poc-phase-test-' + uuid4().hex[:12]
    password = uuid4().hex
    started = False
    old_url = os.environ.get('PHASE_TEST_DATABASE_URL')
    try:
        result = subprocess.run(['docker', 'run', '-d', '--name', name,
            '--tmpfs', '/var/lib/postgresql/data', '-e', 'POSTGRES_USER=phase_test',
            '-e', 'POSTGRES_PASSWORD=' + password, '-e', 'POSTGRES_DB=phase_test',
            '-p', '127.0.0.1::5432', 'postgres:16-alpine'], capture_output=True)
        if result.returncode:
            raise RuntimeError('独立PG启动失败；需Docker及postgres:16-alpine镜像')
        started = True
        deadline = time.monotonic() + 60
        while subprocess.run(['docker', 'exec', name, 'pg_isready', '-U', 'phase_test', '-d', 'phase_test'], capture_output=True).returncode:
            if time.monotonic() >= deadline:
                raise RuntimeError('独立PG等待超时')
            time.sleep(0.5)
        port = subprocess.check_output(['docker', 'port', name, '5432/tcp']).decode().strip().split(':')[-1]
        os.environ['PHASE_TEST_DATABASE_URL'] = f'postgresql+psycopg://phase_test:{password}@127.0.0.1:{port}/phase_test'
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        result = unittest.main(argv=[sys.argv[0], '-v'], exit=False)
        return result.result.wasSuccessful()
    finally:
        if started:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, check=True)
        if old_url is None:
            os.environ.pop('PHASE_TEST_DATABASE_URL', None)
        else:
            os.environ['PHASE_TEST_DATABASE_URL'] = old_url


if __name__ == '__main__':
    if '--isolated-pg' in sys.argv:
        sys.exit(0 if run_isolated_pg() else 1)
    else:
        unittest.main()
