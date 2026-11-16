"""视频画面叠加层：返回指定机位的最新检测框与已配置区域。

检测框来自 vision-worker 写入 Redis 的观测（归一化坐标），
区域来自 zones 表。前端在视频上叠一层 canvas 即可显示。
"""
from __future__ import annotations

import json
import os
import time

import redis
from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from app.db import get_session

router = APIRouter(tags=["monitor"])
_redis = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), socket_timeout=2, decode_responses=True)


@router.get("/monitor/overlay")
def overlay(camera_id: str = Query(..., max_length=64), session=Depends(get_session)):
    boxes: list = []
    phones: list = []
    age: float | None = None
    try:
        # 倒序扫最近的观测，取该机位最新一条 person_detected
        for _entry_id, data in _redis.xrevrange("observations", count=300):
            if data.get("source_id") != camera_id:
                continue
            if data.get("type") != "person_detected":
                continue
            payload = json.loads(data.get("payload", "{}"))
            boxes = payload.get("person_boxes", []) or []
            phones = payload.get("phone_boxes", []) or []
            age = round(time.time() - float(data.get("occurred_at", 0)), 1)
            break
    except Exception:  # noqa: BLE001 — 叠加层拿不到数据时不能让整个页面挂掉
        pass

    rows = session.execute(text(
        "select name, type, polygon from zones where camera_id = :cid order by name"
    ), {"cid": camera_id}).mappings().all()

    return {
        "camera_id": camera_id,
        "boxes": boxes,
        "phones": phones,
        "zones": [{"name": r["name"], "type": r["type"], "polygon": r["polygon"]} for r in rows],
        "age_sec": age,
    }


@router.get("/monitor/overlay/batch")
def overlay_batch(camera_ids: str = Query("", max_length=512), session=Depends(get_session)):
    """一次取多路，供监控墙轮询，避免 12 个请求。"""
    wanted = [c.strip() for c in camera_ids.split(",") if c.strip()]
    if not wanted:
        return {"items": {}}

    latest: dict[str, dict] = {}
    try:
        for _entry_id, data in _redis.xrevrange("observations", count=500):
            sid = data.get("source_id")
            if sid not in wanted or sid in latest:
                continue
            if data.get("type") != "person_detected":
                continue
            payload = json.loads(data.get("payload", "{}"))
            latest[sid] = {
                "boxes": payload.get("person_boxes", []) or [],
                "phones": payload.get("phone_boxes", []) or [],
                "age_sec": round(time.time() - float(data.get("occurred_at", 0)), 1),
            }
    except Exception:  # noqa: BLE001
        pass

    zone_rows = session.execute(text(
        "select camera_id, name, type, polygon from zones where camera_id = any(:ids)"
    ), {"ids": wanted}).mappings().all()
    zones_by_cam: dict[str, list] = {}
    for r in zone_rows:
        zones_by_cam.setdefault(r["camera_id"], []).append(
            {"name": r["name"], "type": r["type"], "polygon": r["polygon"]}
        )

    return {"items": {
        cid: {**latest.get(cid, {"boxes": [], "phones": [], "age_sec": None}),
              "zones": zones_by_cam.get(cid, [])}
        for cid in wanted
    }}
