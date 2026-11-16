"""S10 视频设备健康（执行卡 5.3）：断流、低质量、偏移与恢复 → 契约 2.0.0 观察 + 见证缺口。

判断全部是确定性代码，没有模型。入口每个循环都要调 update()：有帧传帧，没帧传 frame=None；
单次 cap.read() 阻塞很久后才回来的帧也会被追溯成缺口（按上一帧时刻起算）。
场次、源、媒体轮次一律来自 FrameContext，没有任何默认值；media_run_id 变化等同于 reset()。
低质量（黑屏、过暗过曝、失焦、偏移）都是技术事实，只用 technical_reason 区分，不表示人为遮挡。
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import cv2
import numpy as np

from worker.coverage_gap import Gap, GapRegistry

CONTRACT_VERSION = "2.0.0"
MODEL_NAME = "stage_health"
MODEL_VERSION = "5.3-v1"
UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")

KIND_TYPES = {"offline": ("video_offline", "video_online"), "quality": ("camera_blacked", "camera_normal")}
LEGACY_TYPES = {"video_offline": ("offline", "open"), "video_online": ("offline", "close"),
                "camera_blacked": ("quality", "open"), "camera_normal": ("quality", "close")}
SCOPE_FIELDS = ("project_id", "room_id", "session_id")
SNAPSHOT_FIELDS = ("stage_snapshot_id", "phase_id", "phase_version")
MIN_ROI_SIDE = 2


class FrameError(ValueError):
    """帧不可用（形状、dtype 或空帧）。update() 内部把它当「本轮无帧」处理，不向上抛。"""


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class StageHealthConfig:
    """阈值都是工程初值（执行卡:81）；环境变量名沿用旧入口的 OFFLINE_SECONDS / BLACK_SECONDS / BLACK_VAR_THRESHOLD。"""
    offline_seconds: float = 3.0       # 连续无帧多久算断流
    degraded_seconds: float = 2.0      # 低质量持续多久才确认（旧入口 BLACK_SECONDS=1，执行卡改 2）
    recovery_seconds: float = 2.0      # 恢复持续多久才确认
    black_var_threshold: float = 10.0  # 灰度方差低于此值视为黑屏或整块遮挡
    blur_threshold: float = 20.0       # Laplacian 方差低于此值视为失焦
    dark_mean: float = 20.0            # 平均亮度低于此值视为过暗
    bright_mean: float = 235.0         # 平均亮度高于此值视为过曝
    shift_px: float = 20.0             # 与参考图相位相关位移超过此像素视为偏移

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "StageHealthConfig":
        env = os.environ if env is None else env

        def num(name: str, default: float) -> float:
            value = env.get(name)
            return float(value) if value not in (None, "") else default

        return cls(
            offline_seconds=num("OFFLINE_SECONDS", 3.0),
            degraded_seconds=num("BLACK_SECONDS", 2.0),
            recovery_seconds=num("RECOVERY_SECONDS", 2.0),
            black_var_threshold=num("BLACK_VAR_THRESHOLD", 10.0),
            blur_threshold=num("BLUR_THRESHOLD", 20.0),
            dark_mean=num("DARK_MEAN", 20.0),
            bright_mean=num("BRIGHT_MEAN", 235.0),
            shift_px=num("SHIFT_PX", 20.0),
        )


@dataclass
class FrameContext:
    """入口每轮构造一份。source_id、media_run_id、start_ms、occurred_at 必填且没有默认值；场次与快照缺失即 unbound。"""
    source_id: str
    occurred_at: str                       # UTC ISO，Z 结尾
    media_run_id: str                      # 4.2 媒体运行记录；没有 4.2 时入口自造并在交接注明桩
    start_ms: int                          # 相对本轮媒体的毫秒
    frame: Optional[np.ndarray] = None     # BGR 帧；None 表示本轮没有新帧
    asset_id: Optional[str] = None
    project_id: Optional[str] = None
    room_id: Optional[str] = None
    session_id: Optional[str] = None
    stage_snapshot_id: Optional[str] = None
    phase_id: Optional[str] = None
    phase_version: Optional[int] = None
    reference_frame: Optional[np.ndarray] = None   # 偏移参考图
    roi: Optional[tuple] = None                    # 归一化 (x1, y1, x2, y2)；参考图与 ROI 都有才算偏移已配置

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise ValueError("source_id 必填，没有默认值（契约禁止默认 camera-001）")
        if not isinstance(self.media_run_id, str) or not self.media_run_id.strip():
            raise ValueError("media_run_id 必填（契约：媒体本轮 ID 必须提供）")
        if isinstance(self.start_ms, bool) or not isinstance(self.start_ms, int) or self.start_ms < 0:
            raise ValueError("start_ms 必须是非负整数毫秒")
        if not isinstance(self.occurred_at, str) or not UTC_PATTERN.match(self.occurred_at):
            raise ValueError("occurred_at 必须是 Z 结尾的 UTC ISO 字符串")
        if self.roi is not None:
            if len(self.roi) != 4 or not all(0.0 <= float(v) <= 1.0 for v in self.roi):
                raise ValueError("roi 必须是 4 个 0～1 的归一化坐标")
            if not (self.roi[0] < self.roi[2] and self.roi[1] < self.roi[3]):
                raise ValueError("roi 必须满足 x1<x2 且 y1<y2")

    @property
    def bound(self) -> bool:
        return all(getattr(self, k) is not None for k in SCOPE_FIELDS + SNAPSHOT_FIELDS)

    def scope(self) -> dict:
        return {k: getattr(self, k) for k in SCOPE_FIELDS}

    def when(self) -> datetime:
        return parse_utc(self.occurred_at)

    def fields(self) -> dict:
        return {k: getattr(self, k) for k in ("source_id", "occurred_at", "media_run_id", "asset_id") + SCOPE_FIELDS + SNAPSHOT_FIELDS}


def gap_evidence(context: dict, gap: Gap) -> dict:
    """gap 类 missing 证据引用。打开与关闭观察引用同一条、内容恒定（起止都为 null，细节在 payload），
    这样 1.2 的 bundle 校验（引用须与登记完全相等）能对上；夹具 S10-normal 也是这么写的。"""
    return {
        "contract_version": CONTRACT_VERSION,
        "project_id": context.get("project_id"),
        "room_id": context.get("room_id"),
        "session_id": context.get("session_id"),
        "source_id": context["source_id"],
        "evidence_id": f"{gap.gap_id}:evidence",
        "media_run_id": gap.gap_media_run_id,
        "asset_id": context.get("asset_id"),
        "evidence_type": "gap",
        "start_ms": None,
        "end_ms": None,
        "sha256": None,
        "status": "missing",
    }


def build_observation(*, obs_type: str, context: dict, gap: Gap, payload: dict, start_ms: int, end_ms: int,
                      model_name: str = MODEL_NAME, model_version: str = MODEL_VERSION,
                      quality_flags: tuple = ()) -> dict:
    """按 observation.schema.json 2.0.0 构造一条健康观察。

    context 至少含 source_id、occurred_at、media_run_id、asset_id 与场次 / 快照字段（缺失为 None）。
    观察用的是发生时刻的快照（contract runtime-v2「恢复观察仍使用发生时的快照」），缺口自己保留原轮次。
    """
    bound = all(context.get(k) is not None for k in SCOPE_FIELDS + SNAPSHOT_FIELDS)
    flags = list(dict.fromkeys(f for f in quality_flags if f))
    if not bound and "binding_missing" not in flags:
        flags.append("binding_missing")
    end_ms = int(end_ms)
    start_ms = min(int(start_ms), end_ms)
    return {
        "contract_version": CONTRACT_VERSION,
        "project_id": context.get("project_id"),
        "room_id": context.get("room_id"),
        "session_id": context.get("session_id"),
        "source_id": context["source_id"],
        "stage_snapshot_id": context.get("stage_snapshot_id") if bound else None,
        "phase_id": context.get("phase_id") if bound else None,
        "phase_version": context.get("phase_version") if bound else None,
        "observation_id": f"{gap.gap_id}:{obs_type}:{context['media_run_id']}:{end_ms}",
        "binding_status": "bound" if bound else "unbound",
        "occurred_at": context["occurred_at"],
        "media_run_id": context["media_run_id"],
        "asset_id": context.get("asset_id"),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "type": obs_type,
        "payload": payload,
        "model_name": model_name,
        "model_version": model_version,
        "quality_flags": flags,
        "evidence_refs": [gap_evidence(context, gap)],
    }


def to_gray(frame) -> np.ndarray:
    """把入口给的帧统一成 uint8 灰度图；不可用的帧抛 FrameError。"""
    if not isinstance(frame, np.ndarray) or frame.size == 0 or frame.ndim not in (2, 3):
        raise FrameError("frame 必须是非空的二维或三维 ndarray")
    if frame.ndim == 3:
        channels = frame.shape[2]
        if channels == 1:
            frame = frame[:, :, 0]
        elif channels == 3:
            frame = cv2.cvtColor(_as_uint8(frame), cv2.COLOR_BGR2GRAY)
        elif channels == 4:
            frame = cv2.cvtColor(_as_uint8(frame), cv2.COLOR_BGRA2GRAY)
        else:
            raise FrameError(f"不支持的通道数：{channels}")
    gray = _as_uint8(frame)
    if gray.shape[0] < MIN_ROI_SIDE or gray.shape[1] < MIN_ROI_SIDE:
        raise FrameError("frame 太小")
    return gray


def _as_uint8(frame: np.ndarray) -> np.ndarray:
    if frame.dtype == np.uint8:
        return frame
    if frame.dtype == np.uint16:
        return (frame // 257).astype(np.uint8)
    if np.issubdtype(frame.dtype, np.floating):
        if not np.all(np.isfinite(frame)):
            raise FrameError("frame 含 NaN / Inf")
        scale = 255.0 if float(frame.max()) <= 1.0 else 1.0
        return np.clip(frame * scale, 0, 255).astype(np.uint8)
    if np.issubdtype(frame.dtype, np.integer):
        return np.clip(frame, 0, 255).astype(np.uint8)
    raise FrameError(f"不支持的 dtype：{frame.dtype}")


def _crop(gray: np.ndarray, roi: tuple) -> np.ndarray:
    h, w = gray.shape[:2]
    x1, y1 = int(round(roi[0] * w)), int(round(roi[1] * h))
    x2, y2 = int(round(roi[2] * w)), int(round(roi[3] * h))
    return gray[y1:y2, x1:x2]


class StageHealthMonitor:
    """一个源一个实例。状态：stream = waiting | online | offline；quality = good | degraded。"""

    def __init__(self, config: Optional[StageHealthConfig] = None, registry: Optional[GapRegistry] = None) -> None:
        self.config = config or StageHealthConfig()
        self.registry = registry or GapRegistry()
        self._source_id: Optional[str] = None
        self._media_run_id: Optional[str] = None
        self._stream = "waiting"
        self._quality = "good"
        self._started_at: Optional[datetime] = None
        self._last_seen_at: Optional[datetime] = None
        self._last_frame_at: Optional[datetime] = None
        self._last_frame_start_ms: Optional[int] = None
        self._recovery_since: Optional[datetime] = None
        self._recovery_start_ms: Optional[int] = None
        self._degraded_since: Optional[datetime] = None
        self._degraded_start_ms: Optional[int] = None
        self._degraded_reason: Optional[str] = None
        self._good_since: Optional[datetime] = None
        self._good_start_ms: Optional[int] = None
        self._last_metrics: dict = {}
        self._offset_configured = False
        self._offset_state = "unconfigured"
        self._frame_error: Optional[str] = None
        self._frame_flags: tuple = ()
        self._primed = False

    # ---- 对外 ----
    def reset(self, media_run_id: str) -> None:
        """新媒体轮次：清计时与跟踪，未关闭的缺口原样保留（契约：恢复仍引用原 gap）。"""
        self._media_run_id = media_run_id
        self._started_at = None
        self._last_frame_at = None
        self._last_frame_start_ms = None
        self._recovery_since = None
        self._recovery_start_ms = None
        self._clear_quality_timers()

    def update(self, context: FrameContext, detections: Optional[list] = None) -> list[dict]:
        if self._source_id is not None and context.source_id != self._source_id:
            raise ValueError(f"一个实例只跟一个源：已是 {self._source_id}，收到 {context.source_id}")
        if self._media_run_id is not None and context.media_run_id != self._media_run_id:
            self.reset(context.media_run_id)      # 入口没调 reset 也按新轮次处理，计时不跨轮次
        now = context.when()
        if self._started_at is None:
            self._started_at = now
        self._source_id = context.source_id
        self._media_run_id = context.media_run_id
        self._last_seen_at = now
        if not self._primed:
            self._prime_from_registry(context.source_id)
        self._frame_flags = ()
        if context.frame is None:
            return self._no_frame(context, now)
        try:
            gray = to_gray(context.frame)
        except (FrameError, cv2.error) as exc:
            self._frame_error = str(exc).splitlines()[0][:120]
            return self._no_frame(context, now)   # 不可用的帧等于没有帧，不让异常打断入口循环
        self._frame_error = None
        return self._on_frame(context, now, gray)

    def status(self) -> dict:
        waiting = (self._last_seen_at - self._started_at).total_seconds() if (
            self._stream == "waiting" and self._started_at and self._last_seen_at) else 0.0
        since_frame = (self._last_seen_at - self._last_frame_at).total_seconds() if (
            self._last_frame_at and self._last_seen_at) else None
        return {
            "source_id": self._source_id,
            "media_run_id": self._media_run_id,
            "stream": self._stream,
            "quality": self._quality,
            "degraded_reason": self._degraded_reason if self._quality == "degraded" else None,
            "waiting_seconds": round(waiting, 3),
            "seconds_since_last_frame": None if since_frame is None else round(since_frame, 3),
            "open_gaps": [g.gap_id for g in self.registry.open_gaps(self._source_id)] if self._source_id else [],
            "offset": {"configured": self._offset_configured, "state": self._offset_state},
            "last_metrics": dict(self._last_metrics),
            "frame_error": self._frame_error,
        }

    # ---- 内部 ----
    def _prime_from_registry(self, source_id: str) -> None:
        """重启后 restore() 进来的打开缺口决定初始状态，否则缺口永远关不掉。"""
        self._primed = True
        if self.registry.open_gap(source_id, "offline") is not None:
            self._stream = "offline"
        if self.registry.open_gap(source_id, "quality") is not None:
            self._quality = "degraded"
            self._degraded_reason = self.registry.open_gap(source_id, "quality").technical_reason or None

    def _clear_quality_timers(self) -> None:
        self._degraded_since = None
        self._degraded_start_ms = None
        self._good_since = None
        self._good_start_ms = None

    def _go_offline(self, ctx: FrameContext, start_ms: int, reason: str) -> dict:
        self._stream = "offline"
        self._recovery_since = None
        self._clear_quality_timers()                 # 断流期间的画质计时作废，恢复后重新确认
        gap = self.registry.open(source_id=ctx.source_id, kind="offline", media_run_id=ctx.media_run_id,
                                 start_ms=start_ms, occurred_at=ctx.occurred_at, scope=ctx.scope(),
                                 technical_reason=reason)
        return self._emit_open("video_offline", ctx, gap, {})

    def _no_frame(self, ctx: FrameContext, now: datetime) -> list[dict]:
        cfg = self.config
        out: list[dict] = []
        had_frames = self._last_frame_at is not None
        if had_frames and (now - self._last_frame_at).total_seconds() >= cfg.offline_seconds:
            if self._stream in ("online", "waiting"):
                out.append(self._go_offline(ctx, self._last_frame_start_ms, "no_frame"))
            elif self._recovery_since is not None:
                self._recovery_since = None          # 恢复中途又断：恢复计时作废，缺口仍是原来那条
            return out
        if self._stream == "waiting" and not had_frames:
            waited = (now - self._started_at).total_seconds()
            if waited >= cfg.offline_seconds and self.registry.open_gap(ctx.source_id, "offline") is None:
                gap = self.registry.open(source_id=ctx.source_id, kind="offline", media_run_id=ctx.media_run_id,
                                         start_ms=0, occurred_at=ctx.occurred_at, scope=ctx.scope(),
                                         technical_reason="no_stream_at_start")
                out.append(self._emit_open("video_offline", ctx, gap, {}))
        return out

    def _on_frame(self, ctx: FrameContext, now: datetime, gray: np.ndarray) -> list[dict]:
        cfg = self.config
        out: list[dict] = []
        if self._last_frame_at is not None:
            if self._stream in ("online", "waiting") and \
                    (now - self._last_frame_at).total_seconds() >= cfg.offline_seconds:
                # 单次读帧阻塞很久才回来：入口没机会报无帧，这里追溯开缺口
                out.append(self._go_offline(ctx, self._last_frame_start_ms, "no_frame"))
            if self._last_frame_start_ms is not None and ctx.start_ms < self._last_frame_start_ms:
                self._frame_flags = ("start_ms_not_monotonic",)
        self._last_frame_at = now
        self._last_frame_start_ms = ctx.start_ms

        if self._stream in ("waiting", "offline"):
            if self._recovery_since is None:
                self._recovery_since = now
                self._recovery_start_ms = ctx.start_ms
            if (now - self._recovery_since).total_seconds() >= cfg.recovery_seconds:
                self._stream = "online"
                gap = self.registry.open_gap(ctx.source_id, "offline")
                if gap is not None:
                    self.registry.close(gap.gap_id, media_run_id=ctx.media_run_id, end_ms=self._recovery_start_ms,
                                        occurred_at=ctx.occurred_at)
                    out.append(self._emit_close("video_online", ctx, gap, self._recovery_start_ms, "frames_resumed"))
                self._recovery_since = None

        metrics, reasons = self._measure(ctx, gray)
        self._last_metrics = metrics
        if reasons:
            self._good_since = None
            self._good_start_ms = None
            if self._degraded_since is None:
                self._degraded_since = now
                self._degraded_start_ms = ctx.start_ms
                self._degraded_reason = reasons[0]
            if self._quality == "good" and (now - self._degraded_since).total_seconds() >= cfg.degraded_seconds:
                self._quality = "degraded"
                gap = self.registry.open(source_id=ctx.source_id, kind="quality", media_run_id=ctx.media_run_id,
                                         start_ms=self._degraded_start_ms, occurred_at=ctx.occurred_at,
                                         scope=ctx.scope(), technical_reason=self._degraded_reason)
                out.append(self._emit_open("camera_blacked", ctx, gap, metrics, reason=self._degraded_reason))
        else:
            self._degraded_since = None
            self._degraded_start_ms = None
            gap = self.registry.open_gap(ctx.source_id, "quality")   # 查登记而不是查内存状态：restore 进来的缺口也要能关
            if gap is not None:
                self._quality = "degraded"
                if self._good_since is None:
                    self._good_since = now
                    self._good_start_ms = ctx.start_ms
                if (now - self._good_since).total_seconds() >= cfg.recovery_seconds:
                    self.registry.close(gap.gap_id, media_run_id=ctx.media_run_id, end_ms=self._good_start_ms,
                                        occurred_at=ctx.occurred_at)
                    out.append(self._emit_close("camera_normal", ctx, gap, self._good_start_ms, "quality_resumed",
                                                extra=metrics))
                    self._quality = "good"
                    self._degraded_reason = None
                    self._good_since = None
                    self._good_start_ms = None
            else:
                self._quality = "good"
                self._degraded_reason = None
                self._good_since = None
        return out

    def _measure(self, ctx: FrameContext, gray: np.ndarray) -> tuple[dict, list[str]]:
        cfg = self.config
        variance = float(gray.var())
        mean = float(gray.mean())
        laplacian = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        metrics = {"variance": round(variance, 3), "mean": round(mean, 3), "laplacian_var": round(laplacian, 3)}
        reasons: list[str] = []
        if variance < cfg.black_var_threshold:
            reasons.append("bright" if mean > cfg.bright_mean else "blacked")
        elif mean < cfg.dark_mean:
            reasons.append("dark")
        elif mean > cfg.bright_mean:
            reasons.append("bright")
        elif laplacian < cfg.blur_threshold:
            reasons.append("blurred")

        # 偏移：参考图与 ROI 都有才算配置好（执行卡:247「参考图/ROI 缺少则偏移待配置」）
        self._offset_configured = ctx.reference_frame is not None and ctx.roi is not None
        if not self._offset_configured:
            self._offset_state = "unconfigured"
        elif variance < cfg.black_var_threshold:
            self._offset_state = "unmeasurable"          # 画面无纹理，位移算不准，不下结论
        else:
            try:
                ref = _crop(to_gray(ctx.reference_frame), ctx.roi)
            except (FrameError, cv2.error):
                ref = None
            cur = _crop(gray, ctx.roi)
            if ref is None or ref.shape != cur.shape:
                self._offset_state = "size_mismatch"
            elif min(cur.shape[:2]) < MIN_ROI_SIDE:
                self._offset_state = "unmeasurable"      # ROI 裁出来不足 2×2，不调相位相关
            else:
                (dx, dy), response = cv2.phaseCorrelate(ref.astype(np.float32), cur.astype(np.float32))
                shift = math.hypot(dx, dy)
                metrics["shift_px"] = round(shift, 2)
                metrics["shift_response"] = round(float(response), 4)
                if shift > cfg.shift_px:
                    self._offset_state = "shifted"
                    reasons.append("shifted")
                else:
                    self._offset_state = "aligned"
        return metrics, reasons

    # ---- 观察构造 ----
    def _emit_open(self, obs_type: str, ctx: FrameContext, gap: Gap, metrics: dict, reason: Optional[str] = None) -> dict:
        payload = {"gap_id": gap.gap_id, "gap_media_run_id": gap.gap_media_run_id, "gap_start_ms": gap.open_start_ms,
                   "technical_reason": reason or gap.technical_reason}
        payload.update({k: v for k, v in metrics.items() if v is not None})
        start_ms = gap.open_start_ms if gap.gap_media_run_id == ctx.media_run_id else ctx.start_ms
        return build_observation(obs_type=obs_type, context=ctx.fields(), gap=gap, payload=payload,
                                 start_ms=start_ms, end_ms=ctx.start_ms, quality_flags=self._frame_flags)

    def _emit_close(self, obs_type: str, ctx: FrameContext, gap: Gap, resumed_ms: int, reason: str,
                    extra: Optional[dict] = None) -> dict:
        same_run = gap.gap_media_run_id == ctx.media_run_id
        payload = {"gap_id": gap.gap_id, "gap_media_run_id": gap.gap_media_run_id, "gap_start_ms": gap.open_start_ms,
                   "gap_end_ms": resumed_ms, "gap_duration_ms": (resumed_ms - gap.open_start_ms) if same_run else None,
                   "technical_reason": reason}
        payload.update({k: v for k, v in (extra or {}).items() if v is not None})
        return build_observation(obs_type=obs_type, context=ctx.fields(), gap=gap, payload=payload,
                                 start_ms=resumed_ms, end_ms=ctx.start_ms, quality_flags=self._frame_flags)


def adapt_legacy_message(message: dict, *, media_run_id: Optional[str], start_ms: int,
                         registry: GapRegistry, scope: Optional[dict] = None) -> dict:
    """把旧入口 publish() 的消息（type / source_id / session_id / occurred_at 秒浮点 / payload JSON 串）转成 2.0.0 观察。

    旧消息没有 gap_id 与媒体轮次，媒体轮次和相对毫秒由调用方（10.1 接线）提供；旧消息里的 session_id 是
    默认值 session-001 之类的猜测，一律不用，场次与快照只认调用方给的 scope。转不了返回 {"cannot_adapt": 原因}。
    只用于 10.1 接线过渡期；同一源跨轮次仍打开的旧缺口会被复用（契约「恢复仍引用原 gap」）。
    """
    obs_type = message.get("type")
    if obs_type not in LEGACY_TYPES:
        return {"cannot_adapt": "unsupported_type"}
    source_id = message.get("source_id")
    if not isinstance(source_id, str) or not source_id.strip():
        return {"cannot_adapt": "missing_source_id"}
    if not media_run_id:
        return {"cannot_adapt": "missing_media_run_id"}
    occurred = message.get("occurred_at")
    if isinstance(occurred, (int, float)) and not isinstance(occurred, bool):
        occurred_at = format_utc(datetime.fromtimestamp(float(occurred), tz=timezone.utc))
    elif isinstance(occurred, str) and UTC_PATTERN.match(occurred):
        occurred_at = occurred
    else:
        return {"cannot_adapt": "bad_occurred_at"}
    raw = message.get("payload") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    raw = raw if isinstance(raw, dict) else {}
    scope = scope or {}
    context = {"source_id": source_id, "occurred_at": occurred_at, "media_run_id": media_run_id,
               "asset_id": scope.get("asset_id")}
    context.update({k: scope.get(k) for k in SCOPE_FIELDS + SNAPSHOT_FIELDS})
    kind, action = LEGACY_TYPES[obs_type]
    start_ms = int(start_ms)
    if action == "open":
        reason = raw.get("reason") or ("no_frame" if kind == "offline" else "blacked")
        gap = registry.open(source_id=source_id, kind=kind, media_run_id=media_run_id, start_ms=start_ms,
                            occurred_at=occurred_at, scope={k: scope.get(k) for k in SCOPE_FIELDS},
                            technical_reason=reason)
        payload = {"gap_id": gap.gap_id, "gap_media_run_id": gap.gap_media_run_id, "gap_start_ms": gap.open_start_ms,
                   "technical_reason": reason, "legacy": True}
        if isinstance(raw.get("variance"), (int, float)):
            payload["variance"] = raw["variance"]
        open_start = gap.open_start_ms if gap.gap_media_run_id == media_run_id else start_ms
        return build_observation(obs_type=obs_type, context=context, gap=gap, payload=payload,
                                 start_ms=open_start, end_ms=start_ms)
    gap = registry.open_gap(source_id, kind)
    if gap is None:
        return {"cannot_adapt": "no_open_gap"}
    registry.close(gap.gap_id, media_run_id=media_run_id, end_ms=start_ms, occurred_at=occurred_at)
    same_run = gap.gap_media_run_id == media_run_id
    payload = {"gap_id": gap.gap_id, "gap_media_run_id": gap.gap_media_run_id, "gap_start_ms": gap.open_start_ms,
               "gap_end_ms": start_ms, "gap_duration_ms": (start_ms - gap.open_start_ms) if same_run else None,
               "technical_reason": "frames_resumed" if kind == "offline" else "quality_resumed", "legacy": True}
    return build_observation(obs_type=obs_type, context=context, gap=gap, payload=payload,
                             start_ms=start_ms, end_ms=start_ms)
