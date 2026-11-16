"""9.4 离线质检任务接口：建任务（冻结输入）、列表 / 详情、取消、重试、报告。

建任务时冻结的是「判定要用到的输入」：4.3 绑定时间轴上每段对应的完整阶段快照、3.3 区域配置当前版本、4.1 素材的相对路径与 SHA256；
代码版本（模块源码摘要、权重摘要）由 worker 第一次运行时写进 frozen.components，之后对不上就 failed，不静默换版本。
离线结果只存在任务行里，本接口不写 events / risk_clues / Redis。入口由 10.1 以 prefix="/api" 挂载。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.asset_models import StageAsset
from app.auth import AuthRoute
from app.db import get_session as get_db
from app.stage_media_binding_models import StageMediaBinding as MediaBinding
from app.models import Decision, EvalSession, Event
from app.offline_visual_models import CONTRACT_VERSION, RETRYABLE_STATUSES, TERMINAL_STATUSES, OfflineQaJob, utcnow
from app.phase_config import list_timeline
from app.report_builder import MATCH_TOLERANCE_MS, build_report, render_html
from app.zone_config_models import SourceZoneConfig

router = APIRouter(route_class=AuthRoute)

DEFAULT_SAMPLE_FPS = 2.0
LEASE_SECONDS = int(os.environ.get("OFFLINE_LEASE_SECONDS", "120"))      # 与 worker 同一个环境项；过期的 running 由接口直接取消
SYSTEM_REVIEWERS = ("system",)
SYSTEM_ACTIONS = ("auto_close",)
SNAPSHOT_KEYS = ("contract_version", "project_id", "room_id", "session_id", "source_id", "stage_snapshot_id", "phase_id", "phase_version",
                 "effective_at", "enabled_scenes", "always_on_scenes", "assets", "rule_versions", "exceptions")

Id = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
JobId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=36)]


class JobIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: Id
    binding_id: JobId
    rerun_of: Optional[JobId] = None
    sample_fps: Optional[float] = Field(default=None, gt=0, le=10)      # 不给时新任务取默认，重跑时沿用原任务


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _admin(request: Request) -> str:
    admin = getattr(request.state, "admin", None)
    if not isinstance(admin, dict) or admin.get("role") != "admin" or not admin.get("username"):
        raise HTTPException(401, "请先登录管理员账号")
    return admin["username"]


def _iso(moment: Optional[datetime]) -> Optional[str]:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _digest(body: JobIn) -> str:
    canonical = json.dumps({"binding_id": body.binding_id, "rerun_of": body.rerun_of, "sample_fps": body.sample_fps}, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def job_out(row: OfflineQaJob, *, include_result: bool = False) -> dict:
    frozen = row.frozen or {}
    out = {
        "contract_version": row.contract_version, "job_id": row.job_id, "request_id": row.request_id, "session_id": row.session_id,
        "project_id": row.project_id, "room_id": row.room_id, "source_id": row.source_id, "binding_id": row.binding_id, "asset_id": row.asset_id,
        "status": row.status, "cancel_requested": row.cancel_requested, "progress_ms": row.progress_ms,
        "duration_ms": frozen.get("duration_ms"), "attempts": row.attempts, "max_attempts": row.max_attempts, "error": row.error,
        "rerun_of": frozen.get("rerun_of"), "versions_pinned": frozen.get("components") is not None,
        "created_by": row.created_by, "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at), "finished_at": _iso(row.finished_at),
    }
    if include_result:
        out["frozen"] = copy.deepcopy(frozen)
        out["result"] = copy.deepcopy(row.result)
    return out


def _load(db: DbSession, job_id: str, *, lock: bool = False) -> OfflineQaJob:
    statement = select(OfflineQaJob).where(OfflineQaJob.job_id == job_id)
    row = db.scalar(statement.with_for_update() if lock else statement)
    if row is None:
        raise _error(404, "JOB_NOT_FOUND", "离线任务不存在")
    return row


def _freeze(db: DbSession, binding: MediaBinding, asset: StageAsset, sample_fps: float) -> dict:
    """时间轴每段换成完整快照（快照只追加不改，按 id 取即固定）；区域配置取当前版本。"""
    snapshots = {s["stage_snapshot_id"]: {k: s.get(k) for k in SNAPSHOT_KEYS} for s in list_timeline(db, binding.session_id)}
    segments = []
    for segment in binding.timeline or []:
        snapshot_id = segment.get("stage_snapshot_id")
        if snapshot_id is not None and snapshot_id not in snapshots:
            raise _error(409, "SNAPSHOT_MISSING", "绑定时间轴引用的阶段快照不存在")
        segments.append({"from_ms": segment.get("from_ms"), "to_ms": segment.get("to_ms"), "binding_status": segment.get("binding_status"),
                         "snapshot": copy.deepcopy(snapshots[snapshot_id]) if snapshot_id else None})
    zone = db.scalar(select(SourceZoneConfig).where(SourceZoneConfig.session_id == binding.session_id, SourceZoneConfig.source_id == binding.source_id))
    return {
        "media_started_at": _iso(binding.started_at),
        "duration_ms": binding.duration_ms if binding.duration_ms is not None else asset.duration_ms,
        "asset": {"asset_id": asset.asset_id, "relative_path": asset.relative_path, "sha256": asset.sha256, "duration_ms": asset.duration_ms},
        "segments": segments,
        "zone_config": {"config_version": zone.config_version, "zones": copy.deepcopy(zone.zones or []),
                        "entry_lines": copy.deepcopy(zone.entry_lines or [])} if zone else None,
        # 规则上下文事实（6.3 授权内容、6.2 工位身份）：与 10.1 实时入口传给 dispatch 的 context 同形状。
        # 6.3 / 6.2 尚未交付，先冻结为空；快照引用了例外时规则按「事实缺失」出 insufficient，报告限制里写明
        "facts": {"authorizations": [], "seat_identities": {}, "identities": {}, "track_seats": {}},
        "components": None,
        "sample_fps": sample_fps,
        "rerun_of": None,
    }


# ---- 接口 ----
@router.post("/offline-qa/jobs", status_code=201)
def create_job(body: JobIn, request: Request, response: Response, db: DbSession = Depends(get_db)):
    actor = _admin(request)
    digest = _digest(body)
    existing = db.scalar(select(OfflineQaJob).where(OfflineQaJob.request_id == body.request_id))
    if existing is not None:
        if existing.request_digest != digest:
            raise _error(409, "REQUEST_CONFLICT", "同一 request_id 已用于内容不同的请求")
        response.status_code = 200
        return job_out(existing)
    binding = db.scalar(select(MediaBinding).where(MediaBinding.binding_id == body.binding_id))
    if binding is None:
        raise _error(404, "BINDING_NOT_FOUND", "媒体绑定不存在")
    if db.scalar(select(EvalSession).where(EvalSession.session_id == binding.session_id)) is None:
        raise _error(404, "SESSION_NOT_FOUND", "场次不存在")
    asset = db.scalar(select(StageAsset).where(StageAsset.session_id == binding.session_id, StageAsset.asset_id == binding.asset_id)) \
        if binding.asset_id else None
    if asset is None or asset.asset_type != "video":
        raise _error(422, "ASSET_REQUIRED", "离线质检只接受绑定了已登记 video 素材的媒体绑定（单源录像）")
    if body.rerun_of:
        original = db.scalar(select(OfflineQaJob).where(OfflineQaJob.job_id == body.rerun_of))
        if original is None:
            raise _error(404, "JOB_NOT_FOUND", "rerun_of 指向的任务不存在")
        if original.binding_id != binding.binding_id:
            raise _error(422, "RERUN_MISMATCH", "重跑必须针对原任务的同一媒体绑定")
        if (original.frozen or {}).get("components") is None:
            raise _error(409, "RERUN_NOT_PINNED", "原任务还没跑过、代码版本未固定，重跑无版本可沿用；等原任务运行后再重跑")
        if body.sample_fps is not None and body.sample_fps != (original.frozen or {}).get("sample_fps"):
            raise _error(422, "RERUN_MISMATCH", "重跑沿用原任务的采样帧率，不能另给")
        frozen = copy.deepcopy(original.frozen)           # 原样沿用冻结输入与已固定的代码版本，不读当前快照
        frozen["rerun_of"] = original.job_id
    else:
        frozen = _freeze(db, binding, asset, body.sample_fps or DEFAULT_SAMPLE_FPS)
    row = OfflineQaJob(request_id=body.request_id, request_digest=digest, session_id=binding.session_id, project_id=binding.project_id,
                       room_id=binding.room_id, source_id=binding.source_id, binding_id=binding.binding_id, asset_id=asset.asset_id,
                       status="queued", frozen=frozen, created_by=actor, contract_version=CONTRACT_VERSION)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raced = db.scalar(select(OfflineQaJob).where(OfflineQaJob.request_id == body.request_id))
        if raced is not None and raced.request_digest == digest:
            response.status_code = 200                 # 同一请求并发提交：后到的拿原任务
            return job_out(raced)
        raise _error(409, "REQUEST_CONFLICT", "同一 request_id 已被并发提交") from None
    db.refresh(row)
    return job_out(row)


@router.get("/offline-qa/jobs")
def list_jobs(request: Request, session_id: Optional[JobId] = None, db: DbSession = Depends(get_db)):
    _admin(request)
    statement = select(OfflineQaJob).order_by(OfflineQaJob.created_at.desc(), OfflineQaJob.job_id)
    if session_id:
        statement = statement.where(OfflineQaJob.session_id == session_id)
    return {"jobs": [job_out(row) for row in db.scalars(statement).all()]}


@router.get("/offline-qa/jobs/{job_id}")
def get_job(job_id: JobId, request: Request, db: DbSession = Depends(get_db)):
    _admin(request)
    return job_out(_load(db, job_id))


@router.post("/offline-qa/jobs/{job_id}/cancel")
def cancel_job(job_id: JobId, request: Request, db: DbSession = Depends(get_db)):
    _admin(request)
    row = _load(db, job_id, lock=True)
    now = utcnow()
    claim_at = row.claim_at.replace(tzinfo=timezone.utc) if row.claim_at is not None and row.claim_at.tzinfo is None else row.claim_at
    stale = row.status == "running" and (claim_at is None or claim_at <= now - timedelta(seconds=LEASE_SECONDS))
    if row.status == "queued" or stale:
        # 排队中的、以及租约已过期（worker 已不在）的运行中任务：直接取消，不等一个可能永远不来的 worker
        row.status, row.finished_at, row.cancel_requested = "cancelled", now, False
        row.claim_id, row.claimed_by, row.claim_at = None, None, None
    elif row.status == "running":
        row.cancel_requested = True                    # worker 在下一次心跳时停下并置 cancelled
    else:
        raise _error(409, "JOB_NOT_CANCELLABLE", f"任务已是终态 {row.status}")
    row.updated_at = now
    db.commit()
    db.refresh(row)
    return job_out(row)


@router.post("/offline-qa/jobs/{job_id}/retry")
def retry_job(job_id: JobId, request: Request, db: DbSession = Depends(get_db)):
    _admin(request)
    row = _load(db, job_id, lock=True)
    if row.status not in RETRYABLE_STATUSES:
        raise _error(409, "JOB_NOT_RETRYABLE", f"只有 failed / cancelled 能重试，当前 {row.status}")
    if row.status == "failed" and isinstance(row.error, dict) and row.error.get("retryable") is False:
        raise _error(409, "JOB_NOT_RETRYABLE", f"失败原因 {row.error.get('code')} 重试也不会变（如版本或素材摘要不符），请处理原因后新建任务")
    now = utcnow()
    row.status, row.error, row.result, row.cancel_requested = "queued", None, None, False
    row.progress_ms, row.attempts, row.finished_at, row.updated_at = 0, 0, None, now   # 冻结清单不动
    db.commit()
    db.refresh(row)
    return job_out(row)


def _report(db: DbSession, row: OfflineQaJob) -> dict:
    frozen = row.frozen or {}
    events, decisions = [], {}
    started = frozen.get("media_started_at")
    if row.status == "succeeded" and started:
        start = datetime.fromisoformat(started.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)   # events.occurred_at 是无时区 UTC
        end = start + timedelta(milliseconds=int(frozen.get("duration_ms") or 0))
        margin = timedelta(milliseconds=MATCH_TOLERANCE_MS)             # 两端各放宽对照容差，素材首尾附近的实时事件也能对上
        start, end = start - margin, end + margin
        rows = db.scalars(select(Event).where(Event.session_id == row.session_id, Event.camera_id == row.source_id,
                                              Event.occurred_at >= start, Event.occurred_at <= end)).all()
        events = [{"event_id": e.event_id, "skill_code": e.skill_code, "skill_version": e.skill_version, "occurred_at": e.occurred_at,
                   "risk_level": e.risk_level, "status": e.status, "reason": e.reason} for e in rows]
        ids = [e["event_id"] for e in events]
        if ids:
            for decision in db.scalars(select(Decision).where(Decision.event_id.in_(ids)).order_by(Decision.decided_at, Decision.id)).all():
                # 系统自动关闭（设备恢复关缺口事件）不是人工决定，单列，不进「人工决定」
                kind = "system" if decision.reviewer_id in SYSTEM_REVIEWERS or decision.action in SYSTEM_ACTIONS else "human"
                decisions.setdefault(decision.event_id, {})[kind] = {
                    "action": decision.action, "reviewer_id": decision.reviewer_id, "note": decision.note,
                    "decided_at": _iso(decision.decided_at)}                                      # 按时间升序覆盖，留下各类最新一条
    job = job_out(row, include_result=True)
    job["rerun_of"] = frozen.get("rerun_of")
    return build_report(job, events, decisions)


@router.get("/offline-qa/jobs/{job_id}/report")
def get_report(job_id: JobId, request: Request, db: DbSession = Depends(get_db)):
    _admin(request)
    return _report(db, _load(db, job_id))


@router.get("/offline-qa/jobs/{job_id}/report.html", response_class=HTMLResponse)
def get_report_html(job_id: JobId, request: Request, db: DbSession = Depends(get_db)):
    _admin(request)
    return HTMLResponse(render_html(_report(db, _load(db, job_id))))
