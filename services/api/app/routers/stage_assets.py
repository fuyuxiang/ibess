"""4.1 素材清单及登记 API：登记授权素材元数据，服务端自己读文件、算摘要、探测元信息。

「播的就是授权的那一份」只能靠登记时算过的摘要来证明，所以清单只是声明，服务端说了算：
只在 STAGE_ASSETS_ROOT 指向的素材根内找文件；路径四道防线（契约正则、解析、根目录边界、符号链接）；
流式算 SHA256 比对；video / audio 用 ffprobe 读时长与流信息，评分 JSON 只做解析。
入口由 10.1 以 prefix="/api" 挂载；ffprobe 与素材目录挂载由 10.2 提供。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal, Optional, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.asset_models import CONTRACT_VERSION, StageAsset
from app.auth import AuthRoute
from app.db import get_session as get_db
from app.models import EvalSession
from app.phase_models import PhaseBinding

router = APIRouter(route_class=AuthRoute)

# 契约 asset-manifest.schema.json 的 relative_path 正则：禁绝对路径、盘符、`..` 段、反斜线
RELATIVE_PATH = re.compile(r"^(?!/)(?![A-Za-z]:)(?!.*(?:^|/)\.\.(?:/|$))[^\\]+$")
DURATION_TOLERANCE_MS = 1000          # ffprobe 时长与清单时长的容差（工程初值）
MAX_DURATION_MS = 2_147_483_647       # 列是 Integer（PostgreSQL int4），入参上限与之对齐
SCORING_MAX_BYTES = 16 * 1024 * 1024
HASH_CHUNK = 1024 * 1024
FFPROBE_TIMEOUT_SECONDS = 20
STREAM_FIELDS = ("codec_type", "codec_name", "width", "height", "sample_rate", "channels")

Id = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
Notes = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)]
SessionId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=36)]


class AssetManifestIn(BaseModel):
    """契约 AssetManifest；路径与时长的业务规则在处理函数里判，便于返回带 code 的错误。"""
    model_config = ConfigDict(extra="forbid")
    contract_version: Literal["2.0.0"]
    asset_id: Id
    asset_type: Literal["video", "audio", "scoring"]
    relative_path: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    duration_ms: Optional[int] = Field(default=None, strict=True, ge=0, le=MAX_DURATION_MS)   # 严格整数，不收 "60000" / true
    authorization_source: Id
    visibility_notes: Notes


@dataclass
class ProbeResult:
    status: str                             # probed | unavailable | failed | not_applicable
    tool: str = "ffprobe"
    duration_ms: Optional[int] = None
    format_name: Optional[str] = None
    streams: list = field(default_factory=list)
    error_code: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


class Prober(Protocol):
    def probe(self, path: Path) -> ProbeResult: ...


class FfprobeProber:
    """调 ffprobe 读容器时长与流信息。找不到可执行文件如实返回 unavailable，不冒充已探测。"""

    def __init__(self, command: Optional[list[str]] = None, timeout_seconds: float = FFPROBE_TIMEOUT_SECONDS) -> None:
        if command is None:
            found = shutil.which("ffprobe")
            command = [found] if found else None
        self.command = list(command) if command else None
        self.timeout_seconds = timeout_seconds

    def probe(self, path: Path) -> ProbeResult:
        if not self.command:
            return ProbeResult(status="unavailable", error_code="FFPROBE_NOT_FOUND")
        args = [*self.command, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
        try:
            run = subprocess.run(args, capture_output=True, timeout=self.timeout_seconds, check=False)
        except subprocess.TimeoutExpired:
            return ProbeResult(status="failed", error_code="FFPROBE_TIMEOUT")
        except OSError:
            return ProbeResult(status="failed", error_code="FFPROBE_EXEC_ERROR")
        if run.returncode != 0:
            return ProbeResult(status="failed", error_code=f"FFPROBE_EXIT_{run.returncode}")
        try:
            data = json.loads(run.stdout.decode("utf-8", errors="replace"))
        except ValueError:
            return ProbeResult(status="failed", error_code="FFPROBE_BAD_OUTPUT")
        if not isinstance(data, dict):
            return ProbeResult(status="failed", error_code="FFPROBE_BAD_OUTPUT")
        fmt = data.get("format") if isinstance(data.get("format"), dict) else {}
        duration_ms: Optional[int] = None
        raw = fmt.get("duration")
        if raw is not None:
            try:
                duration_ms = int(round(float(raw) * 1000))
            except (TypeError, ValueError):
                duration_ms = None
        streams = [{key: stream.get(key) for key in STREAM_FIELDS if stream.get(key) is not None}
                   for stream in (data.get("streams") or []) if isinstance(stream, dict)]
        return ProbeResult(status="probed", duration_ms=duration_ms, format_name=fmt.get("format_name"), streams=streams)


# ---- 依赖 ----
def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def get_assets_root() -> Path:
    """素材根目录只认环境变量，必须是存在的绝对路径；没有默认目录。"""
    raw = os.environ.get("STAGE_ASSETS_ROOT", "").strip()
    if not raw:
        raise _error(503, "ASSET_ROOT_UNCONFIGURED", "素材根目录未配置（STAGE_ASSETS_ROOT）")
    root = Path(raw)
    if not root.is_absolute() or not root.is_dir():
        raise _error(503, "ASSET_ROOT_UNCONFIGURED", "素材根目录必须是存在的绝对路径")
    return root


def get_prober() -> Prober:
    return FfprobeProber()


# ---- 内部 ----
def _admin(request: Request) -> str:
    admin = getattr(request.state, "admin", None)
    if not isinstance(admin, dict) or admin.get("role") != "admin" or not admin.get("username"):
        raise HTTPException(401, "请先登录管理员账号")
    return admin["username"]


def _load_session(db: DbSession, session_id: str) -> EvalSession:
    session = db.scalar(select(EvalSession).where(EvalSession.session_id == session_id))
    if session is None:
        raise _error(404, "SESSION_NOT_FOUND", "场次不存在")
    return session


def _locate(root: Path, relative: str) -> Path:
    """五道防线：契约正则 → 逐段检查 → 先判根目录边界（不看文件存不存在，免得 404 / 422 之差泄露根外文件）
    → 解析（含符号链接）后再判一次边界 → 路径必须是规范写法（大小写、短名、尾点等别名一律拒）。错误不回显根目录。"""
    if not RELATIVE_PATH.match(relative):
        raise _error(422, "PATH_INVALID", "relative_path 只能是相对素材根的 / 分隔路径，不能含 ..、盘符或反斜线")
    parts = relative.split("/")
    if any(part in ("", ".", "..") or ":" in part for part in parts):
        raise _error(422, "PATH_INVALID", "relative_path 含空段、. 或 .. 段，或段里有冒号")
    root_resolved = root.resolve()
    candidate = root_resolved.joinpath(*parts)
    try:
        lexical = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        raise _error(422, "PATH_INVALID", "路径无法解析") from None
    if not lexical.is_relative_to(root_resolved):
        raise _error(422, "PATH_OUTSIDE_ROOT", "路径解析后落在素材根目录之外")
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        raise _error(404, "FILE_NOT_FOUND", "素材文件不存在") from None
    except (OSError, RuntimeError):
        raise _error(422, "PATH_INVALID", "路径无法解析") from None
    if not resolved.is_relative_to(root_resolved):
        raise _error(422, "PATH_OUTSIDE_ROOT", "路径解析后落在素材根目录之外")
    if not resolved.is_file():
        raise _error(422, "NOT_A_FILE", "路径不是文件")
    # resolve() preserves caller casing on some case-insensitive filesystems.
    # Check actual directory entries so the stored manifest has one canonical path.
    cursor = root_resolved
    try:
        for part in parts:
            if part not in {entry.name for entry in cursor.iterdir()}:
                raise _error(422, "PATH_NOT_CANONICAL", "relative_path 必须使用文件的实际大小写")
            cursor = cursor / part
    except OSError:
        raise _error(422, "FILE_UNREADABLE", "素材路径无法读取") from None
    if resolved.relative_to(root_resolved).as_posix() != relative:
        raise _error(422, "PATH_NOT_CANONICAL", "relative_path 必须与文件的规范路径一致（大小写、短名、尾点、根内链接别名都不行）")
    return resolved


def _sha256_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _read_digest(path: Path) -> tuple[str, int]:
    try:
        return _sha256_and_size(path)
    except OSError:
        raise _error(422, "FILE_UNREADABLE", "素材文件无法读取（被占用、无权限或刚被移走）") from None


def _check_scoring(path: Path, size: int) -> None:
    if size > SCORING_MAX_BYTES:
        raise _error(422, "SCORING_INVALID_JSON", "评分文件过大")
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig"))   # 接受 Windows 导出常见的 UTF-8 BOM
    except OSError:
        raise _error(422, "FILE_UNREADABLE", "评分文件无法读取") from None
    except (ValueError, UnicodeDecodeError):
        raise _error(422, "SCORING_INVALID_JSON", "评分文件不是合法 JSON") from None
    if not isinstance(data, dict):
        raise _error(422, "SCORING_INVALID_JSON", "评分文件必须是 JSON 对象")


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _asset_out(row: StageAsset) -> dict:
    probe = dict(row.probe or {})
    return {
        "contract_version": row.contract_version,
        "asset_id": row.asset_id,
        "asset_type": row.asset_type,
        "relative_path": row.relative_path,
        "sha256": row.sha256,
        "duration_ms": row.duration_ms,
        "authorization_source": row.authorization_source,
        "visibility_notes": row.visibility_notes,
        "session_id": row.session_id,
        "size_bytes": row.size_bytes,
        "probe": probe,
        "probe_pending": probe.get("status") == "unavailable",
        "registered_by": row.registered_by,
        "registered_at": _iso(row.registered_at),
    }


def _same_manifest(row: StageAsset, body: AssetManifestIn) -> bool:
    return (row.asset_type, row.relative_path, row.sha256, row.duration_ms, row.authorization_source, row.visibility_notes) == \
        (body.asset_type, body.relative_path, body.sha256, body.duration_ms, body.authorization_source, body.visibility_notes)


# ---- 接口 ----
@router.post("/sessions/{session_id}/assets/register", status_code=201)
def register_asset(session_id: SessionId, body: AssetManifestIn, request: Request, response: Response,
                   db: DbSession = Depends(get_db), root: Path = Depends(get_assets_root),
                   prober: Prober = Depends(get_prober)):
    actor = _admin(request)
    session = _load_session(db, session_id)
    if session.status == "closed":
        raise _error(409, "SESSION_CLOSED", "已关闭场次不能登记素材")
    target = _locate(root, body.relative_path)
    digest, size = _read_digest(target)
    if digest != body.sha256:
        raise _error(409, "SHA256_MISMATCH", "文件摘要与清单不一致")
    if body.asset_type == "scoring":
        if body.duration_ms is not None:
            raise _error(422, "SCORING_DURATION", "评分素材没有时长，duration_ms 必须为 null")
        _check_scoring(target, size)
        probe = ProbeResult(status="not_applicable", tool="none")
    else:
        if body.duration_ms is None or body.duration_ms < 1:
            raise _error(422, "DURATION_INVALID", "video / audio 的 duration_ms 必须是不小于 1 的整数毫秒")
        probe = prober.probe(target)
        if probe.status == "failed":
            raise _error(422, "MEDIA_UNREADABLE", f"探测器读不出媒体信息（{probe.error_code}）")
        if probe.status == "probed":
            if probe.duration_ms is None:
                # ffprobe 跑成功却给不出时长（裸码流、截断文件）：清单时长无法核验，不能标成已探测放行
                raise _error(422, "MEDIA_UNREADABLE", "探测器读不出时长（FFPROBE_NO_DURATION）")
            if abs(probe.duration_ms - body.duration_ms) > DURATION_TOLERANCE_MS:
                raise _error(409, "DURATION_MISMATCH", "探测时长与清单时长不一致")
    existing = db.scalar(select(StageAsset).where(StageAsset.session_id == session_id, StageAsset.asset_id == body.asset_id))
    if existing is not None:
        if not _same_manifest(existing, body):
            raise _error(409, "ASSET_CONFLICT", "同一 asset_id 已登记且内容不同，不覆盖")
        # 幂等：同一清单重复登记返回已有记录；上次探测器不可用、这次探测成功，就把探测结果补上（装好 ffprobe 后的恢复路径）
        if (existing.probe or {}).get("status") == "unavailable" and probe.status == "probed":
            existing.probe = probe.to_dict()
            existing.size_bytes = size
            db.commit()
            db.refresh(existing)
        response.status_code = 200
        return _asset_out(existing)
    row = StageAsset(session_id=session_id, asset_id=body.asset_id, asset_type=body.asset_type,
                     relative_path=body.relative_path, sha256=digest, duration_ms=body.duration_ms,
                     authorization_source=body.authorization_source, visibility_notes=body.visibility_notes,
                     size_bytes=size, probe=probe.to_dict(), registered_by=actor, contract_version=CONTRACT_VERSION)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise _error(409, "ASSET_CONFLICT", "同一 asset_id 已被并发登记") from None
    db.refresh(row)
    return _asset_out(row)


@router.get("/sessions/{session_id}/assets")
def list_assets(session_id: SessionId, request: Request, db: DbSession = Depends(get_db)):
    _admin(request)
    _load_session(db, session_id)
    binding = db.scalar(select(PhaseBinding).where(PhaseBinding.session_id == session_id, PhaseBinding.active.is_(True)))
    rows = db.scalars(select(StageAsset).where(StageAsset.session_id == session_id)
                      .order_by(StageAsset.registered_at, StageAsset.asset_id)).all()
    return {
        "session_id": session_id,
        "binding": {"project_id": binding.project_id, "room_id": binding.room_id, "source_id": binding.source_id} if binding else None,
        "assets": [_asset_out(row) for row in rows],
    }
