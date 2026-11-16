"""运行态势：推流就绪数、AI 引擎状态、未闭环异常数。

给看板顶部健康条用：一眼看清"系统在不在跑、几路画面正常、积压多少异常"。
推流状态直接探 mediamtx 的 HLS 索引（200 = 已就绪，404 = 未推流），不依赖它的管理 API。
"""
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_session

router = APIRouter()

CAMERAS = [f"cam{i}" for i in range(1, 11)]
CACHE: dict = {"at": 0.0, "data": None}
CACHE_TTL = 15.0
AI_ONLINE_SECONDS = 300


def _admin(request: Request) -> str:
    from fastapi import HTTPException
    admin = getattr(request.state, "admin", None)
    if not isinstance(admin, dict) or not admin.get("username"):
        raise HTTPException(403, "管理员身份缺失")
    return admin["username"]


def _probe(camera: str) -> tuple[str, bool]:
    try:
        with urllib.request.urlopen(f"http://mediamtx:8888/{camera}/index.m3u8", timeout=1.5) as response:
            return camera, response.status == 200
    except Exception:
        return camera, False


@router.get("/ops/health")
def ops_health(request: Request, session: Session = Depends(get_session)):
    _admin(request)
    now = time.time()
    if CACHE["data"] and now - CACHE["at"] < CACHE_TTL:
        return CACHE["data"]

    with ThreadPoolExecutor(max_workers=len(CAMERAS)) as pool:
        probed = list(pool.map(_probe, CAMERAS))
    ready = [cam for cam, ok in probed if ok]

    row = session.execute(text(
        "select count(*) filter (where status = 'PENDING' and risk_level in ('high','medium','low')) as pending, "
        "       count(*) filter (where event_type = 'person_detected') as heartbeat, "
        "       max(occurred_at) as last_event_at "
        "from events where occurred_at > now() - interval '1 day'"
    )).mappings().first()

    last_at = row["last_event_at"] if row else None
    age = (time.time() - last_at.timestamp()) if last_at else None
    # 事件时间只能说明有输出，不能证明各个算法进程仍健康。
    engine = "unknown"

    data = {
        "ai_engine": engine,
        "last_event_seconds": round(age) if age is not None else None,
        "streams_ready": len(ready),
        "streams_total": len(CAMERAS),
        "streams_missing": [cam for cam, ok in probed if not ok],
        "pending": int(row["pending"] or 0) if row else 0,
        "checked_at": time.strftime("%H:%M:%S"),
    }
    CACHE.update({"at": now, "data": data})
    return data
