"""3.3区域配置API：真实PostgreSQL随机schema，不访问正式业务数据。"""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import subprocess
import sys
import time
import unittest
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker


class ZoneConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        if not all((root / f'app/{name}.py').exists() for name in ('zone_config_models', 'routers/zone_config')):
            raise RuntimeError('3.3区域配置模型与逻辑尚未实现')
        from app.models import Base, EvalSession
        from app.business_models import BusinessProject, BusinessRoom, BusinessSeat, BusinessPerson, BusinessParticipant
        from app.auth_models import AdminAccount, AdminAudit, AdminSession
        from app.phase_models import PhaseBinding
        from app.zone_config_models import SourceZoneConfig
        from app.auth import AdminAuthMiddleware
        from app.db import get_session
        from app.routers.zone_config import router

        url = os.environ.get('ZONE_TEST_DATABASE_URL')
        if not url:
            raise RuntimeError('必须显式指定隔离PostgreSQL连接')
        cls.admin = create_engine(url)
        if cls.admin.dialect.name != 'postgresql':
            raise RuntimeError('区域配置并发测试必须使用真实PostgreSQL')
        cls.schema = 'test_zone_v2_' + uuid4().hex
        with cls.admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        try:
            cls.engine = create_engine(url, connect_args={'options': f'-csearch_path={cls.schema} -clock_timeout=5000 -cstatement_timeout=15000'})
            cls.tables = [BusinessProject.__table__, BusinessRoom.__table__, BusinessPerson.__table__, BusinessParticipant.__table__,
                BusinessSeat.__table__, EvalSession.__table__,
                PhaseBinding.__table__, SourceZoneConfig.__table__, AdminAccount.__table__, AdminAudit.__table__, AdminSession.__table__]
            Base.metadata.create_all(cls.engine, tables=cls.tables)
            cls.Session = sessionmaker(bind=cls.engine)
            with cls.Session.begin() as db:
                db.add(AdminAccount(account_id=1, username='zone_admin', display_name='测试管理员', password_hash='not-a-real-password'))
            cls.Project, cls.Room, cls.Seat, cls.EvalSession = BusinessProject, BusinessRoom, BusinessSeat, EvalSession
            cls.Binding, cls.Config, cls.AuthSession, cls.AdminAccount = PhaseBinding, SourceZoneConfig, AdminSession, AdminAccount
            cls.app = FastAPI()
            cls.app.add_middleware(AdminAuthMiddleware, session_factory=cls.Session)
            cls.app.include_router(router, prefix='/api')

            def override_db():
                with cls.Session() as db:
                    yield db
            cls.app.dependency_overrides[get_session] = override_db
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'engine'):
            cls.engine.dispose()
        if hasattr(cls, 'schema'):
            with cls.admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
            cls.admin.dispose()

    def setUp(self):
        self.project, self.room, self.session = [str(uuid4()) for _ in range(3)]
        self.source = 'camera-zone-test'
        self.seat = str(uuid4())
        token, csrf = secrets.token_urlsafe(32), secrets.token_hex(32)
        self.token = token
        with self.Session.begin() as db:
            db.add(self.Project(project_id=self.project, code=self.project, name='测试项目', owner='测试', status='running'))
            db.flush()
            db.add(self.Room(room_id=self.room, project_id=self.project, code=self.room, name='测试评标室', location='测试', enabled=True))
            db.add(self.EvalSession(session_id=self.session, name='区域测试场次', status='running', current_phase='review'))
            db.flush()
            db.add(self.Binding(session_id=self.session, project_id=self.project, room_id=self.room, source_id=self.source, active=True))
            db.add(self.Seat(seat_id=self.seat, room_id=self.room, project_id=self.project, code='A01', enabled=True))
            db.flush()
            db.add(self.AuthSession(token_hash=hashlib.sha256(token.encode('ascii')).hexdigest(), account_id=1,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1), csrf_token=csrf))
        self.client = TestClient(self.app, headers={'X-Requested-With': 'XMLHttpRequest', 'X-CSRF-Token': csrf})
        self.client.cookies.set('eval_admin_session', token)
        self.url = f'/api/sessions/{self.session}/sources/{self.source}/zones'
        self.area = {'zone_id': 'seat-area-1', 'name': '专家工位A01', 'kind': 'seat', 'seat_id': self.seat,
            'polygon': [[0.1, 0.1], [0.35, 0.1], [0.35, 0.4], [0.1, 0.4]]}
        self.line = {'line_id': 'entry-line-1', 'name': '评标室入口线', 'start': [0.8, 0.2], 'end': [0.8, 0.8], 'direction': 'both'}

    def tearDown(self):
        self.client.close()

    def test_unconfigured_get_returns_empty_version_zero_without_writing(self):
        response = self.client.get(self.url)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual({'config_version': 0, 'zones': [], 'entry_lines': []},
            {key: response.json()[key] for key in ('config_version', 'zones', 'entry_lines')})
        with self.Session() as db:
            self.assertIsNone(db.query(self.Config).filter_by(session_id=self.session, source_id=self.source).first())

    def test_put_normalized_polygon_and_entry_line_then_read_same_scope(self):
        response = self.client.put(self.url, json={'config_version': 0, 'zones': [self.area], 'entry_lines': [self.line]})
        self.assertEqual(200, response.status_code, response.text)
        value = response.json()
        self.assertEqual(1, value['config_version'])
        self.assertEqual(self.project, value['project_id'])
        self.assertEqual(self.room, value['room_id'])
        self.assertEqual(self.session, value['session_id'])
        self.assertEqual(self.source, value['source_id'])
        self.assertEqual([self.area], value['zones'])
        self.assertEqual([self.line], value['entry_lines'])
        self.assertEqual(value, self.client.get(self.url).json())

    def test_rejects_invalid_polygons_and_entry_lines(self):
        invalid = [
            {**self.area, 'polygon': [[.1,.1],[.5,.5]]},
            {**self.area, 'polygon': [[.1,.1],[.5,.1],[.5,.5],[.1,.5],[.1,.1]]},
            {**self.area, 'polygon': [[.1,.1],[.5,.5],[.1,.5],[.5,.1]]},
            {**self.area, 'polygon': [[-.1,.1],[.5,.1],[.5,.5]]},
            {**self.area, 'polygon': [[0,.1],[0,.5],[0,.8]]},
        ]
        for zone in invalid:
            with self.subTest(zone=zone):
                response = self.client.put(self.url, json={'config_version': 0, 'zones': [zone], 'entry_lines': []})
                self.assertEqual(422, response.status_code, response.text)
        bad_line = {**self.line, 'end': self.line['start']}
        response = self.client.put(self.url, json={'config_version': 0, 'zones': [], 'entry_lines': [bad_line]})
        self.assertEqual(422, response.status_code, response.text)

    def test_wrong_source_and_unknown_session_are_rejected(self):
        self.assertEqual(409, self.client.get(f'/api/sessions/{self.session}/sources/other-camera/zones').status_code)
        self.assertEqual(404, self.client.get(f'/api/sessions/{uuid4()}/sources/{self.source}/zones').status_code)

    def test_seat_from_another_room_is_rejected(self):
        other_project, other_room = str(uuid4()), str(uuid4())
        with self.Session.begin() as db:
            db.add(self.Project(project_id=other_project, code=other_project, name='另一项目', owner='测试', status='running'))
            db.flush()
            db.add(self.Room(room_id=other_room, project_id=other_project, code=other_room, name='另一评标室', location='测试', enabled=True))
            db.flush()
            foreign_seat_id = str(uuid4())
            db.add(self.Seat(seat_id=foreign_seat_id, room_id=other_room, project_id=other_project, code='B01', enabled=True))
        response = self.client.put(self.url, json={'config_version': 0,
            'zones': [{**self.area, 'seat_id': foreign_seat_id}], 'entry_lines': []})
        self.assertEqual(422, response.status_code, response.text)
        self.assertEqual('SEAT_ROOM_MISMATCH', response.json()['detail']['code'])

    def test_optimistic_version_conflict_does_not_overwrite(self):
        first = self.client.put(self.url, json={'config_version': 0, 'zones': [self.area], 'entry_lines': []})
        self.assertEqual(200, first.status_code, first.text)
        stale = self.client.put(self.url, json={'config_version': 0, 'zones': [], 'entry_lines': []})
        self.assertEqual(409, stale.status_code, stale.text)
        self.assertEqual('VERSION_CONFLICT', stale.json()['detail']['code'])
        self.assertEqual([self.area], self.client.get(self.url).json()['zones'])

    def test_closed_session_cannot_change_zone_configuration(self):
        with self.Session.begin() as db:
            db.scalar(select(self.EvalSession).where(self.EvalSession.session_id == self.session)).status = 'closed'
        response = self.client.put(self.url, json={'config_version': 0, 'zones': [self.area], 'entry_lines': []})
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual('SESSION_CLOSED', response.json()['detail']['code'])

    def test_closed_session_zone_configuration_remains_readable(self):
        self.assertEqual(200, self.client.put(self.url, json={'config_version': 0, 'zones': [self.area], 'entry_lines': []}).status_code)
        with self.Session.begin() as db:
            db.scalar(select(self.EvalSession).where(self.EvalSession.session_id == self.session)).status = 'closed'
        self.assertEqual(1, self.client.get(self.url).json()['config_version'])

    def test_bad_scope_body_and_extra_fields_are_rejected(self):
        response = self.client.put(self.url, json={'config_version': 0, 'project_id': self.project, 'zones': [], 'entry_lines': []})
        self.assertEqual(422, response.status_code, response.text)
        response = self.client.put(self.url, json={'config_version': -1, 'zones': [], 'entry_lines': []})
        self.assertEqual(422, response.status_code, response.text)
        response = self.client.put(self.url, json={'config_version': 0, 'zones': [], 'entry_lines': [], 'actor_id': 'spoofed'})
        self.assertEqual(422, response.status_code, response.text)

    def test_anonymous_and_missing_csrf_writes_are_rejected(self):
        self.client.cookies.clear()
        self.assertEqual(401, self.client.put(self.url, json={'config_version':0,'zones':[],'entry_lines':[]}).status_code)
        no_csrf = TestClient(self.app, headers={'X-Requested-With': 'XMLHttpRequest'})
        no_csrf.cookies.set('eval_admin_session', self.token)
        self.assertEqual(403, no_csrf.put(self.url, json={'config_version':0,'zones':[],'entry_lines':[]}).status_code)
        no_csrf.close()


def run_isolated_pg():
    name = 'poc-zone-test-' + uuid4().hex[:12]
    password = uuid4().hex
    old = os.environ.get('ZONE_TEST_DATABASE_URL')
    started = False
    try:
        result = subprocess.run(['docker', 'run', '-d', '--name', name, '--tmpfs', '/var/lib/postgresql/data',
            '-e', 'POSTGRES_USER=zone_test', '-e', 'POSTGRES_PASSWORD=' + password, '-e', 'POSTGRES_DB=zone_test',
            '-p', '127.0.0.1::5432', 'postgres:16-alpine'], capture_output=True)
        if result.returncode:
            raise RuntimeError('隔离PostgreSQL启动失败')
        started = True
        until = time.monotonic() + 60
        while subprocess.run(['docker', 'exec', name, 'pg_isready', '-U', 'zone_test', '-d', 'zone_test'], capture_output=True).returncode:
            if time.monotonic() > until:
                raise RuntimeError('隔离PostgreSQL等待超时')
            time.sleep(.4)
        port = subprocess.check_output(['docker', 'port', name, '5432/tcp']).decode().strip().split(':')[-1]
        os.environ['ZONE_TEST_DATABASE_URL'] = f'postgresql+psycopg://zone_test:{password}@127.0.0.1:{port}/zone_test'
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        return unittest.main(argv=[sys.argv[0], '-v'], exit=False).result.wasSuccessful()
    finally:
        if started:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, check=True)
        if old is None:
            os.environ.pop('ZONE_TEST_DATABASE_URL', None)
        else:
            os.environ['ZONE_TEST_DATABASE_URL'] = old


if __name__ == '__main__':
    if '--isolated-pg' in sys.argv:
        sys.exit(0 if run_isolated_pg() else 1)
    unittest.main()
