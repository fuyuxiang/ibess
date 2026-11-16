"""执行卡 4.3：素材绑定的校验、时间轴拆分、幂等与时间换算。只 flush 不 commit，调用方管事务。

时间轴：按场次的阶段快照（2.1 phase_snapshots）生效时间把 [started_at, started_at + duration] 切段；
第一份快照生效之前的部分是 unbound（快照 null），不猜、不借后面的快照。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.asset_models import StageAsset
from app.audio_models import AudioRecording
from app.stage_media_binding_models import BINDING_KINDS, CONTRACT_VERSION, StageMediaBinding as MediaBinding
from app.models import EvalSession
from app.phase_models import PhaseBinding, PhaseSnapshot

UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
ASSET_TYPE_FOR_KIND = {"recording": "audio", "scoring": "scoring", "media_run": "video"}


class MediaBindingError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def parse_utc(value) -> datetime:
    if not isinstance(value, str) or not UTC_PATTERN.match(value):
        raise MediaBindingError("INVALID_TIME", "时间必须是 Z 结尾的 UTC ISO 字符串")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise MediaBindingError("INVALID_TIME", "时间不是合法日期") from None


def format_utc(moment: datetime) -> str:
    return _aware(moment).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)   # SQLite 读回无时区


def _text(value, limit=256) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def request_digest(payload: dict) -> str:
    normalized = dict(payload)
    try:
        normalized["started_at"] = format_utc(parse_utc(payload.get("started_at")))   # 02:00:00Z 与 02:00:00.000Z 同一摘要
    except MediaBindingError:
        pass
    canonical = json.dumps({k: normalized.get(k) for k in ("kind", "subject_id", "started_at", "duration_ms", "asset_id")},
                           sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ms_floor(delta: timedelta) -> int:
    return delta // timedelta(milliseconds=1)


def _ms_ceil(delta: timedelta) -> int:
    return -((-delta) // timedelta(milliseconds=1))


# ---------------------------------------------------------------- 时间轴
def build_timeline(snapshots: list[dict], started_at: datetime, duration_ms: Optional[int]) -> list[dict]:
    """snapshots：[{stage_snapshot_id, phase_id, phase_version, effective_at(datetime)}]，按生效时间升序。
    返回 [{from_ms, to_ms, stage_snapshot_id, phase_id, phase_version, binding_status}]；to_ms 为 None 表示时长未知的开放段。"""
    start = _aware(started_at)
    end = start + timedelta(milliseconds=duration_ms) if duration_ms is not None else None
    ordered = sorted(({**s, "effective_at": _aware(s["effective_at"])} for s in snapshots), key=lambda s: (s["effective_at"], s["phase_version"]))
    segments: list[dict] = []
    cursor = start
    current: Optional[dict] = None
    for snapshot in ordered:
        if snapshot["effective_at"] <= start:
            current = snapshot                       # 起点时已生效的最后一份
            continue
        if end is not None and snapshot["effective_at"] >= end:
            break
        _append(segments, current, start, cursor, snapshot["effective_at"])
        cursor = snapshot["effective_at"]
        current = snapshot
    _append(segments, current, start, cursor, end)
    return segments


def _append(segments: list[dict], snapshot: Optional[dict], start: datetime, from_at: datetime, to_at: Optional[datetime]) -> None:
    """快照生效边界向上取整到毫秒：某毫秒只有在快照已生效时才归新段，与 2.1 get_snapshot(effective_at <= occurred_at) 一致。"""
    from_ms = _ms_ceil(from_at - start)
    to_ms = _ms_ceil(to_at - start) if to_at is not None else None
    if to_ms is not None and to_ms <= from_ms:
        return
    segments.append({
        "from_ms": from_ms, "to_ms": to_ms,
        "stage_snapshot_id": snapshot["stage_snapshot_id"] if snapshot else None,
        "phase_id": snapshot["phase_id"] if snapshot else None,
        "phase_version": snapshot["phase_version"] if snapshot else None,
        "binding_status": "bound" if snapshot else "unbound",
    })


def segment_at(timeline: list[dict], media_ms: int) -> Optional[dict]:
    for segment in timeline:
        if segment["from_ms"] <= media_ms and (segment["to_ms"] is None or media_ms < segment["to_ms"]):
            return segment
    if timeline and timeline[-1]["to_ms"] == media_ms:
        return timeline[-1]
    return None


# ---------------------------------------------------------------- 绑定
def _snapshots(db: Session, session_id: str) -> list[dict]:
    rows = db.scalars(select(PhaseSnapshot).where(PhaseSnapshot.session_id == session_id).order_by(PhaseSnapshot.effective_at, PhaseSnapshot.phase_version)).all()
    return [{"stage_snapshot_id": r.stage_snapshot_id, "phase_id": r.phase_id, "phase_version": r.phase_version, "effective_at": r.effective_at} for r in rows]


def to_dict(row: MediaBinding) -> dict:
    return {
        "contract_version": row.contract_version, "binding_id": row.binding_id, "session_id": row.session_id,
        "project_id": row.project_id, "room_id": row.room_id, "source_id": row.source_id, "kind": row.kind,
        "subject_id": row.subject_id, "asset_id": row.asset_id, "started_at": format_utc(row.started_at),
        "duration_ms": row.duration_ms, "timeline": list(row.timeline or []), "request_id": row.request_id,
        "created_by": row.created_by, "created_at": format_utc(row.created_at),
    }


def bind(db: Session, *, session_id: str, request_id: str, kind: str, subject_id: str, started_at: str,
         duration_ms: Optional[int], asset_id: Optional[str], actor: str) -> tuple[dict, bool]:
    """返回 (记录, created)。幂等：request_id 相同且正文相同 → 原记录；(kind, subject) 相同且内容相同 → 原记录。"""
    if not _text(request_id, 64) or not _text(subject_id) or kind not in BINDING_KINDS or not _text(actor, 128):
        raise MediaBindingError("INVALID_BINDING", "request_id / kind / subject_id / actor 非法")
    if duration_ms is not None and (isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 1):
        raise MediaBindingError("INVALID_BINDING", "duration_ms 必须是正整数毫秒")
    start = parse_utc(started_at)
    payload = {"kind": kind, "subject_id": subject_id, "started_at": started_at, "duration_ms": duration_ms, "asset_id": asset_id}
    raw_digest = request_digest(payload)
    normalized = dict(payload, duration_ms=_default_duration(db, session_id, kind, subject_id, asset_id) if duration_ms is None else duration_ms)
    digest = request_digest(normalized)
    existing = db.scalar(select(MediaBinding).where(MediaBinding.request_id == request_id))
    if existing is not None:
        if existing.session_id != session_id or existing.request_digest not in (raw_digest, digest):
            raise MediaBindingError("REQUEST_CONFLICT", "相同 request_id 的正文不同")
        return to_dict(existing), False
    session = db.scalar(select(EvalSession).where(EvalSession.session_id == session_id))
    if session is None:
        raise MediaBindingError("SESSION_NOT_FOUND", "场次不存在")
    if session.status == "closed":
        raise MediaBindingError("SESSION_CLOSED", "已关闭场次不能绑定素材")
    binding = db.scalar(select(PhaseBinding).where(PhaseBinding.session_id == session_id))
    if binding is None or not binding.active:
        raise MediaBindingError("BINDING_REQUIRED", "场次尚未绑定项目、评标室和媒体源（2.1）")
    if kind == "recording":
        recording = db.scalar(select(AudioRecording).where(AudioRecording.recording_id == subject_id))
        if recording is None:
            raise MediaBindingError("RECORDING_NOT_FOUND", "录音不存在")
    if kind == "scoring" and asset_id is None:
        raise MediaBindingError("INVALID_BINDING", "评分素材绑定必须给 asset_id")
    if asset_id is not None:
        asset = db.scalar(select(StageAsset).where(StageAsset.session_id == session_id, StageAsset.asset_id == asset_id))
        if asset is None:
            raise MediaBindingError("ASSET_NOT_FOUND", "素材未在本场次登记（4.1）")
        if asset.asset_type != ASSET_TYPE_FOR_KIND[kind]:
            raise MediaBindingError("ASSET_TYPE_MISMATCH", f"{kind} 绑定只能用 {ASSET_TYPE_FOR_KIND[kind]} 素材")
    duration_ms = normalized["duration_ms"]
    same_subject = db.scalar(select(MediaBinding).where(MediaBinding.kind == kind, MediaBinding.subject_id == subject_id))
    if same_subject is not None:
        if same_subject.request_digest == digest and same_subject.session_id == session_id:
            return to_dict(same_subject), False
        raise MediaBindingError("BINDING_CONFLICT", "该素材已绑定为不同内容，不覆盖")
    timeline = build_timeline(_snapshots(db, session_id), start, duration_ms)
    row = MediaBinding(session_id=session_id, project_id=binding.project_id, room_id=binding.room_id, source_id=binding.source_id,
                       kind=kind, subject_id=subject_id, asset_id=asset_id, started_at=start, duration_ms=duration_ms,
                       timeline=timeline, request_id=request_id, request_digest=digest, created_by=actor.strip(),
                       contract_version=CONTRACT_VERSION)
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raced = db.scalar(select(MediaBinding).where(MediaBinding.kind == kind, MediaBinding.subject_id == subject_id))
        if raced is not None and raced.request_digest == digest and raced.session_id == session_id:
            return to_dict(raced), False               # 并发写同内容：幂等返回先到的那条
        raise MediaBindingError("BINDING_CONFLICT", "并发绑定冲突，请重新读取") from exc
    return to_dict(row), True


def _default_duration(db: Session, session_id: str, kind: str, subject_id: str, asset_id: Optional[str]) -> Optional[int]:
    """未给时长：录音取 0.3 的 duration_ms，否则取 4.1 素材的 duration_ms；查不到就 None（后面会报对应 404）。"""
    if kind == "recording":
        recording = db.scalar(select(AudioRecording).where(AudioRecording.recording_id == subject_id))
        if recording is not None:
            return recording.duration_ms
    if asset_id is not None:
        asset = db.scalar(select(StageAsset).where(StageAsset.session_id == session_id, StageAsset.asset_id == asset_id))
        if asset is not None:
            return asset.duration_ms
    return None


def list_bindings(db: Session, session_id: str) -> list[dict]:
    if db.scalar(select(EvalSession.session_id).where(EvalSession.session_id == session_id)) is None:
        raise MediaBindingError("SESSION_NOT_FOUND", "场次不存在")
    rows = db.scalars(select(MediaBinding).where(MediaBinding.session_id == session_id).order_by(MediaBinding.created_at, MediaBinding.binding_id)).all()
    return [to_dict(r) for r in rows]


def locate(row: MediaBinding, *, media_ms: Optional[int] = None, at: Optional[str] = None) -> dict:
    """相对毫秒 ↔ UTC 双向换算，并给出所在时间轴段；二者给且只给一个。"""
    if (media_ms is None) == (at is None):
        raise MediaBindingError("INVALID_QUERY", "media_ms 与 at 二选一")
    start = _aware(row.started_at)
    if media_ms is not None:
        if isinstance(media_ms, bool) or not isinstance(media_ms, int) or media_ms < 0:
            raise MediaBindingError("INVALID_QUERY", "media_ms 必须是非负整数")
        if row.duration_ms is not None and media_ms > row.duration_ms:
            raise MediaBindingError("OUT_OF_RANGE", "相对时间超出素材时长")
        moment = start + timedelta(milliseconds=media_ms)
    else:
        moment = parse_utc(at)
        if moment < start or (row.duration_ms is not None and moment > start + timedelta(milliseconds=row.duration_ms)):
            raise MediaBindingError("OUT_OF_RANGE", "时刻不在素材时间范围内")
        media_ms = _ms_floor(moment - start)
    segment = segment_at(list(row.timeline or []), media_ms)
    return {"binding_id": row.binding_id, "kind": row.kind, "subject_id": row.subject_id, "media_ms": media_ms,
            "occurred_at": format_utc(moment), "segment": segment,
            "binding_status": segment["binding_status"] if segment else "unbound"}


def recording_context(db: Session, recording_id: str) -> dict:
    """8.x 读：有绑定给全量上下文；没有就如实说缺什么，不猜最近场次。"""
    recording = db.scalar(select(AudioRecording).where(AudioRecording.recording_id == recording_id))
    if recording is None:
        raise MediaBindingError("RECORDING_NOT_FOUND", "录音不存在")
    row = db.scalar(select(MediaBinding).where(MediaBinding.kind == "recording", MediaBinding.subject_id == recording_id))
    if row is None:
        return {"recording_id": recording_id, "binding_status": "unbound", "binding": None,
                "missing": ["场次绑定（项目 / 评标室 / 场次）", "素材起点 UTC（媒体发生时间）", "阶段快照"],
                "recording_status": recording.status, "duration_ms": recording.duration_ms}
    value = to_dict(row)
    missing = []
    if not any(seg["binding_status"] == "bound" for seg in value["timeline"]):
        missing.append("阶段快照（绑定时该场次还没有生效的阶段配置）")
    return {"recording_id": recording_id, "binding_status": "bound" if not missing else "unbound", "binding": value,
            "missing": missing, "recording_status": recording.status, "duration_ms": recording.duration_ms}
