"""执行卡 9.2：等后段媒体采齐再精确截片；缺就记 missing；失败有限重试；按 evidence_id 幂等。

一个源一个流水线。时间只在 TimeMapper 换算一次：预制素材用 4.2 的运行记录（媒体毫秒 → UTC → 录制秒），
现场流用 occurred_at。录制索引是 ffmpeg -segment_list 写的 CSV（filename,start,end），段关闭才有行，
所以「索引覆盖到窗口末尾」就是「后段已落盘」。索引只读不重写（ffmpeg 持有文件句柄追加写，重写会让它写到旧 inode）；
清理只删段文件，reload 按文件存在过滤。窗口端点夹到素材 / 轮次范围内（素材末尾的事件只截到末尾，不整窗判缺失）。
裁剪器可注入；真实 FfmpegCutter 只拼参数数组。
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol

from service.evidence_adapter import EvidenceRegistrar, build_evidence_ref, sha256_of_file

UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
JOB_STATUSES = ("pending", "ready", "missing", "failed")


def parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not UTC_PATTERN.match(value):
        raise ValueError("时间必须是 Z 结尾的 UTC ISO 字符串")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------- 录制索引
@dataclass(frozen=True)
class Segment:
    filename: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


class SegmentIndex:
    """ffmpeg `-segment_list <csv> -segment_list_type csv` 的索引；只认有 end 的行（段已关闭）；只读，不重写。"""

    def __init__(self, csv_path: Path, cache_dir: Path) -> None:
        self.csv_path = Path(csv_path)
        self.cache_dir = Path(cache_dir)
        self._segments: list[Segment] = []

    def reload(self) -> list[Segment]:
        segments: dict[str, Segment] = {}
        if self.csv_path.exists():
            with self.csv_path.open("r", encoding="utf-8", newline="") as handle:
                for row in csv.reader(handle):
                    if len(row) < 3:
                        continue
                    try:
                        start, end = float(row[1]), float(row[2])
                    except ValueError:
                        continue
                    if end <= start:
                        continue
                    segments[row[0].strip()] = Segment(row[0].strip(), start, end)   # 同名文件取最后一行
        self._segments = sorted((s for s in segments.values() if self.exists(s)), key=lambda s: s.start)
        return self._segments

    @property
    def segments(self) -> list[Segment]:
        return list(self._segments)

    def covered_until(self) -> float:
        return max((s.end for s in self._segments), default=0.0)

    def exists(self, segment: Segment) -> bool:
        path = self.cache_dir / segment.filename
        return path.is_file() and path.stat().st_size > 0

    def segments_for(self, start: float, end: float) -> list[Segment]:
        return [s for s in self._segments if s.end > start and s.start < end]

    def gaps(self, start: float, end: float, *, tolerance: float = 0.05) -> list[tuple[float, float]]:
        """窗口内没有段覆盖的区间（录制秒）。"""
        cursor = start
        gaps: list[tuple[float, float]] = []
        for segment in self.segments_for(start, end):
            if segment.start > cursor + tolerance:
                gaps.append((cursor, min(segment.start, end)))
            cursor = max(cursor, segment.end)
            if cursor >= end:
                break
        if cursor < end - tolerance:
            gaps.append((cursor, end))
        return gaps

    def prune(self, keep_seconds: float, now_seconds: float) -> list[str]:
        """只删录制时间早于 now − keep 的段**文件**；索引 CSV 原样不动（ffmpeg 持有句柄在追加），reload 会按文件存在过滤。"""
        removed = []
        for segment in list(self._segments):
            if segment.end < now_seconds - keep_seconds:
                path = self.cache_dir / segment.filename
                try:
                    if path.exists():
                        path.unlink()
                    removed.append(segment.filename)
                except OSError:
                    continue
        if removed:
            gone = set(removed)
            self._segments = [s for s in self._segments if s.filename not in gone]
        return removed


def recorder_args(rtsp_url: str, cache_dir: Path, index_csv: Path, *, segment_seconds: int = 2, binary: str = "ffmpeg") -> list[str]:
    """给 10.1 的录制参数：编号文件名、索引 CSV、每段时间戳归零；不用 -strftime / -segment_wrap。"""
    return [binary, "-hide_banner", "-loglevel", "error", "-nostdin", "-rtsp_transport", "tcp", "-i", rtsp_url,
            "-c", "copy", "-f", "segment", "-segment_time", str(segment_seconds), "-reset_timestamps", "1",
            "-segment_list", str(index_csv), "-segment_list_type", "csv", "-segment_list_flags", "+live",
            "-segment_format", "mp4", "-segment_format_options", "movflags=+faststart",
            str(Path(cache_dir) / "seg_%06d.mp4")]


# ---------------------------------------------------------------- 请求与任务
@dataclass(frozen=True)
class ClipRequest:
    evidence_id: str
    source_id: str
    media_run_id: str
    start_ms: int
    end_ms: int
    occurred_at: str
    project_id: Optional[str] = None
    room_id: Optional[str] = None
    session_id: Optional[str] = None
    asset_id: Optional[str] = None
    pre_ms: int = 10000
    post_ms: int = 10000

    def __post_init__(self) -> None:
        for name in ("evidence_id", "source_id", "media_run_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} 必填")
        for name in ("start_ms", "end_ms", "pre_ms", "post_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数毫秒")
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms 不能早于 start_ms")
        parse_utc(self.occurred_at)

    @property
    def window_start_ms(self) -> int:
        return max(0, self.start_ms - self.pre_ms)

    @property
    def window_end_ms(self) -> int:
        return self.end_ms + self.post_ms


@dataclass
class ClipJob:
    request: dict
    status: str = "pending"
    attempts: int = 0
    missing_ranges: list = field(default_factory=list)      # [[start_ms, end_ms], ...] 媒体毫秒
    error_code: Optional[str] = None
    evidence: Optional[dict] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    clipped_range: Optional[list] = None                    # 夹到素材 / 轮次范围后的实际窗口

    @property
    def evidence_id(self) -> str:
        return self.request["evidence_id"]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ClipJob":
        return cls(**{k: data.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


class ClipJobStore:
    """任务状态 JSON 持久化（原子写）；重启后 ready / missing / failed 不重做，pending 继续等。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else None
        self.jobs: dict[str, ClipJob] = {}
        if self.path and self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for item in data.get("jobs", []):
                job = ClipJob.from_dict(item)
                self.jobs[job.evidence_id] = job

    def get(self, evidence_id: str) -> Optional[ClipJob]:
        return self.jobs.get(evidence_id)

    def put(self, job: ClipJob) -> None:
        self.jobs[job.evidence_id] = job
        self.save()

    def pending(self) -> list[ClipJob]:
        return [job for job in self.jobs.values() if job.status == "pending"]

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "jobs": [j.to_dict() for j in self.jobs.values()]}, handle, ensure_ascii=False, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


# ---------------------------------------------------------------- 时间映射
class TimeMapper(Protocol):
    recorder_started_at: datetime

    def to_recording_seconds(self, request: ClipRequest, media_ms: int) -> Optional[float]: ...

    def bounds(self, request: ClipRequest) -> Optional[tuple[int, Optional[int]]]:
        """(最小媒体毫秒, 最大媒体毫秒|None=未知) ；返回 None 表示轮次未知（尚未登记）。"""
        ...


class WallClockMapper:
    """现场流：request.occurred_at 对应 request.start_ms，其余毫秒按差值推；录制秒 = UTC − 录制起点。"""

    def __init__(self, recorder_started_at: str) -> None:
        self.recorder_started_at = parse_utc(recorder_started_at)

    def to_recording_seconds(self, request: ClipRequest, media_ms: int) -> Optional[float]:
        anchor = parse_utc(request.occurred_at)
        moment = anchor + timedelta(milliseconds=media_ms - request.start_ms)
        return (moment - self.recorder_started_at).total_seconds()

    def bounds(self, request: ClipRequest) -> Optional[tuple[int, Optional[int]]]:
        return (0, None)


class RunRegistryMapper:
    """预制素材：4.2 运行记录把媒体毫秒换成 UTC（run.utc_of），再减录制起点；bounds 按素材时长与轮次结束夹。"""

    def __init__(self, registry, recorder_started_at: str) -> None:
        self.registry = registry
        self.recorder_started_at = parse_utc(recorder_started_at)

    def to_recording_seconds(self, request: ClipRequest, media_ms: int) -> Optional[float]:
        run = self.registry.find(request.media_run_id)
        if run is None:
            return None
        utc = run.utc_of(media_ms)
        if utc is None:
            return None
        return (parse_utc(utc) - self.recorder_started_at).total_seconds()

    def bounds(self, request: ClipRequest) -> Optional[tuple[int, Optional[int]]]:
        run = self.registry.find(request.media_run_id)
        if run is None:
            return None
        upper = run.asset_duration_ms
        if run.stopped_at is not None:
            end_offset = run.offset_ms(parse_utc(run.stopped_at))
            upper = end_offset if upper is None else min(upper, end_offset)
        return (run.asset_start_ms, upper)


# ---------------------------------------------------------------- 裁剪器
class Cutter(Protocol):
    def cut(self, segment_paths: list[Path], offset_seconds: float, duration_seconds: float, out_path: Path) -> bool: ...


class FfmpegCutter:
    """concat 列表 + 裁剪。precise=True（默认）：输出侧 -ss/-t 帧级精确（重编码）；precise=False：输入侧 -ss + -c copy，只能切关键帧。"""

    def __init__(self, binary: str = "ffmpeg", precise: bool = True, timeout_seconds: float = 120.0) -> None:
        self.binary = binary
        self.precise = precise
        self.timeout_seconds = timeout_seconds

    def build_args(self, list_file: Path, offset_seconds: float, duration_seconds: float, out_path: Path) -> list[str]:
        offset = f"{max(0.0, offset_seconds):.3f}"
        duration = f"{max(0.0, duration_seconds):.3f}"
        head = [self.binary, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
        if self.precise:
            return [*head, "-f", "concat", "-safe", "0", "-i", str(list_file), "-ss", offset, "-t", duration,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-movflags", "+faststart", str(out_path)]
        return [*head, "-ss", offset, "-f", "concat", "-safe", "0", "-i", str(list_file), "-t", duration, "-c", "copy",
                "-movflags", "+faststart", str(out_path)]

    def cut(self, segment_paths: list[Path], offset_seconds: float, duration_seconds: float, out_path: Path) -> bool:
        fd, list_file = tempfile.mkstemp(prefix="concat", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for path in segment_paths:
                    escaped = str(path).replace("'", r"'\''")
                    handle.write(f"file '{escaped}'\n")
            result = subprocess.run(self.build_args(Path(list_file), offset_seconds, duration_seconds, out_path),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=self.timeout_seconds)
            return result.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0
        except (OSError, subprocess.TimeoutExpired):
            return False
        finally:
            try:
                os.remove(list_file)
            except OSError:
                pass


# ---------------------------------------------------------------- 流水线
@dataclass
class ClipConfig:
    max_wait_seconds: float = 30.0      # 窗口末尾之后最多再等多久录制索引（录制秒）；轮次未知也按此等
    max_attempts: int = 3
    keep_seconds: float = 180.0
    tolerance_seconds: float = 0.05


class ClipPipeline:
    """一个源一个实例。submit 幂等登记；tick 推进；状态变化回传。"""

    def __init__(self, source_id: str, *, index: SegmentIndex, mapper: TimeMapper, cutter: Cutter, registrar: EvidenceRegistrar,
                 store: Optional[ClipJobStore] = None, config: Optional[ClipConfig] = None, work_dir: Optional[Path] = None,
                 clock: Optional[Callable[[], float]] = None, wall_clock: Optional[Callable[[], datetime]] = None) -> None:
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id 必填，没有默认值")
        self.source_id = source_id
        self.index = index
        self.mapper = mapper
        self.cutter = cutter
        self.registrar = registrar
        self.store = store or ClipJobStore()
        self.config = config or ClipConfig()
        self.work_dir = Path(work_dir) if work_dir else Path(tempfile.gettempdir()) / "poc-clips" / _safe(source_id)
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self.clock = clock or (lambda: (self.wall_clock() - self.mapper.recorder_started_at).total_seconds())
        self._cut_calls = 0

    # ---- 对外 ----
    def submit(self, request: ClipRequest, *, now: Optional[str] = None) -> ClipJob:
        if request.source_id != self.source_id:
            raise ValueError(f"本流水线只处理源 {self.source_id}，不接 {request.source_id}（不同摄像头不混片）")
        existing = self.store.get(request.evidence_id)
        if existing is not None:
            if existing.request != asdict(request):
                raise ValueError("EVIDENCE_CONFLICT: 同一 evidence_id 的请求内容不同，不覆盖")
            return existing
        stamp = now or format_utc(self.wall_clock())
        job = ClipJob(request=asdict(request), created_at=stamp, updated_at=stamp)
        self.store.put(job)
        return job

    def tick(self, *, now_recording_seconds: Optional[float] = None, now: Optional[str] = None) -> list[ClipJob]:
        """now_recording_seconds：当前录制秒；缺省用墙钟 − 录制起点，录制停了也会按最长等待判 missing。"""
        self.index.reload()
        clock = self.clock() if now_recording_seconds is None else now_recording_seconds
        stamp = now or format_utc(self.wall_clock())
        changed: list[ClipJob] = []
        for job in self.store.pending():
            try:
                if self._advance(job, clock, stamp):
                    changed.append(job)
            except Exception as exc:  # noqa: BLE001  单任务异常不打断整轮
                self._finish(job, "failed", stamp, code=f"INTERNAL_{type(exc).__name__}"[:64])
                changed.append(job)
        return changed

    def status(self) -> dict:
        counts = {s: 0 for s in JOB_STATUSES}
        for job in self.store.jobs.values():
            counts[job.status] = counts.get(job.status, 0) + 1
        return {"source_id": self.source_id, "jobs": counts, "cut_calls": self._cut_calls, "covered_until": self.index.covered_until()}

    # ---- 内部 ----
    def _advance(self, job: ClipJob, clock: float, stamp: str) -> bool:
        request = ClipRequest(**job.request)
        recorded_until = self.index.covered_until()
        bounds = self.mapper.bounds(request)
        if bounds is None:                                     # 轮次尚未登记：等，超过最长等待判缺失
            waited = (parse_utc(stamp) - parse_utc(job.created_at)).total_seconds() if job.created_at else 0.0
            if waited > self.config.max_wait_seconds:
                self._finish(job, "missing", stamp, missing=[[request.window_start_ms, request.window_end_ms]], code="TIME_UNMAPPED")
                return True
            return False
        lower, upper = bounds
        win_start = max(request.window_start_ms, lower)
        win_end = request.window_end_ms if upper is None else min(request.window_end_ms, upper)
        if win_end <= win_start:
            self._finish(job, "missing", stamp, missing=[[request.window_start_ms, request.window_end_ms]], code="TIME_UNMAPPED")
            return True
        job.clipped_range = [win_start, win_end]
        start_s = self.mapper.to_recording_seconds(request, win_start)
        end_s = self.mapper.to_recording_seconds(request, win_end)
        if start_s is None or end_s is None or end_s <= start_s:
            self._finish(job, "missing", stamp, missing=[[win_start, win_end]], code="TIME_UNMAPPED")
            return True
        tol = self.config.tolerance_seconds
        if recorded_until + tol < end_s:
            if clock - end_s > self.config.max_wait_seconds:
                self._finish(job, "missing", stamp, missing=self._to_media(win_start, start_s, self.index.gaps(start_s, end_s, tolerance=tol)), code="RECORDING_GAP")
                return True
            return False                                       # 后段还没落盘：继续等
        gaps = self.index.gaps(start_s, end_s, tolerance=tol)
        if gaps:
            self._finish(job, "missing", stamp, missing=self._to_media(win_start, start_s, gaps), code="RECORDING_GAP")
            return True
        segments = self.index.segments_for(start_s, end_s)
        if not segments:
            self._finish(job, "missing", stamp, missing=[[win_start, win_end]], code="RECORDING_GAP")
            return True
        offset = start_s - segments[0].start
        duration = end_s - start_s
        self.work_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.work_dir / f"{_safe(request.evidence_id)}-{hashlib.sha1(request.evidence_id.encode('utf-8')).hexdigest()[:8]}.mp4"
        job.attempts += 1
        self._cut_calls += 1
        try:
            ok = self.cutter.cut([self.index.cache_dir / s.filename for s in segments], offset, duration, out_path)
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            _unlink(out_path)
            return self._retry_or_fail(job, stamp, "CUT_FAILED")
        try:
            digest = sha256_of_file(out_path)
            ref = build_evidence_ref(evidence_id=request.evidence_id, project_id=request.project_id, room_id=request.room_id,
                                     session_id=request.session_id, source_id=request.source_id, media_run_id=request.media_run_id,
                                     asset_id=request.asset_id, evidence_type="video", start_ms=win_start, end_ms=win_end,
                                     status="ready", sha256=digest)
            try:
                self.registrar.register(ref, out_path)
            except Exception as exc:  # noqa: BLE001  登记失败是取证失败的一种，可重试
                return self._retry_or_fail(job, stamp, f"REGISTER_{type(exc).__name__}"[:64])
        finally:
            _unlink(out_path)
        job.status, job.evidence, job.updated_at = "ready", ref, stamp
        self.store.put(job)
        return True

    def _retry_or_fail(self, job: ClipJob, stamp: str, code: str) -> bool:
        if job.attempts >= self.config.max_attempts:
            self._finish(job, "failed", stamp, code=code)
            return True
        job.updated_at = stamp
        job.error_code = code
        self.store.put(job)
        return False

    def _finish(self, job: ClipJob, status: str, stamp: str, *, missing: Optional[list] = None, code: Optional[str] = None) -> None:
        request = ClipRequest(**job.request)
        start, end = job.clipped_range if job.clipped_range else [request.window_start_ms, request.window_end_ms]
        job.status = status
        job.error_code = code
        job.missing_ranges = missing or []
        job.updated_at = stamp
        job.evidence = build_evidence_ref(evidence_id=request.evidence_id, project_id=request.project_id, room_id=request.room_id,
                                          session_id=request.session_id, source_id=request.source_id, media_run_id=request.media_run_id,
                                          asset_id=request.asset_id, evidence_type="video", start_ms=start, end_ms=end, status=status)
        try:
            self.registrar.register(job.evidence, None)
        except Exception:  # noqa: BLE001  缺失 / 失败的登记失败不阻塞状态
            pass
        self.store.put(job)

    @staticmethod
    def _to_media(window_start_ms: int, window_start_s: float, gaps: list[tuple[float, float]]) -> list[list[int]]:
        return [[window_start_ms + int(round((a - window_start_s) * 1000)), window_start_ms + int(round((b - window_start_s) * 1000))] for a, b in gaps]


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


def _unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
