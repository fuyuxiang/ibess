"""2.2真实PG/Redis隔离验证；不向业务流或正式数据库写测试消息。"""
import copy
import hashlib
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
import secrets
from pathlib import Path
import threading
import tempfile
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import redis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker


NOW = datetime(2026, 10, 14, 2, tzinfo=timezone.utc)


class PhaseControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root=Path(__file__).resolve().parents[1]
        if not all((root/f'app/{name}.py').exists() for name in ('phase_control','phase_control_models')):
            return
        from app import phase_control, phase_config
        from app.models import Base, EvalSession
        from app.business_models import BusinessProject, BusinessRoom
        from app.phase_models import PhaseBinding, PhaseSnapshot
        from app.phase_control_models import PhaseControlOutbox, PhaseControlAck
        from app.auth import AdminAuthMiddleware
        from app.auth_models import AdminAccount, AdminAudit, AdminSession
        from app.db import get_session
        cls.logic,cls.config=phase_control,phase_config
        cls.Project,cls.Room,cls.EvalSession=BusinessProject,BusinessRoom,EvalSession
        cls.Outbox,cls.Ack,cls.Snapshot=PhaseControlOutbox,PhaseControlAck,PhaseSnapshot
        cls.AuthSession=AdminSession
        url=os.environ.get('PHASE_TEST_DATABASE_URL')
        redis_url=os.environ.get('PHASE_TEST_REDIS_URL')
        if not url or not redis_url:raise RuntimeError('必须显式提供隔离PG/Redis连接')
        cls.admin=create_engine(url)
        if cls.admin.dialect.name!='postgresql':raise RuntimeError('必须使用真实PostgreSQL')
        cls.schema='test_phase_control_'+uuid4().hex
        with cls.admin.begin() as connection:connection.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        try:
            cls.engine=create_engine(url,connect_args={'options':f'-csearch_path={cls.schema} -clock_timeout=5000 -cstatement_timeout=15000'})
            Base.metadata.create_all(cls.engine,tables=[m.__table__ for m in
                (BusinessProject,BusinessRoom,EvalSession,PhaseBinding,PhaseSnapshot,PhaseControlOutbox,PhaseControlAck,AdminAccount,AdminAudit,AdminSession)])
            cls.Session=sessionmaker(bind=cls.engine)
            cls.app=FastAPI()
            cls.app.add_middleware(AdminAuthMiddleware,session_factory=cls.Session)
            router_path=root/'app/routers/phase_control.py'
            if router_path.exists():
                from app.routers.phase_control import router
                cls.app.include_router(router)
            def override_db():
                with cls.Session() as db:yield db
            cls.app.dependency_overrides[get_session]=override_db
            cls.redis=redis.from_url(redis_url,decode_responses=True,socket_connect_timeout=1,socket_timeout=1)
            cls.redis.ping()
        except Exception:
            cls.tearDownClass();raise

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls,'redis'):cls.redis.close()
        if hasattr(cls,'engine'):cls.engine.dispose()
        if hasattr(cls,'schema'):
            with cls.admin.begin() as connection:connection.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
            cls.admin.dispose()

    def setUp(self):
        root=Path(__file__).resolve().parents[1]
        self.assertTrue((root/'app/phase_control.py').exists(),'2.2控制模块尚未实现')
        self.assertTrue((root/'app/phase_control_models.py').exists(),'2.2持久记录尚未实现')
        # 所有表仅在本测试的随机schema；避免前一测试遗留pending通知影响发布计数。
        with self.engine.begin() as connection:
            connection.execute(text('TRUNCATE admin_sessions, admin_audit, admin_accounts, phase_control_acks, phase_control_outbox, phase_snapshots, phase_bindings, eval_sessions, business_rooms, business_projects RESTART IDENTITY CASCADE'))
        from app.auth_models import AdminAccount
        token=secrets.token_urlsafe(32);csrf=secrets.token_hex(32)
        with self.Session.begin() as db:
            db.add(AdminAccount(account_id=1,username='test_admin',display_name='Test',password_hash='not-a-usable-password'));db.flush()
            db.add(self.AuthSession(token_hash=hashlib.sha256(token.encode('ascii')).hexdigest(),account_id=1,
                csrf_token=csrf,expires_at=datetime.now(timezone.utc)+timedelta(hours=1)))
        self.client=TestClient(self.app,headers={'X-CSRF-Token':csrf,'X-Requested-With':'XMLHttpRequest'})
        self.client.cookies.set('eval_admin_session',token)
        self.stream='test:phase-control:'+uuid4().hex
        self.p,self.r,self.s=uuid4().hex,uuid4().hex,uuid4().hex
        with self.Session.begin() as db:
            db.add(self.Project(project_id=self.p,code=self.p,name='Constructed',owner='Test',status='running'));db.flush()
            db.add(self.Room(room_id=self.r,project_id=self.p,code=self.r,name='Constructed',location='Test',enabled=True))
            db.add(self.EvalSession(session_id=self.s,name='Constructed',status='running',current_phase='review'));db.flush()
            self.config.bind_session(db,session_id=self.s,project_id=self.p,room_id=self.r,source_id='camera-test')
        self.snapshot=self.create_snapshot()
        self.consumer_token=secrets.token_urlsafe(32)
        self.app.state.phase_control_consumers={'media-controller':{
            'token_sha256':hashlib.sha256(self.consumer_token.encode('ascii')).hexdigest(),
            'scopes':[{'session_id':self.s,'source_id':'camera-test','project_id':self.p,'room_id':self.r}]}}

    def tearDown(self):
        if hasattr(self,'stream'):self.redis.delete(self.stream)
        if hasattr(self,'client'):self.client.close()

    def create_snapshot(self,version=0):
        with self.Session.begin() as db:
            return self.config.append_snapshot(db,session_id=self.s,phase_id='DETAIL',expected_version=version,
                enabled_scenes=['S03'],rule_versions={'S03':'v1','S10':'v1'},actor_id='test_admin',reason='Constructed control',effective_at=NOW+timedelta(minutes=version))

    def enqueue(self,consumers=None,snapshot=None):
        with self.Session.begin() as db:
            return self.logic.enqueue_snapshot(db,(snapshot or self.snapshot)['stage_snapshot_id'],
                required_consumers=consumers or ['media-controller','event-dispatcher'])

    def publish(self,publisher=None,now=NOW):
        return self.logic.publish_pending(self.Session,publisher or self.redis,stream=self.stream,now=now)

    def ack(self,consumer='media-controller',status='applied',snapshot=None,received=None):
        value=snapshot or self.snapshot
        received=received or NOW+timedelta(seconds=1)
        return {k:value[k] for k in ('contract_version','project_id','room_id','session_id','source_id','stage_snapshot_id','phase_id','phase_version')} | {
            'consumer_id':consumer,'received_at':received.isoformat().replace('+00:00','Z'),
            'applied_at':(received+timedelta(seconds=1)).isoformat().replace('+00:00','Z') if status=='applied' else None,
            'status':status,'error':None if status=='applied' else {'code':'ASSET_MISSING' if status=='missing_asset' else 'APPLY_FAILED','message':'脱敏错误示例'}}

    def record(self,value):
        with self.Session.begin() as db:return self.logic.record_ack(db,value)

    def state(self):
        with self.Session() as db:return self.logic.get_control_status(db,self.s)

    def assert_error(self,code,action):
        with self.assertRaises(self.logic.PhaseControlError) as exc:action()
        self.assertEqual(code,exc.exception.code)

    def test_enqueue_idempotent_without_marking_applied(self):
        first=self.enqueue();self.assertEqual(first,self.enqueue())
        self.assertEqual('pending',self.state()['sync_status'])
        self.assertEqual('pending',self.state()['delivery_status'])
        with self.Session() as db:self.assertEqual(1,len(db.scalars(select(self.Outbox).where(self.Outbox.stage_snapshot_id==self.snapshot['stage_snapshot_id'])).all()))

    def test_same_transaction_rollback_removes_outbox_and_snapshot(self):
        with self.Session() as db:
            snapshot=self.config.append_snapshot(db,session_id=self.s,phase_id='REPORT',expected_version=1,enabled_scenes=[],rule_versions={'S10':'v1'},actor_id='test',reason='rollback',effective_at=NOW+timedelta(minutes=1))
            self.logic.enqueue_snapshot(db,snapshot['stage_snapshot_id'],required_consumers=['media-controller'])
            identifier=snapshot['stage_snapshot_id'];db.rollback()
        with self.Session() as db:
            self.assertIsNone(db.get(self.Snapshot,identifier))
            self.assertIsNone(db.scalar(select(self.Outbox).where(self.Outbox.stage_snapshot_id==identifier)))

    def test_real_redis_publish_then_no_duplicate_publish(self):
        identifier=self.enqueue();self.assertEqual(1,self.publish())
        messages=self.redis.xrange(self.stream)
        self.assertEqual(1,len(messages));message=messages[0][1]
        self.assertEqual(identifier,message['notification_id'])
        self.assertEqual(self.s,message['session_id'])
        self.assertEqual(self.snapshot,json.loads(message['payload']))
        self.assertEqual(0,self.publish());self.assertEqual('published',self.state()['delivery_status'])
        self.assertEqual('pending',self.state()['sync_status'])

    def test_redis_failure_and_recovery_preserve_stable_notification(self):
        identifier=self.enqueue()
        class Offline:
            def xadd(self,*args,**kwargs):raise redis.ConnectionError('secret-connection-string')
        self.assertEqual(0,self.publish(Offline()))
        state=self.state();self.assertEqual('pending',state['sync_status']);self.assertEqual('pending',state['delivery_status'])
        self.assertNotIn('secret',json.dumps(state));self.assertEqual(0,self.publish(now=NOW))
        self.assertEqual(1,self.publish(now=NOW+timedelta(seconds=30)))
        self.assertEqual(identifier,self.redis.xrange(self.stream)[0][1]['notification_id'])

    def test_one_ack_not_enough_and_restart_uses_database(self):
        self.enqueue();self.publish();self.record(self.ack())
        self.assertEqual('partially_applied',self.state()['sync_status'])
        self.redis.delete(self.stream)
        with self.Session() as db:
            config=self.logic.read_latest_config(db,self.s)
        self.assertEqual(self.snapshot,config)
        self.record(self.ack('event-dispatcher'))
        self.assertEqual('applied',self.state()['sync_status'])
        with self.Session() as db:self.assertEqual('pending',db.get(self.Snapshot,self.snapshot['stage_snapshot_id']).sync_status)

    def test_missing_asset_and_recovery(self):
        self.enqueue();self.record(self.ack(status='missing_asset'))
        self.assertEqual('missing_asset',self.state()['sync_status'])
        self.record(self.ack('event-dispatcher'));self.record(self.ack(received=NOW+timedelta(seconds=5)))
        self.assertEqual('applied',self.state()['sync_status'])

    def test_old_version_ack_never_applies_new_version(self):
        self.enqueue();new=self.create_snapshot(version=1);self.enqueue(snapshot=new)
        self.record(self.ack());self.record(self.ack('event-dispatcher'))
        state=self.state();self.assertEqual(2,state['phase_version']);self.assertEqual('pending',state['sync_status'])

    def test_ack_dedupe_and_late_failure_cannot_regress_success(self):
        self.enqueue();value=self.ack();self.assertEqual(value,self.record(value));self.record(value)
        self.record(self.ack(status='failed',received=NOW))
        self.assertEqual('partially_applied',self.state()['sync_status'])
        with self.Session() as db:self.assertEqual(1,len(db.scalars(select(self.Ack).where(self.Ack.stage_snapshot_id==self.snapshot['stage_snapshot_id'])).all()))

    def test_ack_scope_version_consumer_and_times_rejected(self):
        self.enqueue()
        for field,value,code in [('session_id','other','ACK_SCOPE_MISMATCH'),('source_id','other','ACK_SCOPE_MISMATCH'),
            ('phase_version',2,'ACK_SCOPE_MISMATCH'),('stage_snapshot_id','missing','SNAPSHOT_NOT_FOUND'),
            ('consumer_id','unexpected','CONSUMER_NOT_REQUIRED'),('received_at',123,'INVALID_ACK'),
            ('applied_at','2026-10-14T01:00:00Z','INVALID_ACK')]:
            with self.subTest(field=field):self.assert_error(code,lambda:self.record(self.ack()|{field:value}))

    def test_consumer_set_cannot_be_changed_or_empty(self):
        self.enqueue()
        self.assert_error('CONTROL_CONFLICT',lambda:self.enqueue(['media-controller']))
        with self.Session() as db:
            self.assert_error('INVALID_CONSUMERS',lambda:self.logic.enqueue_snapshot(db,self.snapshot['stage_snapshot_id'],required_consumers=[]))

    def test_concurrent_publishers_do_not_publish_same_locked_row(self):
        self.enqueue()
        with ThreadPoolExecutor(max_workers=2) as pool:counts=list(pool.map(lambda _:self.publish(),range(2)))
        self.assertEqual(1,sum(counts));self.assertEqual(1,self.redis.xlen(self.stream))

    def test_redis_accepted_then_connection_lost_replays_same_message_id(self):
        identifier=self.enqueue();client=self.redis
        class AmbiguousDelivery:
            def xadd(self,*args,**kwargs):
                client.xadd(*args,**kwargs)
                raise redis.ConnectionError('reply lost after accepting message')
        self.assertEqual(0,self.publish(AmbiguousDelivery()))
        self.assertEqual(1,self.publish(now=NOW+timedelta(seconds=30)))
        self.assertEqual(2,self.redis.xlen(self.stream))
        self.assertEqual({identifier},{fields['notification_id'] for _,fields in self.redis.xrange(self.stream)})

    def test_real_redis_pause_and_resume(self):
        container=os.environ.get('PHASE_TEST_REDIS_CONTAINER')
        if not container or not container.startswith('poc-control-test-'):self.skipTest('仅独立服务入口操作专属Redis容器')
        self.enqueue()
        subprocess.run(['docker','pause',container],capture_output=True,check=True)
        try:self.assertEqual(0,self.publish())
        finally:subprocess.run(['docker','unpause',container],capture_output=True,check=True)
        self.assertEqual('pending',self.state()['delivery_status'])
        self.assertEqual(1,self.publish(now=NOW+timedelta(seconds=30)))

    def test_ack_concurrency_dedupe_and_equal_time_conflict(self):
        self.enqueue();value=self.ack()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:self.record(copy.deepcopy(value)),range(2)))
        self.assertEqual([value,value],results)
        self.assert_error('ACK_CONFLICT',lambda:self.record(self.ack(status='failed')))
        with self.Session() as db:self.assertEqual(1,len(db.scalars(select(self.Ack)).all()))

    def test_error_text_is_sanitized_and_ack_payload_is_not_mutated(self):
        self.enqueue();value=self.ack(status='failed');value['error']={'code':'APPLY_FAILED','message':'secret-path-token'}
        original=copy.deepcopy(value);stored=self.record(value)
        self.assertEqual(original,value)
        self.assertNotIn('secret',json.dumps(stored))
        path=Path(__file__).resolve().parents[3]/'tests/contracts/contract_validator.py'
        if not path.is_file():
            self.skipTest('当前代码分发不含独立契约测试资料')
        spec=importlib.util.spec_from_file_location('ack_contract_validator',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertEqual([],module.ContractValidator().validate_object('control-ack',stored))

    def test_malformed_ack_and_consumer_definitions_fail_safely(self):
        self.enqueue()
        for bad in [True,'1',1.5]:self.assert_error('INVALID_ACK',lambda:self.record(self.ack()|{'phase_version':bad}))
        for bad in [[],['same','same'],[{}],['  '],['a'*129]]:
            with self.Session() as db:self.assert_error('INVALID_CONSUMERS',lambda:self.logic.enqueue_snapshot(db,self.snapshot['stage_snapshot_id'],required_consumers=bad))
        for error in [{'code':'e','message':'m','stack':'secret'}, {'code':'e','message':4}]:
            self.assert_error('INVALID_ACK',lambda:self.record(self.ack(status='failed')|{'error':error}))

    def test_consumers_can_recover_even_after_losing_entire_redis_stream(self):
        self.enqueue();self.publish();self.redis.delete(self.stream)
        with self.Session() as db:recovered=self.logic.read_latest_config(db,self.s)
        self.assertEqual(self.snapshot,recovered)
        self.assertEqual('pending',self.state()['sync_status'])
        self.record(self.ack());self.record(self.ack('event-dispatcher'))
        self.assertEqual('applied',self.state()['sync_status'])

    def test_cross_session_ack_does_not_change_either_control(self):
        self.enqueue()
        forged=self.ack();forged['session_id']=uuid4().hex
        self.assert_error('ACK_SCOPE_MISMATCH',lambda:self.record(forged))
        self.assertEqual('pending',self.state()['sync_status'])
        with self.Session() as db:self.assertEqual(0,len(db.scalars(select(self.Ack)).all()))

    def test_duplicate_enqueue_concurrent_keeps_one_notification(self):
        with ThreadPoolExecutor(max_workers=2) as pool:identifiers=list(pool.map(lambda _:self.enqueue(),range(2)))
        self.assertEqual(identifiers[0],identifiers[1])
        with self.Session() as db:self.assertEqual(1,len(db.scalars(select(self.Outbox)).all()))

    def internal(self,s=None):
        return f'/internal/phase-control/sessions/{s or self.s}'

    def consumer_headers(self):
        return {'Authorization':'Bearer '+self.consumer_token}

    def test_status_api_admin_only_and_no_cache(self):
        self.enqueue()
        url=f'/api/sessions/{self.s}/control-status'
        with TestClient(self.app) as anonymous:self.assertEqual(401,anonymous.get(url).status_code)
        response=self.client.get(url)
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual('no-store',response.headers['cache-control'])
        self.assertEqual('pending',response.json()['sync_status'])
        self.record(self.ack())
        self.assertEqual('partially_applied',self.client.get(url).json()['sync_status'])
        self.record(self.ack('event-dispatcher'))
        self.assertEqual('applied',self.client.get(url).json()['sync_status'])

    def test_internal_latest_uses_scoped_bearer_not_admin_cookie(self):
        self.enqueue()
        url=self.internal()+'/config'
        self.assertEqual(401,self.client.get(url).status_code)
        with TestClient(self.app) as consumer:
            response=consumer.get(url,headers=self.consumer_headers())
            self.assertEqual(200,response.status_code,response.text)
            self.assertEqual(self.snapshot,response.json())
            self.assertEqual('no-store',response.headers['cache-control'])
            self.assertEqual(401,consumer.get(url,headers={'Authorization':'Bearer wrong'}).status_code)
            self.assertEqual(403,consumer.get(self.internal(uuid4().hex)+'/config',headers=self.consumer_headers()).status_code)

    def test_ack_api_put_deduplicates_and_cannot_claim_other_consumer(self):
        self.enqueue()
        with TestClient(self.app) as consumer:
            url=self.internal()+'/acks/media-controller'
            value=self.ack()
            response=consumer.put(url,json=value,headers=self.consumer_headers())
            self.assertEqual(200,response.status_code,response.text)
            self.assertEqual(value,response.json())
            self.assertEqual(value,consumer.put(url,json=value,headers=self.consumer_headers()).json())
            response=consumer.put(self.internal()+'/acks/event-dispatcher',json=self.ack('event-dispatcher'),headers=self.consumer_headers())
            self.assertEqual(403,response.status_code,response.text)
            response=consumer.put(url,json=self.ack('event-dispatcher'),headers=self.consumer_headers())
            self.assertEqual(403,response.status_code,response.text)
        self.assertEqual('partially_applied',self.state()['sync_status'])

    def test_ack_api_malformed_scope_time_extra_fields(self):
        self.enqueue();url=self.internal()+'/acks/media-controller'
        with TestClient(self.app) as consumer:
            for changes in ({'phase_version':True},{'phase_version':'1'},{'status':'normal'},
                            {'received_at':123},{'applied_at':'2026-10-14T01:00:00Z'},
                            {'actor_id':'forged'},{'error':{'code':'e','message':'m'}}):
                with self.subTest(changes=changes):
                    response=consumer.put(url,json=self.ack()|changes,headers=self.consumer_headers())
                    self.assertEqual(422,response.status_code,response.text)
            response=consumer.put(url,json=self.ack()|{'project_id':'other'},headers=self.consumer_headers())
            self.assertEqual(403,response.status_code,response.text)
            response=consumer.put(url,json=self.ack()|{'phase_version':2},headers=self.consumer_headers())
            self.assertEqual(409,response.status_code,response.text)
        with self.Session() as db:self.assertEqual(0,len(db.scalars(select(self.Ack)).all()))

    def test_unconfigured_service_credentials_fail_closed(self):
        self.enqueue();self.app.state.phase_control_consumers={}
        with TestClient(self.app) as consumer:
            response=consumer.get(self.internal()+'/config',headers=self.consumer_headers())
            self.assertEqual(503,response.status_code,response.text)
            self.assertEqual('CONSUMER_AUTH_NOT_CONFIGURED',response.json()['detail']['code'])
        self.assertNotIn(self.consumer_token,response.text)

    def test_api_ack_conflict_old_version_and_missing_asset(self):
        self.enqueue()
        with TestClient(self.app) as consumer:
            url=self.internal()+'/acks/media-controller'
            self.assertEqual(200,consumer.put(url,json=self.ack(status='missing_asset'),headers=self.consumer_headers()).status_code)
            self.assertEqual('missing_asset',self.state()['sync_status'])
            response=consumer.put(url,json=self.ack(),headers=self.consumer_headers())
            self.assertEqual(409,response.status_code,response.text)
            response=consumer.put(url,json=self.ack(received=NOW+timedelta(seconds=5)),headers=self.consumer_headers())
            self.assertEqual(200,response.status_code,response.text)
            new=self.create_snapshot(1);self.enqueue(snapshot=new)
            response=consumer.put(url,json=self.ack('media-controller'),headers=self.consumer_headers())
            self.assertEqual(200,response.status_code,response.text)
        self.assertEqual('pending',self.state()['sync_status']);self.assertEqual(2,self.state()['phase_version'])

    def test_missing_control_and_missing_snapshot_have_explicit_errors(self):
        url=f'/api/sessions/{self.s}/control-status'
        response=self.client.get(url)
        self.assertEqual(409,response.status_code,response.text)
        self.assertEqual('CONTROL_NOT_ENQUEUED',response.json()['detail']['code'])
        self.assertEqual(404,self.client.get(f'/api/sessions/{uuid4().hex}/control-status').status_code)
        with TestClient(self.app) as consumer:
            response=consumer.put(self.internal()+'/acks/media-controller',json=self.ack(),headers=self.consumer_headers())
            self.assertEqual(409,response.status_code,response.text)

    def test_ack_commit_failure_rolls_back_and_sanitizes_exception(self):
        self.enqueue()
        with TestClient(self.app) as consumer:
            with patch('app.routers.phase_control.DbSession.commit',side_effect=OperationalError('secret-sql',{},RuntimeError('secret-token'))):
                response=consumer.put(self.internal()+'/acks/media-controller',json=self.ack(),headers=self.consumer_headers())
            self.assertEqual(503,response.status_code,response.text)
            self.assertNotIn('secret',response.text)
        self.assertEqual('pending',self.state()['sync_status'])

    def test_consumer_credentials_cannot_be_replaced_by_cookie_query_or_origin(self):
        self.enqueue()
        url=self.internal()+'/config'
        with TestClient(self.app) as consumer:
            self.assertEqual(401,consumer.get(url,params={'token':self.consumer_token}).status_code)
            headers=self.consumer_headers()|{'Origin':'http://other-origin'}
            self.assertEqual(403,consumer.get(url,headers=headers).status_code)
        self.assertEqual(401,self.client.put(self.internal()+'/acks/media-controller',json=self.ack()).status_code)
        self.assertEqual('pending',self.state()['sync_status'])

    def test_malformed_authorization_config_does_not_open_endpoint(self):
        self.enqueue()
        original=copy.deepcopy(self.app.state.phase_control_consumers)
        self.app.state.phase_control_consumers['media-controller']['scopes']=[{'session_id':self.s}]
        with TestClient(self.app) as consumer:
            self.assertEqual(403,consumer.get(self.internal()+'/config',headers=self.consumer_headers()).status_code)
            self.app.state.phase_control_consumers=original
            self.app.state.phase_control_consumers['second-controller']=copy.deepcopy(original['media-controller'])
            self.assertEqual(401,consumer.get(self.internal()+'/config',headers=self.consumer_headers()).status_code)

    def new_publisher(self, publisher=None):
        cls=getattr(self.logic,'PhaseControlPublisher',None)
        self.assertIsNotNone(cls,'后台发布循环尚未实现')
        return cls(self.Session,publisher or self.redis,stream=self.stream,poll_seconds=.02,batch_size=1)

    def wait_for(self, predicate, timeout=6):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if predicate():return
            time.sleep(.02)
        self.fail('等待后台发布状态超时')

    def test_background_publisher_automatically_sends_and_stops(self):
        self.enqueue();worker=self.new_publisher()
        try:
            worker.start();worker.start()
            self.wait_for(lambda:self.redis.xlen(self.stream)==1)
            self.assertEqual('published',self.state()['delivery_status'])
            self.assertEqual('pending',self.state()['sync_status'])
            self.assertTrue(worker.status()['running'])
        finally:self.assertTrue(worker.stop(timeout=5))
        self.assertFalse(worker.status()['running'])

    def test_background_publisher_continues_after_database_failure(self):
        self.enqueue();worker=self.new_publisher();original=self.logic.publish_pending
        calls=0
        def recovering(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==1:raise OperationalError('private-sql',{},RuntimeError('private-connection'))
            return original(*args,**kwargs)
        with patch.object(self.logic,'publish_pending',side_effect=recovering):
            try:
                worker.start();self.wait_for(lambda:self.redis.xlen(self.stream)==1)
            finally:self.assertTrue(worker.stop(timeout=5))
        self.assertGreaterEqual(calls,2)
        self.assertNotIn('private',json.dumps(worker.status()))

    def test_background_publisher_redis_failure_then_auto_retry(self):
        self.enqueue();attempt=threading.Event();client=self.redis
        class TemporaryOffline:
            def xadd(self,*args,**kwargs):
                if not attempt.is_set():
                    attempt.set();raise redis.ConnectionError('private-redis')
                return client.xadd(*args,**kwargs)
        worker=self.new_publisher(TemporaryOffline())
        try:
            worker.start();self.assertTrue(attempt.wait(2))
            self.wait_for(lambda:self.redis.xlen(self.stream)==1)
            self.assertEqual(2,self.state()['attempts'])
        finally:self.assertTrue(worker.stop(timeout=5))

    def test_background_restart_finds_persisted_queue_without_new_request(self):
        worker=self.new_publisher();worker.start();self.assertTrue(worker.stop(timeout=5))
        self.enqueue();restarted=self.new_publisher()
        try:
            restarted.start();self.wait_for(lambda:self.redis.xlen(self.stream)==1)
        finally:self.assertTrue(restarted.stop(timeout=5))

    def test_owned_publisher_client_closed_on_shutdown_only(self):
        cls=getattr(self.logic,'PhaseControlPublisher',None)
        self.assertIsNotNone(cls,'后台发布循环尚未实现')
        closed=threading.Event()
        class Client:
            def close(self):closed.set()
        worker=cls(self.Session,Client(),stream=self.stream,poll_seconds=.02,batch_size=1,close_client=True)
        worker.start();self.assertTrue(worker.stop(timeout=5));self.assertTrue(closed.is_set())

    def test_main_lifespan_registers_control_and_manages_worker(self):
        path=Path(__file__).resolve().parents[1]/'app/main.py'
        spec=importlib.util.spec_from_file_location('phase_lifespan_test_main',path)
        module=importlib.util.module_from_spec(spec)
        with patch('app.core.service_recovery.initialize_database'):
            spec.loader.exec_module(module)
        self.assertTrue(hasattr(module,'phase_control_lifespan'),'主应用尚未接生命周期')
        paths=set(module.app.openapi()['paths'])
        self.assertIn('/api/sessions/{session_id}/control-status',paths)
        self.assertIn('/internal/phase-control/sessions/{session_id}/config',paths)
        started=threading.Event();stopped=threading.Event()
        class Worker:
            def start(self):started.set()
            def stop(self,**kwargs):stopped.set();return True
        async def exercise():
            with patch.dict(os.environ,{'POC_PHASE_CONTROL_ENABLED':'true','POC_PHASE_CONTROL_REQUIRED_CONSUMERS':'["media-controller","event-dispatcher"]'}),patch.object(module,'build_phase_control_publisher',return_value=Worker()):
                async with module.phase_control_lifespan(module.app):
                    self.assertTrue(started.is_set())
                    self.assertIsNotNone(module.app.state.phase_control_publisher)
            self.assertTrue(stopped.is_set())
        asyncio.run(exercise())

    def test_enabled_lifespan_requires_explicit_server_consumer_configuration(self):
        path=Path(__file__).resolve().parents[1]/'app/main.py'
        spec=importlib.util.spec_from_file_location('phase_explicit_config_main',path)
        module=importlib.util.module_from_spec(spec)
        with patch('app.core.service_recovery.initialize_database'):spec.loader.exec_module(module)
        async def exercise():
            with patch.dict(os.environ,{'POC_PHASE_CONTROL_ENABLED':'true'}):
                os.environ.pop('POC_PHASE_CONTROL_REQUIRED_CONSUMERS',None)
                with self.assertRaises(RuntimeError):
                    async with module.phase_control_lifespan(module.app):pass
        asyncio.run(exercise())

    def test_disabled_lifespan_removes_previous_required_consumer_configuration(self):
        path=Path(__file__).resolve().parents[1]/'app/main.py'
        spec=importlib.util.spec_from_file_location('phase_clear_config_main',path)
        module=importlib.util.module_from_spec(spec)
        with patch('app.core.service_recovery.initialize_database'):spec.loader.exec_module(module)
        module.app.state.phase_control_required_consumers=['stale-consumer']
        async def exercise():
            with patch.dict(os.environ,{'POC_PHASE_CONTROL_ENABLED':'false'}):
                async with module.phase_control_lifespan(module.app):
                    self.assertIsNone(module.app.state.phase_control_required_consumers)
        asyncio.run(exercise())

    def test_real_publisher_runs_with_application_lifespan(self):
        self.enqueue();worker=self.new_publisher()
        path=Path(__file__).resolve().parents[1]/'app/main.py'
        spec=importlib.util.spec_from_file_location('phase_real_lifespan_main',path)
        module=importlib.util.module_from_spec(spec)
        with patch('app.core.service_recovery.initialize_database'):spec.loader.exec_module(module)
        async def exercise():
            with patch.dict(os.environ,{'POC_PHASE_CONTROL_ENABLED':'true','POC_PHASE_CONTROL_REQUIRED_CONSUMERS':'["media-controller","event-dispatcher"]'}),patch.object(module,'build_phase_control_publisher',return_value=worker):
                async with module.phase_control_lifespan(module.app):
                    await asyncio.to_thread(self.wait_for,lambda:self.state()['delivery_status']=='published')
                    self.assertEqual('pending',self.state()['sync_status'])
            self.assertFalse(worker.status()['running'])
        try:asyncio.run(exercise())
        finally:worker.stop(timeout=5)

    def test_stop_interrupts_long_poll_wait(self):
        cls=getattr(self.logic,'PhaseControlPublisher',None);self.assertIsNotNone(cls)
        worker=cls(self.Session,self.redis,stream=self.stream,poll_seconds=60,batch_size=1)
        try:
            worker.start();self.wait_for(lambda:worker.status()['cycles']>=1)
            self.assertTrue(worker.stop(timeout=1))
        finally:worker.stop(timeout=5)

    def test_invalid_publisher_settings_do_not_create_thread(self):
        cls=getattr(self.logic,'PhaseControlPublisher',None);self.assertIsNotNone(cls)
        for args in ({'poll_seconds':0},{'poll_seconds':True},{'poll_seconds':float('nan')},
                     {'batch_size':0},{'batch_size':True},{'batch_size':101}):
            with self.subTest(args=args):
                self.assert_error('INVALID_PUBLISHER_CONFIG',lambda:cls(self.Session,self.redis,**args))

    def test_disabled_lifespan_does_not_start_or_configure_consumers(self):
        path=Path(__file__).resolve().parents[1]/'app/main.py'
        spec=importlib.util.spec_from_file_location('phase_lifespan_disabled_main',path)
        module=importlib.util.module_from_spec(spec)
        with patch('app.core.service_recovery.initialize_database'):spec.loader.exec_module(module)
        self.assertTrue(hasattr(module,'phase_control_lifespan'),'主应用尚未接生命周期')
        async def exercise():
            with patch.dict(os.environ,{'POC_PHASE_CONTROL_ENABLED':'false'}),patch.object(module,'build_phase_control_publisher') as factory:
                async with module.phase_control_lifespan(module.app):
                    factory.assert_not_called()
                    self.assertIsNone(module.app.state.phase_control_publisher)
        asyncio.run(exercise())

    def load_test_main(self):
        path=Path(__file__).resolve().parents[1]/'app/main.py'
        spec=importlib.util.spec_from_file_location('phase_credentials_main',path)
        module=importlib.util.module_from_spec(spec)
        with patch('app.core.service_recovery.initialize_database'):spec.loader.exec_module(module)
        return module

    def credential_mapping(self):
        return {name:{'token_sha256':hashlib.sha256(token.encode('ascii')).hexdigest(),
                'scopes':[{'project_id':self.p,'room_id':self.r,'session_id':self.s,'source_id':'camera-test'}]}
            for name,token in [('media-controller',self.consumer_token),('event-dispatcher',secrets.token_urlsafe(32))]}

    def test_lifespan_loads_scoped_credentials_and_real_internal_requests(self):
        self.enqueue();module=self.load_test_main();mapping=self.credential_mapping()
        class Worker:
            def start(self):pass
            def stop(self,**kwargs):return True
        async def exercise(path):
            with patch.dict(os.environ,{'POC_PHASE_CONTROL_ENABLED':'true','POC_PHASE_CONTROL_REQUIRED_CONSUMERS':'["media-controller","event-dispatcher"]',
                    'POC_PHASE_CONTROL_CONSUMERS_FILE':str(path)}),patch.object(module,'build_phase_control_publisher',return_value=Worker()):
                async with module.phase_control_lifespan(module.app):
                    self.assertEqual(mapping,getattr(module.app.state,'phase_control_consumers',None))
                    self.app.state.phase_control_consumers=module.app.state.phase_control_consumers
                    with TestClient(self.app) as client:
                        response=client.get(self.internal()+'/config',headers=self.consumer_headers())
                        self.assertEqual(200,response.status_code)
                        self.assertEqual(self.snapshot['stage_snapshot_id'],response.json()['stage_snapshot_id'])
                        value=self.ack(status='missing_asset')
                        response=client.put(self.internal()+'/acks/media-controller',json=value,headers=self.consumer_headers())
                        self.assertEqual(200,response.status_code)
                    self.assertEqual('missing_asset',self.state()['sync_status'])
            self.assertIsNone(module.app.state.phase_control_consumers)
            self.assertIsNone(module.app.state.phase_control_required_consumers)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'consumers.json';path.write_text(json.dumps(mapping),encoding='utf-8')
            asyncio.run(exercise(path))

    def test_missing_credentials_file_keeps_service_auth_denied(self):
        module=self.load_test_main();module.app.state.phase_control_consumers={'stale':{}}
        class Worker:
            def start(self):pass
            def stop(self,**kwargs):return True
        async def exercise():
            with patch.dict(os.environ,{'POC_PHASE_CONTROL_ENABLED':'true','POC_PHASE_CONTROL_REQUIRED_CONSUMERS':'["media-controller","event-dispatcher"]'}),patch.object(module,'build_phase_control_publisher',return_value=Worker()):
                os.environ.pop('POC_PHASE_CONTROL_CONSUMERS_FILE',None)
                async with module.phase_control_lifespan(module.app):
                    self.assertIsNone(module.app.state.phase_control_consumers)
                    self.app.state.phase_control_consumers=module.app.state.phase_control_consumers
                    with TestClient(self.app) as client:
                        self.assertEqual(503,client.get(self.internal()+'/config',headers=self.consumer_headers()).status_code)
        asyncio.run(exercise())

    def test_empty_initial_scope_is_valid_but_cannot_read_or_ack_any_session(self):
        module=self.load_test_main();mapping=self.credential_mapping()
        mapping['media-controller']['scopes']=[]
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'consumers.json';path.write_text(json.dumps(mapping))
            with patch.dict(os.environ,{'POC_PHASE_CONTROL_CONSUMERS_FILE':str(path)}):
                accepted=module.load_phase_control_consumers(['media-controller','event-dispatcher'])
            self.app.state.phase_control_consumers=accepted
            with TestClient(self.app) as client:
                self.assertEqual(403,client.get(self.internal()+'/config',headers=self.consumer_headers()).status_code)
                self.assertEqual(403,client.put(self.internal()+'/acks/media-controller',json=self.ack(),headers=self.consumer_headers()).status_code)

    def test_invalid_credential_mapping_rejects_before_worker_start(self):
        module=self.load_test_main();base=self.credential_mapping()
        invalid=[]
        def altered(edit):
            value=copy.deepcopy(base);edit(value);invalid.append(value)
        altered(lambda v:v['media-controller'].update(token=self.consumer_token))
        altered(lambda v:v['event-dispatcher'].update(token_sha256=v['media-controller']['token_sha256']))
        altered(lambda v:v['media-controller'].update(token_sha256='not-a-digest'))
        altered(lambda v:v['media-controller'].update(scopes=None))
        altered(lambda v:v['media-controller']['scopes'][0].pop('source_id'))
        altered(lambda v:v['media-controller']['scopes'][0].update(session_id='*'))
        altered(lambda v:v['media-controller']['scopes'][0].update(extra='unexpected'))
        altered(lambda v:v['media-controller']['scopes'].append(copy.deepcopy(v['media-controller']['scopes'][0])))
        altered(lambda v:v.pop('event-dispatcher'))
        invalid.extend([{},[],None])
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'consumers.json'
            for value in invalid:
                path.write_text(json.dumps(value),encoding='utf-8')
                async def exercise():
                    with patch.dict(os.environ,{'POC_PHASE_CONTROL_ENABLED':'true','POC_PHASE_CONTROL_REQUIRED_CONSUMERS':'["media-controller","event-dispatcher"]',
                            'POC_PHASE_CONTROL_CONSUMERS_FILE':str(path)}),patch.object(module,'build_phase_control_publisher') as factory:
                        with self.assertRaises(RuntimeError) as caught:
                            async with module.phase_control_lifespan(module.app):pass
                        self.assertNotIn(str(path),str(caught.exception));self.assertNotIn(self.consumer_token,str(caught.exception))
                        factory.assert_not_called()
                        self.assertIsNone(module.app.state.phase_control_consumers)
                        self.assertIsNone(module.app.state.phase_control_required_consumers)
                with self.subTest(value=value):asyncio.run(exercise())

    def test_credential_file_io_json_size_and_duplicate_keys_rejected(self):
        module=self.load_test_main()
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'consumers.json'
            for content in (None,'{bad-json','{"media-controller":{},"media-controller":{}}',' '*(256*1024+1)):
                if content is not None:path.write_text(content,encoding='utf-8')
                async def exercise():
                    with patch.dict(os.environ,{'POC_PHASE_CONTROL_ENABLED':'true','POC_PHASE_CONTROL_REQUIRED_CONSUMERS':'["media-controller","event-dispatcher"]',
                            'POC_PHASE_CONTROL_CONSUMERS_FILE':str(path)}),patch.object(module,'build_phase_control_publisher') as factory:
                        with self.assertRaises(RuntimeError):
                            async with module.phase_control_lifespan(module.app):pass
                        factory.assert_not_called()
                        self.assertIsNone(module.app.state.phase_control_consumers)
                with self.subTest(content_kind=type(content).__name__):asyncio.run(exercise())

    def test_disabled_lifespan_clears_credentials_without_reading_file(self):
        module=self.load_test_main();module.app.state.phase_control_consumers=self.credential_mapping()
        async def exercise():
            with patch.dict(os.environ,{'POC_PHASE_CONTROL_ENABLED':'false','POC_PHASE_CONTROL_CONSUMERS_FILE':'missing-file'}):
                async with module.phase_control_lifespan(module.app):
                    self.assertIsNone(module.app.state.phase_control_consumers)
        asyncio.run(exercise())

    def test_publisher_build_failure_clears_loaded_credentials(self):
        module=self.load_test_main()
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'consumers.json';path.write_text(json.dumps(self.credential_mapping()),encoding='utf-8')
            async def exercise():
                with patch.dict(os.environ,{'POC_PHASE_CONTROL_ENABLED':'true','POC_PHASE_CONTROL_REQUIRED_CONSUMERS':'["media-controller","event-dispatcher"]',
                        'POC_PHASE_CONTROL_CONSUMERS_FILE':str(path)}),patch.object(module,'build_phase_control_publisher',side_effect=RuntimeError('test-build-failure')):
                    with self.assertRaises(RuntimeError):
                        async with module.phase_control_lifespan(module.app):pass
                self.assertIsNone(module.app.state.phase_control_consumers)
                self.assertIsNone(module.app.state.phase_control_required_consumers)
            asyncio.run(exercise())


def run_isolated():
    names=['poc-control-test-'+uuid4().hex[:12] for _ in range(2)]
    started=[];password=uuid4().hex
    old={key:os.environ.get(key) for key in ('PHASE_TEST_DATABASE_URL','PHASE_TEST_REDIS_URL','PHASE_TEST_REDIS_CONTAINER')}
    try:
        commands=[['docker','run','-d','--name',names[0],'--tmpfs','/var/lib/postgresql/data','-e','POSTGRES_USER=phase_test','-e','POSTGRES_PASSWORD='+password,'-e','POSTGRES_DB=phase_test','-p','127.0.0.1::5432','postgres:16-alpine'],
            ['docker','run','-d','--name',names[1],'-p','127.0.0.1::6379','redis:7-alpine','redis-server','--save','','--appendonly','no']]
        for name,command in zip(names,commands):
            if subprocess.run(command,capture_output=True).returncode:raise RuntimeError('独立PG/Redis启动失败')
            started.append(name)
        deadline=time.monotonic()+60
        while subprocess.run(['docker','exec',names[0],'pg_isready','-U','phase_test','-d','phase_test'],capture_output=True).returncode:
            if time.monotonic()>deadline:raise RuntimeError('独立PG等待超时')
            time.sleep(.5)
        ports=[subprocess.check_output(['docker','port',name,port]).decode().strip().split(':')[-1] for name,port in zip(names,['5432/tcp','6379/tcp'])]
        os.environ['PHASE_TEST_DATABASE_URL']=f'postgresql+psycopg://phase_test:{password}@127.0.0.1:{ports[0]}/phase_test'
        os.environ['PHASE_TEST_REDIS_URL']=f'redis://127.0.0.1:{ports[1]}/0'
        os.environ['PHASE_TEST_REDIS_CONTAINER']=names[1]
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
        return unittest.main(argv=[sys.argv[0],'-v'],exit=False).result.wasSuccessful()
    finally:
        for name in reversed(started):subprocess.run(['docker','rm','-f',name],capture_output=True,check=True)
        for key,value in old.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value


if __name__=='__main__':
    if '--isolated-services' in sys.argv:sys.exit(0 if run_isolated() else 1)
    else:unittest.main()
