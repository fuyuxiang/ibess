"""无需管理员登录的最小探针；只返回依赖状态，不泄露配置。"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse


def create_health_router(checks):
    router = APIRouter(prefix='/health')

    @router.get('/live')
    def live():
        return JSONResponse({'status': 'alive'}, headers={'Cache-Control': 'no-store'})

    @router.get('/ready')
    def ready():
        dependencies = {}
        for name, check in checks.items():
            try:
                available = check()
            except Exception:
                available = False
            dependencies[name] = 'ok' if available else 'unavailable'
        available = all(value == 'ok' for value in dependencies.values())
        return JSONResponse({'status': 'ready' if available else 'not_ready', 'dependencies': dependencies},
                            status_code=200 if available else 503, headers={'Cache-Control': 'no-store'})

    return router


def database_ready():
    from sqlalchemy import text
    from app.db import engine
    with engine.connect() as connection:
        return connection.scalar(text('SELECT 1')) == 1


def redis_ready():
    import redis
    from app.config import settings
    with redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2) as client:
        return client.ping()


def minio_ready():
    import urllib3
    from app.config import settings
    from minio import Minio
    http = urllib3.PoolManager(timeout=urllib3.Timeout(connect=2, read=2), retries=False)
    client = Minio(settings.minio_endpoint, access_key=settings.minio_root_user,
                   secret_key=settings.minio_root_password, secure=False, http_client=http)
    try:
        client.list_buckets()
        return True
    finally:
        http.clear()


router = create_health_router({'database': database_ready, 'redis': redis_ready, 'minio': minio_ready})
