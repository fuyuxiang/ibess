"""S10 音频采集健康（执行卡 5.3）：采集心跳、静音标记、断流与恢复 → 契约 2.0.0 观察 + 见证缺口。

心跳看「音频块有没有按时到」，静音看能量（RMS）。静音只是质量标记，不是断流（执行卡「静音不当设备断流」）。
仓库里目前没有实时采集进程（音频走 PG 录音队列），谁来喂 on_chunk() / tick() 由 10.1 接线时定。
audio-worker 不能导入 vision-worker 的包，所以这里内置一本最小缺口簿 AudioGapBook，gap_id 规则与
vision-worker 的 coverage_gap 完全相同；同进程部署时可注入 coverage_gap.GapRegistry（接口一致）。
样本约定：有符号整数 PCM 或 -1～1 浮点；多声道为 (采样数, 声道数)；空块不算心跳；NaN 按 0 处理。
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np

CONTRACT_VERSION = "2.0.0"
MODEL_NAME = "audio_health"
MODEL_VERSION = "5.3-v1"
KIND = "audio_offline"
UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
SCOPE_FIELDS = ("project_id", "room_id", "session_id")
SNAPSHOT_FIELDS = ("stage_snapshot_id", "phase_id", "phase_version")
STATE_VERSION = 1


def make_gap_id(source_id: str, media_run_id: str, kind: str, start_ms: int) -> str:
    return f"gap:{source_id}:{media_run_id}:{kind}:{int(start_ms)}"


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass
class AudioGap:
    gap_id: str
    kind: str
    source_id: str
    gap_media_run_id: str
    open_start_ms: int
    opened_at: str
    scope: dict = field(default_factory=dict)
    technical_reason: str = ""
    closed_at: Optional[str] = None
    close_media_run_id: Optional[str] = None
    close_end_ms: Optional[int] = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AudioGap":
        return cls(**data)


class AudioGapBook:
    """与 vision-worker coverage_gap.GapRegistry 同接口的最小缺口簿。"""

    def __init__(self) -> None:
        self._gaps: dict[str, AudioGap] = {}

    def open(self, *, source_id: str, kind: str, media_run_id: str, start_ms: int, occurred_at: str,
             scope: Optional[dict] = None, technical_reason: str = "") -> AudioGap:
        existing = self.open_gap(source_id, kind)
        if existing is not None:
            return existing
        gap_id = make_gap_id(source_id, media_run_id, kind, start_ms)
        suffix = 1
        while gap_id in self._gaps:
            suffix += 1
            gap_id = f"{make_gap_id(source_id, media_run_id, kind, start_ms)}:{suffix}"
        gap = AudioGap(gap_id=gap_id, kind=kind, source_id=source_id, gap_media_run_id=media_run_id,
                       open_start_ms=int(start_ms), opened_at=occurred_at, scope=dict(scope or {}),
                       technical_reason=technical_reason)
        self._gaps[gap_id] = gap
        return gap

    def open_gap(self, source_id: str, kind: str) -> Optional[AudioGap]:
        for gap in self._gaps.values():
            if gap.source_id == source_id and gap.kind == kind and gap.is_open:
                return gap
        return None

    def open_gaps(self, source_id: Optional[str] = None) -> list[AudioGap]:
        return [g for g in self._gaps.values() if g.is_open and (source_id is None or g.source_id == source_id)]

    def all(self) -> list[AudioGap]:
        return list(self._gaps.values())

    def find(self, gap_id: str) -> Optional[AudioGap]:
        return self._gaps.get(gap_id)

    def close(self, gap_id: str, *, media_run_id: str, end_ms: int, occurred_at: str) -> AudioGap:
        gap = self._gaps[gap_id]
        if not gap.is_open:
            return gap
        gap.closed_at = occurred_at
        gap.close_media_run_id = media_run_id
        gap.close_end_ms = int(end_ms)
        return gap

    def export(self) -> dict:
        return {"version": STATE_VERSION, "gaps": [g.to_dict() for g in self._gaps.values()]}

    def restore(self, state: dict) -> None:
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            raise ValueError("缺口状态版本不支持")
        self._gaps = {g["gap_id"]: AudioGap.from_dict(g) for g in state.get("gaps", [])}


@dataclass
class AudioHealthConfig:
    heartbeat_seconds: float = 3.0        # 多久没有音频块算断流（对齐视频断流阈值，工程初值）
    silence_rms_threshold: float = 0.01   # 归一化 RMS 低于此值记静音（满幅为 1.0）
    recovery_seconds: float = 2.0         # 恢复持续多久才确认

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "AudioHealthConfig":
        env = os.environ if env is None else env

        def num(name: str, default: float) -> float:
            value = env.get(name)
            return float(value) if value not in (None, "") else default

        return cls(heartbeat_seconds=num("AUDIO_HEARTBEAT_SECONDS", 3.0),
                   silence_rms_threshold=num("AUDIO_SILENCE_RMS", 0.01),
                   recovery_seconds=num("AUDIO_RECOVERY_SECONDS", 2.0))


@dataclass
class AudioChunkContext:
    source_id: str
    occurred_at: str
    media_run_id: str
    start_ms: int
    asset_id: Optional[str] = None
    project_id: Optional[str] = None
    room_id: Optional[str] = None
    session_id: Optional[str] = None
    stage_snapshot_id: Optional[str] = None
    phase_id: Optional[str] = None
    phase_version: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise ValueError("source_id 必填，没有默认值")
        if not isinstance(self.media_run_id, str) or not self.media_run_id.strip():
            raise ValueError("media_run_id 必填")
        if isinstance(self.start_ms, bool) or not isinstance(self.start_ms, int) or self.start_ms < 0:
            raise ValueError("start_ms 必须是非负整数毫秒")
        if not isinstance(self.occurred_at, str) or not UTC_PATTERN.match(self.occurred_at):
            raise ValueError("occurred_at 必须是 Z 结尾的 UTC ISO 字符串")

    @property
    def bound(self) -> bool:
        return all(getattr(self, k) is not None for k in SCOPE_FIELDS + SNAPSHOT_FIELDS)

    def scope(self) -> dict:
        return {k: getattr(self, k) for k in SCOPE_FIELDS}

    def when(self) -> datetime:
        return parse_utc(self.occurred_at)


def rms_of(samples) -> float:
    """归一化 RMS（满幅 1.0）。有符号整数按其最大值归一；无符号整数按中点归一；NaN / Inf 按 0；空块返回 0。"""
    data = np.asarray(samples)
    if data.size == 0:
        return 0.0
    if np.issubdtype(data.dtype, np.integer):
        info = np.iinfo(data.dtype)
        if info.min < 0:
            data = data.astype(np.float64) / float(info.max)
        else:
            mid = (float(info.max) + 1.0) / 2.0
            data = (data.astype(np.float64) - mid) / mid
    else:
        data = np.nan_to_num(data.astype(np.float64), nan=0.0, posinf=0.0, neginf=0.0)
    if data.ndim > 1:
        if data.ndim == 2 and data.shape[0] <= 8 < data.shape[1]:
            data = data.T                      # (声道数, 采样数) 布局，转成 (采样数, 声道数)
        data = data.reshape(data.shape[0], -1).mean(axis=1)
    return float(np.sqrt(np.mean(np.square(data))))


def _gap_evidence(ctx: AudioChunkContext, gap: AudioGap) -> dict:
    """gap 类 missing 证据引用，打开与关闭观察内容恒定（起止都为 null），便于 bundle 级校验对上。"""
    return {
        "contract_version": CONTRACT_VERSION,
        "project_id": ctx.project_id,
        "room_id": ctx.room_id,
        "session_id": ctx.session_id,
        "source_id": ctx.source_id,
        "evidence_id": f"{gap.gap_id}:evidence",
        "media_run_id": gap.gap_media_run_id,
        "asset_id": ctx.asset_id,
        "evidence_type": "gap",
        "start_ms": None,
        "end_ms": None,
        "sha256": None,
        "status": "missing",
    }


def _build_observation(obs_type: str, ctx: AudioChunkContext, gap: AudioGap, payload: dict,
                       start_ms: int, end_ms: int, quality_flags: tuple = ()) -> dict:
    bound = ctx.bound
    flags = list(dict.fromkeys(f for f in quality_flags if f))
    if not bound and "binding_missing" not in flags:
        flags.append("binding_missing")
    end_ms = int(end_ms)
    start_ms = min(int(start_ms), end_ms)
    return {
        "contract_version": CONTRACT_VERSION,
        "project_id": ctx.project_id,
        "room_id": ctx.room_id,
        "session_id": ctx.session_id,
        "source_id": ctx.source_id,
        "stage_snapshot_id": ctx.stage_snapshot_id if bound else None,
        "phase_id": ctx.phase_id if bound else None,
        "phase_version": ctx.phase_version if bound else None,
        "observation_id": f"{gap.gap_id}:{obs_type}:{ctx.media_run_id}:{end_ms}",
        "binding_status": "bound" if bound else "unbound",
        "occurred_at": ctx.occurred_at,
        "media_run_id": ctx.media_run_id,
        "asset_id": ctx.asset_id,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "type": obs_type,
        "payload": payload,
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
        "quality_flags": flags,
        "evidence_refs": [_gap_evidence(ctx, gap)],
    }


class AudioHealthMonitor:
    """一个采集源一个实例。stream = waiting | online | offline；silence 是独立的质量标记。"""

    def __init__(self, config: Optional[AudioHealthConfig] = None, registry=None) -> None:
        self.config = config or AudioHealthConfig()
        self.registry = registry if registry is not None else AudioGapBook()
        self._source_id: Optional[str] = None
        self._media_run_id: Optional[str] = None
        self._stream = "waiting"
        self._started_at: Optional[datetime] = None
        self._last_seen_at: Optional[datetime] = None
        self._last_chunk_at: Optional[datetime] = None
        self._last_chunk_start_ms: Optional[int] = None
        self._recovery_since: Optional[datetime] = None
        self._recovery_start_ms: Optional[int] = None
        self._last_rms: Optional[float] = None
        self._silence = False
        self._primed = False

    def reset(self, media_run_id: str) -> None:
        self._media_run_id = media_run_id
        self._started_at = None
        self._last_chunk_at = None
        self._last_chunk_start_ms = None
        self._recovery_since = None
        self._recovery_start_ms = None

    def on_chunk(self, context: AudioChunkContext, samples) -> list[dict]:
        data = np.asarray(samples)
        if data.size == 0:
            return self.tick(context)                      # 空块不算心跳
        now = self._touch(context)
        cfg = self.config
        out: list[dict] = []
        if self._last_chunk_at is not None and self._stream in ("online", "waiting") and \
                (now - self._last_chunk_at).total_seconds() >= cfg.heartbeat_seconds:
            out.append(self._go_offline(context, self._last_chunk_start_ms, "no_chunk"))   # 迟到的块：追溯开缺口
        self._last_chunk_at = now
        self._last_chunk_start_ms = context.start_ms
        self._last_rms = rms_of(data)
        self._silence = self._last_rms < cfg.silence_rms_threshold
        if self._stream in ("waiting", "offline"):
            if self._recovery_since is None:
                self._recovery_since = now
                self._recovery_start_ms = context.start_ms
            if (now - self._recovery_since).total_seconds() >= cfg.recovery_seconds:
                self._stream = "online"
                gap = self.registry.open_gap(context.source_id, KIND)
                if gap is not None:
                    self.registry.close(gap.gap_id, media_run_id=context.media_run_id,
                                        end_ms=self._recovery_start_ms, occurred_at=context.occurred_at)
                    same_run = gap.gap_media_run_id == context.media_run_id
                    payload = {"gap_id": gap.gap_id, "gap_media_run_id": gap.gap_media_run_id,
                               "gap_start_ms": gap.open_start_ms, "gap_end_ms": self._recovery_start_ms,
                               "gap_duration_ms": (self._recovery_start_ms - gap.open_start_ms) if same_run else None,
                               "technical_reason": "chunks_resumed", "rms": round(self._last_rms, 5)}
                    out.append(_build_observation("audio_online", context, gap, payload,
                                                  self._recovery_start_ms, context.start_ms, self._flags()))
                self._recovery_since = None
        return out

    def tick(self, context: AudioChunkContext) -> list[dict]:
        now = self._touch(context)
        cfg = self.config
        out: list[dict] = []
        had_chunks = self._last_chunk_at is not None
        if had_chunks and (now - self._last_chunk_at).total_seconds() >= cfg.heartbeat_seconds:
            if self._stream in ("online", "waiting"):
                out.append(self._go_offline(context, self._last_chunk_start_ms, "no_chunk"))
            elif self._recovery_since is not None:
                self._recovery_since = None
            return out
        if self._stream == "waiting" and not had_chunks:
            waited = (now - self._started_at).total_seconds()
            if waited >= cfg.heartbeat_seconds and self.registry.open_gap(context.source_id, KIND) is None:
                gap = self.registry.open(source_id=context.source_id, kind=KIND, media_run_id=context.media_run_id,
                                         start_ms=0, occurred_at=context.occurred_at, scope=context.scope(),
                                         technical_reason="no_stream_at_start")
                out.append(self._emit_open(context, gap))
        return out

    def status(self) -> dict:
        waiting = (self._last_seen_at - self._started_at).total_seconds() if (
            self._stream == "waiting" and self._started_at and self._last_seen_at) else 0.0
        since = (self._last_seen_at - self._last_chunk_at).total_seconds() if (
            self._last_chunk_at and self._last_seen_at) else None
        return {
            "source_id": self._source_id,
            "media_run_id": self._media_run_id,
            "stream": self._stream,
            "silence": self._silence,
            "last_rms": self._last_rms,
            "waiting_seconds": round(waiting, 3),
            "seconds_since_last_chunk": None if since is None else round(since, 3),
            "open_gaps": [g.gap_id for g in self.registry.open_gaps(self._source_id)] if self._source_id else [],
        }

    # ---- 内部 ----
    def _touch(self, context: AudioChunkContext) -> datetime:
        if self._source_id is not None and context.source_id != self._source_id:
            raise ValueError(f"一个实例只跟一个源：已是 {self._source_id}，收到 {context.source_id}")
        if self._media_run_id is not None and context.media_run_id != self._media_run_id:
            self.reset(context.media_run_id)
        now = context.when()
        if self._started_at is None:
            self._started_at = now
        self._source_id = context.source_id
        self._media_run_id = context.media_run_id
        self._last_seen_at = now
        if not self._primed:
            self._primed = True
            if self.registry.open_gap(context.source_id, KIND) is not None:
                self._stream = "offline"               # restore 进来的打开缺口：从断流态续接
        return now

    def _flags(self) -> tuple:
        return ("silence",) if self._silence else ()

    def _go_offline(self, context: AudioChunkContext, start_ms: int, reason: str) -> dict:
        self._stream = "offline"
        self._recovery_since = None
        gap = self.registry.open(source_id=context.source_id, kind=KIND, media_run_id=context.media_run_id,
                                 start_ms=start_ms, occurred_at=context.occurred_at, scope=context.scope(),
                                 technical_reason=reason)
        return self._emit_open(context, gap)

    def _emit_open(self, context: AudioChunkContext, gap: AudioGap) -> dict:
        payload = {"gap_id": gap.gap_id, "gap_media_run_id": gap.gap_media_run_id, "gap_start_ms": gap.open_start_ms,
                   "technical_reason": gap.technical_reason}
        if self._last_rms is not None:
            payload["last_rms"] = round(self._last_rms, 5)
        start_ms = gap.open_start_ms if gap.gap_media_run_id == context.media_run_id else context.start_ms
        return _build_observation("audio_offline", context, gap, payload, start_ms, context.start_ms, self._flags())
