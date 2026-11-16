"""阶段媒体控制器（执行卡 4.2）：按阶段快照把授权素材推到 MediaMTX，回 ControlAck，记媒体运行记录。

一个进程只服务一个明确的 session_id + source_id（环境项必填，没有默认 session-001 / camera-001）。
输入：持久配置（启动补查 + phase-control 通知，按 notification_id 去重、按版本保护）与 4.1 登记表里的素材。
输出：FFmpeg 推流（参数数组，不拼 shell）、ControlAck（只有探针确认在发布才 applied）、运行记录文件。
所有外部组件（素材解析、发布器、探针、通知、ACK、时钟）都可注入，测试用假对象，真实实现在本文件下半部分。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from media_run_registry import MediaRun, MediaRunRegistry, format_utc, parse_utc  # noqa: E402

log = logging.getLogger("stage-media-controller")
CONTRACT_VERSION = "2.0.0"
DEFAULT_CONSUMER_ID = "media-controller"
ASSET_MISSING = {"code": "ASSET_MISSING", "message": "所需素材不可用"}
APPLY_FAILED = {"code": "APPLY_FAILED", "message": "消费者应用失败"}
PHASES = ("OPENING", "ATTENDANCE", "RECUSAL", "PRELIMINARY", "DETAILED", "CLARIFICATION", "SCORING", "REPORT", "OFFLINE_QA")
MAX_SEEN_NOTIFICATIONS = 10000


def redact_url(url: str) -> str:
    """日志里只留主机与路径，去掉 user:pass（K-07）。"""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = parts.port
    except ValueError:
        return "<url>"
    if port:
        host = f"{host}:{port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


# ---------------------------------------------------------------- 协议与数据
@dataclass(frozen=True)
class ResolvedAsset:
    asset_id: str
    path: Path
    duration_ms: Optional[int]
    sha256: Optional[str]


class AssetResolver(Protocol):
    def resolve(self, session_id: str, asset_id: str) -> Optional[ResolvedAsset]: ...


class Publisher(Protocol):
    def start(self, path: Path, publish_url: str) -> object: ...
    def poll(self, handle: object) -> Optional[int]: ...
    def stop(self, handle: object, grace_seconds: float) -> None: ...


class StreamProbe(Protocol):
    def is_publishing(self, path_name: str, since: Optional[datetime] = None) -> Optional[bool]: ...


class AckSender(Protocol):
    def send(self, session_id: str, ack: dict) -> None: ...


class ConfigFetcher(Protocol):
    def fetch(self, session_id: str) -> Optional[dict]: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass
class ControllerConfig:
    session_id: str
    source_id: str
    consumer_id: str = DEFAULT_CONSUMER_ID
    publish_url: str = "rtsp://mediamtx:8554/cam1"
    confirm_seconds: float = 5.0
    confirm_poll_seconds: float = 0.5
    stop_grace_seconds: float = 3.0
    verify_sha256: bool = False
    max_restarts: int = 3
    refresh_interval_seconds: float = 30.0
    ack_retry_seconds: float = 5.0

    def __post_init__(self) -> None:
        for name in ("session_id", "source_id", "consumer_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 必填，没有默认值")
        self.publish_path = urlsplit(self.publish_url).path.strip("/")
        if not self.publish_path:
            raise ValueError("发布地址必须带路径名，如 rtsp://mediamtx:8554/cam1")


# ---------------------------------------------------------------- 状态机
class MediaController:
    """apply_config / handle_messages / tick / recover / shutdown。一个实例一个源。"""

    def __init__(self, config: ControllerConfig, *, registry: MediaRunRegistry, resolver: AssetResolver,
                 publisher: Publisher, probe: Optional[StreamProbe], acks: Optional[AckSender],
                 fetcher: Optional[ConfigFetcher] = None, clock: Optional[Clock] = None,
                 run_sink: Optional[Callable[[dict], None]] = None) -> None:
        self.config = config
        self.registry = registry
        self.resolver = resolver
        self.publisher = publisher
        self.probe = probe
        self.acks = acks
        self.fetcher = fetcher
        self.clock = clock or SystemClock()
        self.run_sink = run_sink
        self._handle: Optional[object] = None
        self._run: Optional[MediaRun] = None
        self._asset: Optional[ResolvedAsset] = None
        self._applied: Optional[dict] = None       # 当前生效快照（可能是 missing_asset 的）
        self._seen_notifications: dict[str, int] = {}
        self._restarts = 0
        self._probe_unavailable = False
        self._last_ack: Optional[dict] = None
        self._pending_ack: Optional[dict] = None
        self._pending_ack_at: Optional[float] = None
        self._last_ack_error: Optional[str] = None
        self._min_version = 0                     # 重启后从运行记录取的版本下限
        self._last_refresh_at: Optional[float] = None

    # ---- 对外 ----
    def recover(self) -> Optional[dict]:
        """启动：把上次残留的 run 收尾，再补查持久配置并应用。"""
        now = format_utc(self.clock.now())
        closed = self.registry.close_dangling(at=now, source_id=self.config.source_id)
        for run in closed:
            self._emit_run(run)
        history = self.registry.runs(self.config.source_id)
        if history:
            self._min_version = max(run.phase_version for run in history)
        self._last_refresh_at = self.clock.now().timestamp()
        config = self._fetch()
        if config is None:
            return None
        return self.apply_config(config, received_at=now)

    def _fetch(self) -> Optional[dict]:
        if self.fetcher is None:
            return None
        try:
            return self.fetcher.fetch(self.config.session_id)
        except Exception as exc:  # noqa: BLE001  网络错误只记固定码
            log.warning("补查持久配置失败: %s", _failure_code(exc))
            return None

    def maybe_refresh(self) -> Optional[dict]:
        """周期补查持久配置（2.2 要求启动和周期恢复都补查）；只应用比当前更新的版本。"""
        if self.fetcher is None:
            return None
        now = self.clock.now().timestamp()
        if self._last_refresh_at is not None and now - self._last_refresh_at < self.config.refresh_interval_seconds:
            return None
        self._last_refresh_at = now
        config = self._fetch()
        if config is None or not self._valid_config(config):
            return None
        if config["phase_version"] <= self._current_version():
            return None
        return self.apply_config(config, received_at=format_utc(self.clock.now()))

    def _current_version(self) -> int:
        return self._applied["phase_version"] if self._applied else self._min_version

    def handle_messages(self, messages: list[dict]) -> list[dict]:
        """一批通知只应用本场次本源的最高版本；其余去重 / 忽略。返回发出的 ACK 列表。"""
        candidates: dict[int, dict] = {}
        for message in messages:
            parsed = self._parse_message(message)
            if parsed is None:
                continue
            notification_id, config = parsed
            version = config["phase_version"]
            if notification_id in self._seen_notifications:
                continue
            self._seen_notifications[notification_id] = version
            if len(self._seen_notifications) > MAX_SEEN_NOTIFICATIONS:
                for key in list(self._seen_notifications)[: len(self._seen_notifications) - MAX_SEEN_NOTIFICATIONS]:
                    del self._seen_notifications[key]
            candidates[version] = config
        if not candidates:
            return []
        top = max(candidates)
        current = self._current_version()
        if top <= current:
            log.info("忽略不高于当前版本 %s 的通知（最高 %s）", current, top)
            return []
        ack = self.apply_config(candidates[top], received_at=format_utc(self.clock.now()))
        return [ack] if ack else []

    def apply_config(self, config: dict, *, received_at: str) -> Optional[dict]:
        if not self._valid_config(config):
            log.warning("快照形状非法或不属于本控制器的场次 / 源，忽略（不做任何副作用）")
            return None
        if config["phase_version"] < self._current_version():
            log.info("旧版本 %s 不覆盖当前 %s", config["phase_version"], self._current_version())
            return None
        if self._applied and config["phase_version"] == self._applied["phase_version"]:
            if config["stage_snapshot_id"] == self._applied["stage_snapshot_id"]:
                log.info("快照 %s 已是当前生效配置，不重复应用", config["stage_snapshot_id"])
            else:
                log.warning("同版本 %s 出现不同快照，按冲突忽略，不应用", config["phase_version"])
            return None
        asset_entry = next((a for a in config.get("assets", []) if a.get("slot") == "video" and a.get("source_id") == self.config.source_id), None)
        resolved = self._resolve(asset_entry["asset_id"]) if asset_entry else None
        self._stop_current(reason="phase_switch", planned=True)
        self._applied = config
        self._restarts = 0
        if resolved is None:
            self._asset = None
            return self._send(self._ack(config, received_at, "missing_asset", error=ASSET_MISSING))
        self._asset = resolved
        ok, confirmed_at, note = self._start_run(config, resolved, loop_index=0)
        if not ok:
            return self._send(self._ack(config, received_at, "failed", error=APPLY_FAILED))
        return self._send(self._ack(config, received_at, "applied", applied_at=confirmed_at))

    def tick(self) -> None:
        """周期调用：进程正常退出 → 下一轮；异常退出 → 有限次重起，超过回 failed；另做周期补查与 ACK 重试。"""
        self._retry_ack()
        self.maybe_refresh()
        if self._handle is None or self._run is None:
            return
        code = self.publisher.poll(self._handle)
        if code is None:
            return
        now = format_utc(self.clock.now())
        finished = self._run
        self._handle = None
        if code == 0:
            self.registry.stop(finished.media_run_id, stopped_at=now, reason="loop_end", planned=True)
            self._emit_run(finished)
            self._run = None
            self._restarts = 0                     # 完整播完一轮即视为已恢复，重置重启预算
            if self._applied is not None and self._asset is not None:
                ok, _, _ = self._start_run(self._applied, self._asset, loop_index=finished.loop_index + 1)
                if not ok:
                    self._send(self._ack(self._applied, now, "failed", error=APPLY_FAILED))
            return
        self.registry.fail(finished.media_run_id, failed_at=now, reason="process_exit", note=f"exit={code}")
        self._emit_run(finished)
        self._run = None
        self._restarts += 1
        if self._restarts <= self.config.max_restarts and self._applied is not None and self._asset is not None:
            ok, _, _ = self._start_run(self._applied, self._asset, loop_index=finished.loop_index + 1)
            if ok:
                return
        if self._applied is not None:
            self._send(self._ack(self._applied, now, "failed", error=APPLY_FAILED))

    def shutdown(self) -> None:
        self._stop_current(reason="shutdown", planned=True)

    def status(self) -> dict:
        run = self._run.to_dict() if self._run else None
        return {
            "session_id": self.config.session_id,
            "source_id": self.config.source_id,
            "consumer_id": self.config.consumer_id,
            "publish_path": self.config.publish_path,
            "applied_phase_version": self._applied["phase_version"] if self._applied else None,
            "applied_snapshot": self._applied["stage_snapshot_id"] if self._applied else None,
            "current_run": run,
            "ack_disabled": self.acks is None,
            "probe_unavailable": self._probe_unavailable,
            "last_ack_status": self._last_ack["status"] if self._last_ack else None,
            "ack_pending": self._pending_ack is not None,
            "last_ack_error": self._last_ack_error,
            "version_floor": self._min_version,
            "restarts": self._restarts,
        }

    # ---- 内部 ----
    def _valid_config(self, config: dict) -> bool:
        """完整形状校验，在任何副作用之前；不合格一律丢弃。"""
        if not isinstance(config, dict):
            return False
        for key in ("project_id", "room_id", "session_id", "stage_snapshot_id"):
            if not isinstance(config.get(key), str) or not config[key].strip():
                return False
        if config["session_id"] != self.config.session_id:
            return False
        if config.get("source_id") not in (self.config.source_id, None):
            return False
        if config.get("phase_id") not in PHASES:
            return False
        version = config.get("phase_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            return False
        assets = config.get("assets")
        if not isinstance(assets, list):
            return False
        for asset in assets:
            if not isinstance(asset, dict) or asset.get("slot") not in ("video", "audio", "scoring"):
                return False
            if not isinstance(asset.get("source_id"), str) or not isinstance(asset.get("asset_id"), str) or not asset["asset_id"].strip():
                return False
        return True

    _config_is_mine = _valid_config

    def _parse_message(self, message: dict) -> Optional[tuple[str, dict]]:
        if not isinstance(message, dict) or message.get("type") != "phase_changed":
            return None
        notification_id = message.get("notification_id")
        if not isinstance(notification_id, str) or not notification_id:
            return None
        if message.get("session_id") != self.config.session_id:
            return None
        if message.get("source_id") not in (self.config.source_id, None, ""):
            return None
        payload = message.get("payload")
        try:
            config = json.loads(payload) if isinstance(payload, str) else payload
        except json.JSONDecodeError:
            log.warning("通知 payload 不是合法 JSON，丢弃")
            return None
        if not self._valid_config(config):
            return None
        return notification_id, config

    def _resolve(self, asset_id: str) -> Optional[ResolvedAsset]:
        try:
            asset = self.resolver.resolve(self.config.session_id, asset_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("素材解析失败: %s", type(exc).__name__)
            return None
        if asset is None or not asset.path.is_file():
            return None
        if self.config.verify_sha256 and asset.sha256:
            digest = hashlib.sha256()
            with asset.path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != asset.sha256:
                log.warning("素材摘要与登记不符，按缺失处理")
                return None
        return asset

    def _start_run(self, config: dict, asset: ResolvedAsset, *, loop_index: int) -> tuple[bool, Optional[str], Optional[str]]:
        started_at = format_utc(self.clock.now())
        run = self.registry.start(session_id=self.config.session_id, source_id=self.config.source_id, asset_id=asset.asset_id,
                                  stage_snapshot_id=config["stage_snapshot_id"], phase_id=config["phase_id"],
                                  phase_version=config["phase_version"], started_at=started_at,
                                  asset_duration_ms=asset.duration_ms, loop_index=loop_index)
        self._run = run
        try:
            self._handle = self.publisher.start(asset.path, self.config.publish_url)
        except Exception as exc:  # noqa: BLE001
            self._handle = None
            self.registry.fail(run.media_run_id, failed_at=format_utc(self.clock.now()), note=type(exc).__name__)
            self._emit_run(run)
            self._run = None
            return False, None, type(exc).__name__
        confirmed, note = self._confirm()
        if not confirmed:
            self._stop_handle()
            self.registry.fail(run.media_run_id, failed_at=format_utc(self.clock.now()), note=note)
            self._emit_run(run)
            self._run = None
            return False, None, note
        confirmed_at = format_utc(self.clock.now())
        self.registry.mark_playing(run.media_run_id, confirmed_at)
        self._emit_run(run)
        return True, confirmed_at, None

    def _confirm(self) -> tuple[bool, Optional[str]]:
        """探针确认在发布；探针不可用则进程存活满 confirm_seconds 才算，并记 probe_unavailable。"""
        deadline = self.clock.now().timestamp() + self.config.confirm_seconds
        alive_since = self.clock.now().timestamp()
        since = parse_utc(self._run.started_at) if self._run is not None else None
        while True:
            if self.publisher.poll(self._handle) is not None:
                return False, "process_exited_before_confirm"
            result = self.probe.is_publishing(self.config.publish_path, since=since) if self.probe is not None else None
            if result is True:
                self._probe_unavailable = False
                return True, None
            if result is None:
                self._probe_unavailable = True
                if self.clock.now().timestamp() - alive_since >= self.config.confirm_seconds:
                    return True, "probe_unavailable"
            if self.clock.now().timestamp() >= deadline:
                return False, "confirm_timeout" if result is False else "probe_unavailable_timeout"
            self.clock.sleep(self.config.confirm_poll_seconds)

    def _stop_handle(self) -> None:
        if self._handle is None:
            return
        try:
            self.publisher.stop(self._handle, self.config.stop_grace_seconds)
        finally:
            self._handle = None

    def _stop_current(self, *, reason: str, planned: bool) -> None:
        self._stop_handle()
        if self._run is not None:
            self.registry.stop(self._run.media_run_id, stopped_at=format_utc(self.clock.now()), reason=reason, planned=planned)
            self._emit_run(self._run)
            self._run = None

    def _ack(self, config: dict, received_at: str, status: str, *, applied_at: Optional[str] = None,
             error: Optional[dict] = None) -> dict:
        if status == "applied":
            if applied_at is None or error is not None:
                raise ValueError("applied 必须有 applied_at 且 error 为 null")
            if parse_utc(applied_at) < parse_utc(received_at):
                applied_at = received_at
        else:
            applied_at = None
            if error is None:
                raise ValueError("非 applied 必须带脱敏 error")
        source_id = config["source_id"] if isinstance(config.get("source_id"), str) and config["source_id"].strip() else self.config.source_id
        return {
            "contract_version": CONTRACT_VERSION,
            "project_id": config["project_id"],
            "room_id": config["room_id"],
            "session_id": config["session_id"],
            "source_id": source_id,
            "stage_snapshot_id": config["stage_snapshot_id"],
            "phase_id": config["phase_id"],
            "phase_version": config["phase_version"],
            "consumer_id": self.config.consumer_id,
            "received_at": received_at,
            "applied_at": applied_at,
            "status": status,
            "error": dict(error) if error else None,
        }

    def _send(self, ack: dict) -> dict:
        self._last_ack = ack
        self._pending_ack = None
        if self.acks is None:
            log.info("未配置 ACK 凭据，状态 %s 只记本地（ack_disabled）", ack["status"])
            return ack
        try:
            self.acks.send(ack["session_id"], ack)
            self._last_ack_error = None
        except Exception as exc:  # noqa: BLE001
            self._last_ack_error = _failure_code(exc)
            self._pending_ack = ack
            self._pending_ack_at = self.clock.now().timestamp()
            log.warning("ACK 发送失败: %s，稍后重试", self._last_ack_error)
        return ack

    def _retry_ack(self) -> None:
        if self._pending_ack is None or self.acks is None:
            return
        if self._pending_ack_at is not None and self.clock.now().timestamp() - self._pending_ack_at < self.config.ack_retry_seconds:
            return
        ack = self._pending_ack
        try:
            self.acks.send(ack["session_id"], ack)
            self._pending_ack = None
            self._last_ack_error = None
        except Exception as exc:  # noqa: BLE001
            self._last_ack_error = _failure_code(exc)
            self._pending_ack_at = self.clock.now().timestamp()

    def _emit_run(self, run: MediaRun) -> None:
        if self.run_sink is not None:
            try:
                self.run_sink(run.to_dict())
            except Exception as exc:  # noqa: BLE001
                log.warning("运行记录通知失败: %s", type(exc).__name__)


def _failure_code(exc: BaseException) -> str:
    """HTTP 失败压成固定码（含状态码、不含 URL / 凭据）。"""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP_{exc.code}"
    return type(exc).__name__


# ---------------------------------------------------------------- 真实实现
class FfmpegPublisher:
    """参数数组起 ffmpeg（不走 shell），每轮一个进程，不用 -stream_loop。"""

    def __init__(self, binary: str = "ffmpeg", extra_args: Optional[list[str]] = None) -> None:
        self.binary = binary
        self.extra_args = list(extra_args or [])

    def build_args(self, path: Path, publish_url: str) -> list[str]:
        return [self.binary, "-hide_banner", "-loglevel", "error", "-nostdin", "-re", "-i", str(path),
                *self.extra_args, "-c", "copy", "-f", "rtsp", "-rtsp_transport", "tcp", publish_url]

    def start(self, path: Path, publish_url: str) -> subprocess.Popen:
        kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        log.info("起流: %s -> %s", path.name, redact_url(publish_url))
        return subprocess.Popen(self.build_args(path, publish_url), **kwargs)

    def poll(self, handle: subprocess.Popen) -> Optional[int]:
        return handle.poll()

    def stop(self, handle: subprocess.Popen, grace_seconds: float) -> None:
        stop_process(handle, grace_seconds)


def stop_process(proc: subprocess.Popen, grace_seconds: float) -> int:
    """terminate → 等 grace → kill → 等；返回退出码。进程已退出直接返回。"""
    if proc.poll() is not None:
        return proc.returncode
    try:
        if os.name != "nt":
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()
    try:
        return proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name != "nt":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()
    try:
        return proc.wait(timeout=max(grace_seconds, 5))
    except subprocess.TimeoutExpired:
        log.error("进程 kill 后仍未退出（pid=%s），交由系统回收", proc.pid)
        return None


class MediaMtxProbe:
    """MediaMTX v3 API：GET /v3/paths/get/{name} → ready。网络错误返回 None（不可知）。"""

    def __init__(self, api_url: str, timeout: float = 2.0, opener: Optional[Callable] = None) -> None:
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.opener = opener or urllib.request.urlopen

    def is_publishing(self, path_name: str, since: Optional[datetime] = None) -> Optional[bool]:
        """ready 且（给了 since 时）readyTime 不早于 since 才算本轮在发布；readyTime 缺失或不可解析按不可知（None）。"""
        request = urllib.request.Request(f"{self.api_url}/v3/paths/get/{path_name}", headers={"Accept": "application/json"})
        try:
            with self.opener(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return False if exc.code == 404 else None
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return None
        ready = data.get("ready") if isinstance(data, dict) else None
        if not isinstance(ready, bool):
            return None
        if not ready:
            return False
        if since is None:
            return True
        ready_time = parse_rfc3339(data.get("readyTime"))
        if ready_time is None:
            return None
        return ready_time >= since - timedelta(seconds=1)


def parse_rfc3339(value) -> Optional[datetime]:
    """MediaMTX 的 readyTime 形如 2024-01-01T00:00:00.123456789Z；小数截到微秒。"""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, tail = text.split(".", 1)
        digits = ""
        rest = ""
        for index, ch in enumerate(tail):
            if ch.isdigit():
                digits += ch
            else:
                rest = tail[index:]
                break
        text = f"{head}.{digits[:6].ljust(6, '0')}{rest}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class _BearerHttp:
    def __init__(self, base_url: str, token: str, timeout: float = 5.0, opener: Optional[Callable] = None) -> None:
        if not token or not token.strip():
            raise ValueError("token 不能为空")
        self.base_url = base_url.rstrip("/")
        self._token = token.strip()
        self.timeout = timeout
        self.opener = opener or urllib.request.urlopen

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> Optional[dict]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}", "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {})})
        with self.opener(request, timeout=self.timeout) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else None

    def __repr__(self) -> str:   # 不泄露 token
        return f"<{type(self).__name__} {self.base_url}>"


class HttpConfigFetcher(_BearerHttp):
    def fetch(self, session_id: str) -> Optional[dict]:
        try:
            return self._request("GET", f"/internal/phase-control/sessions/{session_id}/config")
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 409):
                return None
            raise


class HttpAckSender(_BearerHttp):
    def __init__(self, base_url: str, token: str, consumer_id: str, **kwargs) -> None:
        super().__init__(base_url, token, **kwargs)
        self.consumer_id = consumer_id

    def send(self, session_id: str, ack: dict) -> None:
        self._request("PUT", f"/internal/phase-control/sessions/{session_id}/acks/{self.consumer_id}", ack)


class DbAssetResolver:
    """只读 4.1 的 stage_assets 表；路径 = STAGE_ASSETS_ROOT / relative_path，不越出根目录。"""

    def __init__(self, engine, assets_root: Path) -> None:
        self.engine = engine
        self.root = Path(assets_root).resolve()

    def resolve(self, session_id: str, asset_id: str) -> Optional[ResolvedAsset]:
        from sqlalchemy import text
        with self.engine.connect() as connection:
            row = connection.execute(text(
                "SELECT asset_id, relative_path, duration_ms, sha256, asset_type FROM stage_assets "
                "WHERE session_id = :session_id AND asset_id = :asset_id"),
                {"session_id": session_id, "asset_id": asset_id}).mappings().first()
        if row is None or row["asset_type"] != "video":
            return None
        candidate = (self.root / row["relative_path"]).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            log.warning("登记路径解析后越出素材根，按缺失处理")
            return None
        return ResolvedAsset(asset_id=row["asset_id"], path=candidate, duration_ms=row["duration_ms"], sha256=row["sha256"])


class RedisNotifications:
    """phase-control 消费组读取：组名 = consumer_id:source，消费者 = 主机名；处理后 XACK。"""

    def __init__(self, client, stream: str, group: str, consumer: Optional[str] = None, block_ms: int = 1000) -> None:
        self.client = client
        self.stream = stream
        self.group = group
        self.consumer = consumer or socket.gethostname()
        self.block_ms = block_ms
        self._drained_pending = False
        try:
            self.client.xgroup_create(stream, group, id="$", mkstream=True)
        except Exception as exc:  # noqa: BLE001  组已存在
            if "BUSYGROUP" not in str(exc):
                raise

    def read(self) -> list[tuple[str, dict]]:
        """首次先读本消费者崩溃前已投递未确认的条目（id 0），之后只读新条目（>）。"""
        if not self._drained_pending:
            self._drained_pending = True
            pending = self.client.xreadgroup(self.group, self.consumer, {self.stream: "0"}, count=100)
            decoded = self._decode(pending)
            if decoded:
                return decoded
        entries = self.client.xreadgroup(self.group, self.consumer, {self.stream: ">"}, count=100, block=self.block_ms)
        return self._decode(entries)

    @staticmethod
    def _decode(entries) -> list[tuple[str, dict]]:
        result = []
        for _stream, items in entries or []:
            for message_id, data in items:
                decoded = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
                           for k, v in data.items()}
                result.append((message_id.decode() if isinstance(message_id, bytes) else message_id, decoded))
        return result

    def ack(self, message_ids: list[str]) -> None:
        if message_ids:
            self.client.xack(self.stream, self.group, *message_ids)


def read_token(env: dict) -> Optional[str]:
    token = env.get("MEDIA_CONTROL_TOKEN")
    if token:
        return token.strip()
    path = env.get("MEDIA_CONTROL_TOKEN_FILE")
    if path and Path(path).is_file():
        return Path(path).read_text(encoding="utf-8").strip() or None
    return None


def build_from_env(env: Optional[dict] = None) -> tuple[MediaController, Optional[RedisNotifications]]:
    """按环境项组装真实组件。缺少必填项直接抛 ValueError，不猜默认场次。"""
    env = os.environ if env is None else env
    config = ControllerConfig(
        session_id=env.get("MEDIA_SESSION_ID", ""), source_id=env.get("MEDIA_SOURCE_ID", ""),
        consumer_id=env.get("MEDIA_CONSUMER_ID", DEFAULT_CONSUMER_ID),
        publish_url=env.get("RTSP_PUBLISH_URL", "rtsp://mediamtx:8554/cam1"),
        confirm_seconds=float(env.get("MEDIA_CONFIRM_SECONDS", "5")),
        stop_grace_seconds=float(env.get("MEDIA_STOP_GRACE_SECONDS", "3")),
        verify_sha256=env.get("MEDIA_VERIFY_SHA256", "0") == "1",
        refresh_interval_seconds=float(env.get("MEDIA_REFRESH_SECONDS", "30")),
        ack_retry_seconds=float(env.get("MEDIA_ACK_RETRY_SECONDS", "5")))
    root = env.get("STAGE_ASSETS_ROOT")
    if not root or not Path(root).is_absolute() or not Path(root).is_dir():
        raise ValueError("STAGE_ASSETS_ROOT 必须是存在的绝对目录")
    from sqlalchemy import create_engine
    engine = create_engine(env["DATABASE_URL"], pool_pre_ping=True)
    registry = MediaRunRegistry(env.get("MEDIA_RUN_REGISTRY_PATH") or f"/var/lib/poc/media_runs/{config.source_id}.json")
    token = read_token(env)
    api = env.get("API_INTERNAL_BASE_URL", "http://api:8000")
    acks = HttpAckSender(api, token, config.consumer_id) if token else None
    fetcher = HttpConfigFetcher(api, token) if token else None
    import redis
    client = redis.from_url(env.get("REDIS_URL", "redis://redis:6379/0"), socket_timeout=5, socket_connect_timeout=5)
    stream = env.get("PHASE_CONTROL_STREAM", "phase-control")
    runs_stream = env.get("MEDIA_RUNS_STREAM", "media-runs")

    def run_sink(run: dict) -> None:
        client.xadd(runs_stream, {k: ("" if v is None else str(v)) for k, v in run.items()}, maxlen=1000, approximate=True)

    controller = MediaController(config, registry=registry, resolver=DbAssetResolver(engine, Path(root)),
                                 publisher=FfmpegPublisher(env.get("FFMPEG_BIN", "ffmpeg")),
                                 probe=MediaMtxProbe(env.get("MEDIAMTX_API_URL", "http://mediamtx:9997")),
                                 acks=acks, fetcher=fetcher, run_sink=run_sink if runs_stream else None)
    notifications = RedisNotifications(client, stream, f"{config.consumer_id}:{config.source_id}")
    return controller, notifications


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    controller, notifications = build_from_env()
    if controller.acks is None:
        log.warning("未配置 MEDIA_CONTROL_TOKEN，控制器照常推流但不回 ACK（ack_disabled）")
    controller.recover()
    stop = {"flag": False}

    def _signal(*_args):
        stop["flag"] = True

    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _signal)
    try:
        while not stop["flag"]:
            batch = notifications.read() if notifications else []
            if batch:
                controller.handle_messages([data for _id, data in batch])
                notifications.ack([message_id for message_id, _ in batch])
            controller.tick()
    finally:
        controller.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
