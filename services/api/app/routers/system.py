import socket
import urllib.request
import urllib3
from concurrent.futures import ThreadPoolExecutor

import redis
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from minio import Minio
from sqlalchemy import text

from app.config import settings
from app.db import engine

router = APIRouter()


def _check_postgres() -> str:
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return "ok"
    except Exception as e:
        return f"error: {e}"


def _check_redis() -> str:
    try:
        r = redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            return "ok" if r.ping() else "error: ping failed"
        finally:
            r.close()
    except Exception as e:
        return f"error: {e}"


def _check_minio() -> str:
    try:
        m = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_root_user,
            secret_key=settings.minio_root_password,
            secure=False,
            http_client=urllib3.PoolManager(timeout=urllib3.Timeout(connect=1,read=2),retries=False),
        )
        return "ok" if m.bucket_exists(settings.minio_bucket) else "error: 证据存储桶不存在"
    except Exception as e:
        return f"error: {e}"


def _check_mediamtx() -> str:
    try:
        with socket.create_connection(("mediamtx", 8554), timeout=2):
            return "ok"
    except Exception as e:
        return f"error: {e}"


@router.get("/health")
def health():
    checks = {'postgres':_check_postgres, 'redis':_check_redis, 'minio':_check_minio, 'mediamtx':_check_mediamtx}
    with ThreadPoolExecutor(max_workers=4) as pool:
        services = dict(zip(checks, pool.map(lambda check:check(), checks.values())))
    ok_count = sum(1 for v in services.values() if v == "ok")
    status = "ok" if ok_count == len(services) else "degraded"
    return JSONResponse(
        {
            "status": status,
            "healthy": f"{ok_count}/{len(services)}",
            "services": services,
        }
    )
