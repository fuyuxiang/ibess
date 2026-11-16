"""有界启动等待和运行时数据库故障响应，不输出连接凭据。"""
import logging
import time

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeout
from starlette.responses import JSONResponse

log = logging.getLogger('poc.service-recovery')


class StartupUnavailable(RuntimeError):
    pass


def initialize_database(engine, create_schema, migrate, seed, *, timeout=120, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            with engine.connect() as connection:
                connection.execute(text('SELECT 1'))
            create_schema(engine)
            migrate(engine)
            seed(engine)
            log.info('数据库初始化就绪；attempt=%d', attempt)
            return
        except (OperationalError, PoolTimeout) as exc:
            code = getattr(getattr(exc, 'orig', None), 'sqlstate', None)
            if code and (str(code).startswith('28') or code == '3D000'):
                raise StartupUnavailable('数据库配置无效，请检查数据库名称和认证配置') from None
            remaining = deadline - clock()
            if remaining <= 0:
                raise StartupUnavailable('数据库启动等待超时；服务将由容器重启策略重新尝试') from None
            delay = min(remaining, 2 ** min(attempt, 3))
            # 非认证类连接失败可重试。引擎pre_ping负责清理已失效连接。
            log.warning('数据库暂不可用，%.1f秒后重试；attempt=%d error_type=%s', delay, attempt, type(exc).__name__)
            sleep(delay)


class DatabaseRecoveryMiddleware:
    """覆盖认证中间件中的连接异常；不续期会话，不重放写请求。"""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        started = False

        async def wrapped_send(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
            await send(message)

        try:
            await self.app(scope, receive, wrapped_send)
        except (OperationalError, PoolTimeout) as exc:
            log.warning('数据库请求暂不可用；error_type=%s', type(exc).__name__)
            if started:
                # 已开始SSE/文件响应时不能再发第二组响应头。
                raise
            response = JSONResponse({'detail': '数据库暂不可用，请稍后重试'}, status_code=503,
                                    headers={'Cache-Control': 'no-store', 'Retry-After': '3'})
            await response(scope, receive, send)
