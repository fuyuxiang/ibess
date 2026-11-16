"""媒体运行记录（执行卡 4.2）：每起一次推流就是一条 run，素材播完一轮换新 run_id。

记录回答两个问题：「这一刻播的是哪个场次哪个源的哪个素材」与「这一刻对应素材第几毫秒」。
UTC 与素材相对毫秒的换算只依赖本轮的 started_at 与 asset_start_ms，不依赖墙钟推断阶段。
持久化为 JSON 文件（原子写），重启后能恢复；没有任何默认场次或默认源。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REGISTRY_VERSION = 1
UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
STOP_REASONS = ("phase_switch", "loop_end", "shutdown", "controller_restart", "process_exit", "apply_failed")
STATUSES = ("starting", "playing", "stopped", "failed")


def parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not UTC_PATTERN.match(value):
        raise ValueError("时间必须是 Z 结尾的 UTC ISO 字符串")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_utc(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("时间必须带时区")
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_media_run_id(source_id: str, stage_snapshot_id: str, loop_index: int, started_at: str) -> str:
    epoch_ms = int(parse_utc(started_at).timestamp() * 1000)
    return f"run:{source_id}:{stage_snapshot_id}:{loop_index}:{epoch_ms}"


@dataclass
class MediaRun:
    media_run_id: str
    session_id: str
    source_id: str
    asset_id: str
    stage_snapshot_id: str
    phase_id: str
    phase_version: int
    started_at: str
    asset_start_ms: int = 0
    asset_duration_ms: Optional[int] = None
    loop_index: int = 0
    status: str = "starting"
    confirmed_at: Optional[str] = None
    stopped_at: Optional[str] = None
    stop_reason: Optional[str] = None
    planned: bool = False
    note: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MediaRun":
        return cls(**{k: data.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]

    @property
    def active(self) -> bool:
        return self.status in ("starting", "playing")

    def locate_ms(self, at: datetime) -> Optional[int]:
        """UTC 时刻 → 本轮素材相对毫秒；不在本轮时间范围内返回 None。"""
        start = parse_utc(self.started_at)
        if at < start:
            return None
        if self.stopped_at is not None and at > parse_utc(self.stopped_at):
            return None
        offset = int((at - start).total_seconds() * 1000)
        value = self.asset_start_ms + offset
        if self.asset_duration_ms is not None and value > self.asset_duration_ms:
            return None
        return value

    def offset_ms(self, at: datetime) -> int:
        """本轮内的素材相对毫秒，不要求在范围内：早于开始取起点，超过素材时长截到时长。"""
        start = parse_utc(self.started_at)
        value = self.asset_start_ms + max(0, int((at - start).total_seconds() * 1000))
        if self.asset_duration_ms is not None:
            value = min(value, self.asset_duration_ms)
        return value

    def utc_of(self, start_ms: int) -> Optional[str]:
        """素材相对毫秒 → UTC；不在本轮覆盖范围内返回 None。"""
        if isinstance(start_ms, bool) or not isinstance(start_ms, int) or start_ms < self.asset_start_ms:
            return None
        if self.asset_duration_ms is not None and start_ms > self.asset_duration_ms:
            return None
        start = parse_utc(self.started_at)
        moment = start + _ms(start_ms - self.asset_start_ms)
        if self.stopped_at is not None and moment > parse_utc(self.stopped_at):
            return None
        return format_utc(moment)


def _ms(value: int):
    from datetime import timedelta
    return timedelta(milliseconds=value)


@dataclass
class _State:
    runs: list = field(default_factory=list)
    boundaries: list = field(default_factory=list)


class MediaRunRegistry:
    """内存 + 可选 JSON 文件。一个文件可记多个源，但每个源同时只有一条 active run。"""

    def __init__(self, path: Optional[os.PathLike | str] = None) -> None:
        self.path = Path(path) if path is not None else None
        self._state = _State()
        if self.path is not None and self.path.exists():
            self.restore(json.loads(self.path.read_text(encoding="utf-8")))

    # ---- 查询 ----
    def runs(self, source_id: Optional[str] = None) -> list[MediaRun]:
        return [r for r in self._state.runs if source_id is None or r.source_id == source_id]

    def current(self, source_id: str) -> Optional[MediaRun]:
        for run in reversed(self._state.runs):
            if run.source_id == source_id and run.active:
                return run
        return None

    def find(self, media_run_id: str) -> Optional[MediaRun]:
        for run in self._state.runs:
            if run.media_run_id == media_run_id:
                return run
        return None

    def history(self, source_id: str) -> list[dict]:
        return [r.to_dict() for r in self.runs(source_id)]

    def boundaries(self, source_id: Optional[str] = None) -> list[dict]:
        return [b for b in self._state.boundaries if source_id is None or b["source_id"] == source_id]

    def locate(self, source_id: str, at: datetime) -> Optional[dict]:
        """某源在某 UTC 时刻播的是哪轮哪毫秒。多条匹配取最晚开始的一条。"""
        for run in reversed(self.runs(source_id)):
            ms = run.locate_ms(at)
            if ms is not None:
                return {"media_run_id": run.media_run_id, "asset_id": run.asset_id, "start_ms": ms,
                        "stage_snapshot_id": run.stage_snapshot_id, "phase_version": run.phase_version}
        return None

    # ---- 变更 ----
    def start(self, *, session_id: str, source_id: str, asset_id: str, stage_snapshot_id: str, phase_id: str,
              phase_version: int, started_at: str, asset_duration_ms: Optional[int] = None, loop_index: int = 0,
              asset_start_ms: int = 0, note: Optional[str] = None) -> MediaRun:
        for name, value in (("session_id", session_id), ("source_id", source_id), ("asset_id", asset_id),
                            ("stage_snapshot_id", stage_snapshot_id), ("phase_id", phase_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 必填，没有默认值")
        if isinstance(phase_version, bool) or not isinstance(phase_version, int) or phase_version < 1:
            raise ValueError("phase_version 必须是正整数")
        parse_utc(started_at)
        if self.current(source_id) is not None:
            raise ValueError(f"源 {source_id} 已有未结束的 run，先 stop 再 start")
        run_id = make_media_run_id(source_id, stage_snapshot_id, loop_index, started_at)
        if self.find(run_id) is not None:
            run_id = f"{run_id}:{len(self._state.runs)}"
        run = MediaRun(media_run_id=run_id, session_id=session_id, source_id=source_id, asset_id=asset_id,
                       stage_snapshot_id=stage_snapshot_id, phase_id=phase_id, phase_version=phase_version,
                       started_at=started_at, asset_start_ms=asset_start_ms, asset_duration_ms=asset_duration_ms,
                       loop_index=loop_index, note=note)
        self._state.runs.append(run)
        self._boundary(run, started_at, "run_start", asset_start_ms)
        self._save()
        return run

    def mark_playing(self, media_run_id: str, confirmed_at: str) -> MediaRun:
        run = self._require(media_run_id)
        parse_utc(confirmed_at)
        if run.status != "starting":
            raise ValueError(f"run {media_run_id} 状态为 {run.status}，不能标为 playing")
        run.status = "playing"
        run.confirmed_at = confirmed_at
        self._save()
        return run

    def stop(self, media_run_id: str, *, stopped_at: str, reason: str, planned: bool) -> MediaRun:
        run = self._require(media_run_id)
        if reason not in STOP_REASONS:
            raise ValueError(f"未知停止原因 {reason}")
        if not run.active:
            return run                      # 幂等：已结束的不再改
        parse_utc(stopped_at)
        run.status = "stopped"
        run.stopped_at = stopped_at
        run.stop_reason = reason
        run.planned = bool(planned)
        self._boundary(run, stopped_at, "run_end", run.offset_ms(parse_utc(stopped_at)))
        self._save()
        return run

    def fail(self, media_run_id: str, *, failed_at: str, reason: str = "apply_failed", note: Optional[str] = None) -> MediaRun:
        run = self._require(media_run_id)
        if not run.active:
            return run
        parse_utc(failed_at)
        run.status = "failed"
        run.stopped_at = failed_at
        run.stop_reason = reason
        run.planned = False
        if note:
            run.note = note[:200]
        self._boundary(run, failed_at, "run_end", run.offset_ms(parse_utc(failed_at)))
        self._save()
        return run

    def next_loop(self, previous: MediaRun, *, started_at: str) -> MediaRun:
        """素材播完一轮：以新 run_id、loop_index+1 续同一素材。调用前应已 stop(previous, loop_end)。"""
        if previous.active:
            raise ValueError("上一轮尚未结束")
        return self.start(session_id=previous.session_id, source_id=previous.source_id, asset_id=previous.asset_id,
                          stage_snapshot_id=previous.stage_snapshot_id, phase_id=previous.phase_id,
                          phase_version=previous.phase_version, started_at=started_at,
                          asset_duration_ms=previous.asset_duration_ms, loop_index=previous.loop_index + 1)

    def close_dangling(self, *, at: str, reason: str = "controller_restart", source_id: Optional[str] = None) -> list[MediaRun]:
        """重启恢复：进程已不在，本源（或不限源）的 active run 一律标 stopped。"""
        closed = []
        for run in self._state.runs:
            if run.active and (source_id is None or run.source_id == source_id):
                self.stop(run.media_run_id, stopped_at=at, reason=reason, planned=False)
                closed.append(run)
        return closed

    # ---- 持久化 ----
    def export(self) -> dict:
        return {"version": REGISTRY_VERSION, "runs": [r.to_dict() for r in self._state.runs],
                "boundaries": list(self._state.boundaries)}

    def restore(self, state: dict) -> None:
        if not isinstance(state, dict) or state.get("version") != REGISTRY_VERSION:
            raise ValueError("运行记录文件版本不识别")
        runs = [MediaRun.from_dict(r) for r in state.get("runs", [])]
        for run in runs:
            if run.status not in STATUSES:
                raise ValueError(f"运行记录状态非法：{run.status}")
        self._state = _State(runs=runs, boundaries=list(state.get("boundaries", [])))

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.export(), ensure_ascii=False, indent=1)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def _require(self, media_run_id: str) -> MediaRun:
        run = self.find(media_run_id)
        if run is None:
            raise KeyError(media_run_id)
        return run

    def _boundary(self, run: MediaRun, at: str, kind: str, start_ms: int) -> None:
        self._state.boundaries.append({"at": at, "kind": kind, "source_id": run.source_id,
                                       "media_run_id": run.media_run_id, "start_ms": int(start_ms),
                                       "stage_snapshot_id": run.stage_snapshot_id, "phase_version": run.phase_version,
                                       "loop_index": run.loop_index})

    def __len__(self) -> int:
        return len(self._state.runs)
