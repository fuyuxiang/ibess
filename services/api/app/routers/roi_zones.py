"""ROI 区域标定：按摄像头维护工位区/限制区多边形。

坐标一律用 0~1 归一化值，换分辨率不用重标。
与检测侧共用 zones 表：vision-worker 与 audit worker 都从这里读配置。
"""
from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.db import get_session

router = APIRouter(tags=["roi"])

ALLOWED_TYPES = ("seat", "restricted")

class ZoneIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    camera_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64, description="区域名，可带人员，如「A1 测试专家01」")
    type: str = Field(default="seat", description="seat=工位区（无人判离岗）；restricted=限制区（多人判越界）")
    polygon: list[list[float]] = Field(min_length=3, description="归一化多边形顶点，至少 3 个")


def _validate(polygon: list[list[float]]):
    for point in polygon:
        if len(point) != 2:
            raise HTTPException(422, "每个顶点必须是 [x, y]")
        x, y = float(point[0]), float(point[1])
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise HTTPException(422, "坐标必须在 0~1 之间")


@router.get("/roi/zones")
def list_zones(camera_id: str | None = Query(None, max_length=64), session=Depends(get_session)):
    sql = "select zone_id, camera_id, name, type, polygon from zones"
    params = {}
    if camera_id:
        sql += " where camera_id = :cid"
        params["cid"] = camera_id
    sql += " order by camera_id, name"
    rows = session.execute(text(sql), params).mappings().all()
    return {"items": [{
        "zone_id": r["zone_id"], "camera_id": r["camera_id"],
        "name": r["name"], "type": r["type"], "polygon": r["polygon"],
    } for r in rows]}


@router.post("/roi/zones", status_code=201)
def upsert_zone(body: ZoneIn, session=Depends(get_session)):
    if body.type not in ALLOWED_TYPES:
        raise HTTPException(422, f"类型仅支持 {'/'.join(ALLOWED_TYPES)}")
    _validate(body.polygon)
    existing = session.execute(text(
        "select zone_id from zones where camera_id = :cid and name = :nm"
    ), {"cid": body.camera_id, "nm": body.name}).mappings().first()

    if existing:
        session.execute(text(
            "update zones set type = :tp, polygon = cast(:pg as jsonb) where zone_id = :zid"
        ), {"tp": body.type, "pg": _json(body.polygon), "zid": existing["zone_id"]})
        zone_id = existing["zone_id"]
        action = "updated"
    else:
        zone_id = str(uuid4())
        session.execute(text("""
            insert into zones (zone_id, session_id, camera_id, name, type, polygon)
            values (:zid, null, :cid, :nm, :tp, cast(:pg as jsonb))
        """), {"zid": zone_id, "cid": body.camera_id, "nm": body.name,
               "tp": body.type, "pg": _json(body.polygon)})
        action = "created"
    session.commit()
    return {"zone_id": zone_id, "action": action}


@router.delete("/roi/zones/{zone_id}", status_code=204)
def delete_zone(zone_id: str, session=Depends(get_session)):
    session.execute(text("delete from zones where zone_id = :zid"), {"zid": zone_id})
    session.commit()


@router.get("/roi/frame")
def get_frame(camera_id: str = Query(..., max_length=64)):
    """从当前推流截一帧当画布背景。摄像头号取自 camera-009 → cam9。"""
    suffix = camera_id.split("-")[-1].lstrip("0") or camera_id[-1]
    stream = f"rtsp://mediamtx:8554/cam{suffix}"
    # 用 OpenCV 取帧，不再依赖系统 ffmpeg（镜像里少装 100 多个 deb 包）
    import cv2
    capture = cv2.VideoCapture(stream, cv2.CAP_FFMPEG)
    try:
        capture.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8000)
        capture.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 8000)
        if not capture.isOpened():
            raise HTTPException(504, "取帧失败：无法连接推流")
        ok, frame = capture.read()
        if not ok or frame is None:
            raise HTTPException(504, "取帧失败：读取画面超时")
        encoded, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not encoded:
            raise HTTPException(504, "取帧失败：编码失败")
        return Response(content=buffer.tobytes(), media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})
    finally:
        capture.release()


def _json(value) -> str:
    import json
    return json.dumps(value)
