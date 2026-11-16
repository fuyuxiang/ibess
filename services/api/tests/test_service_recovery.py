"""启动等待、健康检查和数据库瞬断恢复，不改正式业务数据。"""
import unittest
from unittest.mock import MagicMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError, ProgrammingError


class Clock:
    def __init__(self): self.value = 0
    def now(self): return self.value
    def sleep(self, seconds): self.value += seconds


def outage():
    return OperationalError('SELECT 1', {}, Exception('sensitive database connection information'))


class RecoveryTest(unittest.TestCase):
    def test_bootstrap_retries_transient_connection_then_initializes(self):
        from app.core.service_recovery import initialize_database
        clock, engine = Clock(), Mock()
        engine.connect.side_effect = [outage(), outage(), MagicMock()]
        create, migrate, seed = Mock(), Mock(), Mock()
        initialize_database(engine, create, migrate, seed, timeout=20, clock=clock.now, sleep=clock.sleep)
        self.assertEqual(engine.connect.call_count, 3)
        create.assert_called_once_with(engine)
        migrate.assert_called_once_with(engine)
        seed.assert_called_once_with(engine)
        self.assertGreater(clock.value, 0)

    def test_bootstrap_expires_cleanly_and_does_not_retry_schema_errors(self):
        from app.core.service_recovery import initialize_database, StartupUnavailable
        clock, engine = Clock(), Mock()
        engine.connect.side_effect = outage()
        with self.assertRaises(StartupUnavailable) as error:
            initialize_database(engine, Mock(), Mock(), Mock(), timeout=5, clock=clock.now, sleep=clock.sleep)
        self.assertNotIn('sensitive', str(error.exception))
        self.assertLessEqual(clock.value, 5)
        engine = MagicMock()
        create = Mock(side_effect=ProgrammingError('bad schema', {}, Exception('error')))
        with self.assertRaises(ProgrammingError):
            initialize_database(engine, create, Mock(), Mock(), timeout=10, clock=clock.now, sleep=clock.sleep)
        create.assert_called_once()

    def test_health_ready_reports_503_then_recovers_live_stays_200(self):
        from app.routers.health import create_health_router
        state = {'database': False, 'redis': True, 'minio': True}
        app = FastAPI()
        app.include_router(create_health_router({k: (lambda name=k: state[name]) for k in state}))
        with TestClient(app) as client:
            ready = client.get('/health/ready')
            self.assertEqual(ready.status_code, 503)
            self.assertEqual(ready.json()['dependencies']['database'], 'unavailable')
            self.assertEqual(client.get('/health/live').status_code, 200)
            self.assertEqual(ready.headers['cache-control'], 'no-store')
            state['database'] = True
            self.assertEqual(client.get('/health/ready').status_code, 200)
            state['redis'] = False
            self.assertEqual(client.get('/health/ready').status_code, 503)

    def test_health_does_not_expose_raw_connection_exception(self):
        from app.routers.health import create_health_router
        app = FastAPI()
        app.include_router(create_health_router({'database': Mock(side_effect=outage())}))
        with TestClient(app) as client:
            response = client.get('/health/ready')
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('sensitive', response.text)

    def test_middleware_database_failure_before_auth_is_503_and_recovers(self):
        from app.core.service_recovery import DatabaseRecoveryMiddleware
        state = {'broken': True}
        inner = FastAPI()
        @inner.get('/api/auth/status')
        async def status():
            if state['broken']: raise outage()
            return {'ok': True}
        inner.add_middleware(DatabaseRecoveryMiddleware)
        with TestClient(inner) as client:
            response = client.get('/api/auth/status')
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('sensitive', response.text)
            self.assertEqual(response.headers['retry-after'], '3')
            state['broken'] = False
            self.assertEqual(client.get('/api/auth/status').status_code, 200)


if __name__ == '__main__': unittest.main()
