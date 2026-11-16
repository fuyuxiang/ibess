"""历史视频审计：任务创建、进度查询、发现清单。

与实时事件的边界：
- 审计发现只落在 video_audits / audit_findings，不写 events 表
- 否则历史录像里的"违规"会混进当天的告警流，冷却、去重、取证全部错乱
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.db import get_session

router = APIRouter(tags=["video-audits"])

VIDEO_ROOT = Path(os.getenv("AUDIT_VIDEO_ROOT", "/videos"))
ALLOWED_SUFFIX = (".mp4", ".mov", ".mkv", ".avi")


class AuditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_name: str = Field(min_length=1, max_length=256, description="素材目录下的相对路径")


def _shape_run(row) -> dict:
    return {
        "audit_id": row["audit_id"],
        "file_name": row["file_name"],
        "status": row["status"],
        "progress": int(row["progress"] or 0),
        "findings_count": int(row["findings_count"] or 0),
        "duration_sec": float(row["duration_sec"]) if row["duration_sec"] else None,
        "error": row["error"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
    }


@router.get("/video-audits/sources")
def list_sources():
    """可审计的素材清单（素材目录下的一级/二级视频文件）。"""
    if not VIDEO_ROOT.exists():
        return {"items": [], "root": str(VIDEO_ROOT)}
    items = []
    for path in sorted(VIDEO_ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in ALLOWED_SUFFIX:
            continue
        # 只列两层，避免把深目录里的临时文件也带出来
        rel = path.relative_to(VIDEO_ROOT)
        if len(rel.parts) > 3:
            continue
        items.append({
            "file_name": rel.as_posix(),
            "size_mb": round(path.stat().st_size / 1024 / 1024, 1),
        })
    return {"items": items, "root": str(VIDEO_ROOT)}


@router.post("/video-audits", status_code=201)
def create_audit(body: AuditIn, session=Depends(get_session)):
    name = body.file_name.strip()
    if ".." in name or name.startswith("/"):
        raise HTTPException(422, "非法文件名")
    if not name.lower().endswith(ALLOWED_SUFFIX):
        raise HTTPException(422, "仅支持 mp4/mov/mkv/avi")
    if not (VIDEO_ROOT / name).exists():
        raise HTTPException(404, f"素材不存在：{name}")

    audit_id = str(uuid4())
    session.execute(text("""
        insert into video_audits (audit_id, file_name, file_path, status)
        values (:aid, :fn, :fp, 'queued')
    """), {"aid": audit_id, "fn": name, "fp": str(VIDEO_ROOT / name)})
    session.commit()
    return {"audit_id": audit_id, "status": "queued", "file_name": name}


@router.get("/video-audits")
def list_audits(limit: int = Query(20, ge=1, le=100), session=Depends(get_session)):
    rows = session.execute(text("""
        select audit_id, file_name, status, progress, findings_count, duration_sec, error, created_at, finished_at
        from video_audits order by created_at desc limit :lim
    """), {"lim": limit}).mappings().all()
    return {"items": [_shape_run(r) for r in rows]}


@router.get("/video-audits/{audit_id}")
def get_audit(audit_id: str, session=Depends(get_session)):
    row = session.execute(text("""
        select audit_id, file_name, status, progress, findings_count, duration_sec, error, created_at, finished_at
        from video_audits where audit_id = :aid
    """), {"aid": audit_id}).mappings().first()
    if row is None:
        raise HTTPException(404, "审计任务不存在")

    findings = session.execute(text("""
        select at_ms, event_type, risk_level, confidence, person_count, phone_count, snapshot_key, reason
        from audit_findings where audit_id = :aid order by at_ms
    """), {"aid": audit_id}).mappings().all()

    payload = _shape_run(row)
    payload["findings"] = [{
        "at_ms": int(f["at_ms"]),
        "at_sec": round(int(f["at_ms"]) / 1000, 1),
        "event_type": f["event_type"],
        "risk_level": f["risk_level"],
        "confidence": float(f["confidence"]) if f["confidence"] is not None else None,
        "person_count": f["person_count"],
        "phone_count": f["phone_count"],
        # 截图与证据同桶，浏览器可直接取
        "snapshot_url": f"/api/evidence/keyframes/object?key={quote(str(f['snapshot_key']))}" if f["snapshot_key"] else None,
        "reason": f["reason"],
    } for f in findings]
    return payload


@router.delete("/video-audits/{audit_id}", status_code=204)
def delete_audit(audit_id: str, session=Depends(get_session)):
    session.execute(text("delete from audit_findings where audit_id = :aid"), {"aid": audit_id})
    session.execute(text("delete from video_audits where audit_id = :aid"), {"aid": audit_id})
    session.commit()
