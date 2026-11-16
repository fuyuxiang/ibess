"""离线流水线：逐帧调用实时那一套模块，不另写算法（计划「离线质检需要复用同一批规则/模型接口」）。

帧 → 检测器 → 5.3 StageHealthMonitor（S10 健康观察、质量标记）→ 5.1 ZoneObserver（区域事实）→ 2.3 dispatch（S10 内置规则 + S03 AbsenceRules）。
实时模块来自 vision-worker（包 `worker`）与 event-service（包 `service`）的源码目录，由 ensure_rule_paths() 加进 sys.path；
本包叫 `offline` 就是为了不和 `worker` 撞名。每个任务新建一套有状态实例，不共用模块级默认实例。
代码版本清单 = 参与判定的模块源码 SHA256（换行统一成 LF 后算，免得 autocrlf 让同一份代码判成不同版本）+ 检测器身份 + 采样帧率。
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional, Protocol

PIPELINE_VERSION = "9.4-v1"
SERVICES_DIR = Path(__file__).resolve().parents[2]
COMPONENT_MODULES = {
    "stage_dispatcher": "service.stage_dispatcher",
    "stage_health": "worker.stage_health",
    "coverage_gap": "worker.coverage_gap",
    "zone_observer": "worker.zone_observer",
    "person_tracking": "worker.person_tracking",
    "zone_geometry": "worker.zone_geometry",
    "absence_rules": "worker.absence_rules",
}
OFFLINE_SCENES = ("S10", "S03")      # 离线首批接入的场景；其余启用场景在报告里是「未覆盖」


class PipelineError(RuntimeError):
    def __init__(self, code: str, message: str = "", *, retryable: bool = False, partial: Optional[dict] = None) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.retryable = retryable
        self.partial = partial or {}


class ModelUnavailable(PipelineError):
    def __init__(self, message: str = "") -> None:
        super().__init__("MODEL_UNAVAILABLE", message, retryable=True)


class Cancelled(Exception):
    pass


class ClaimLost(Exception):
    pass


class FrameSource(Protocol):
    def frames(self, sample_fps: float) -> Iterator[tuple[int, object]]:
        """按媒体时间升序给 (media_ms, BGR 帧)；读不了文件抛 PipelineError("VIDEO_UNREADABLE")。"""


class Detector(Protocol):
    def detect(self, frame) -> list[dict]: ...

    def identity(self) -> dict: ...


def ensure_rule_paths(env: Optional[dict] = None) -> None:
    env = os.environ if env is None else env
    for key, default in (("OFFLINE_VISION_WORKER_DIR", SERVICES_DIR / "vision-worker"), ("OFFLINE_EVENT_SERVICE_DIR", SERVICES_DIR / "event-service")):
        path = str(Path(env.get(key) or default).resolve())
        if path not in sys.path:
            sys.path.insert(0, path)


def _source_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _library_versions() -> dict:
    versions = {}
    for name in ("numpy", "opencv-python", "opencv-python-headless", "ultralytics", "torch"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return versions


def component_manifest(detector: Detector, sample_fps: float) -> dict:
    ensure_rule_paths()
    offline_dir = Path(__file__).resolve().parent
    components = {"pipeline": {"version": PIPELINE_VERSION, **{f"{p.stem}_sha256": _source_digest(p) for p in sorted(offline_dir.glob("*.py"))}},
                  "sample_fps": {"value": float(sample_fps)}, "detector": dict(detector.identity()), "libraries": _library_versions()}
    for name, dotted in COMPONENT_MODULES.items():
        module = importlib.import_module(dotted)
        components[name] = {"sha256": _source_digest(Path(module.__file__)), "model_version": getattr(module, "MODEL_VERSION", None)}
    return components


def manifest_diff(expected: dict, actual: dict) -> list[str]:
    return sorted(key for key in set(expected) | set(actual) if expected.get(key) != actual.get(key))


class OpenCvFrameSource:
    """按素材原 PTS 取帧（文件源按原素材时间，执行卡 9.2 同口径）；每 1000/fps 毫秒取一帧。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def frames(self, sample_fps: float) -> Iterator[tuple[int, object]]:
        import cv2  # noqa: WPS433  运行时才需要
        capture = cv2.VideoCapture(str(self.path))
        if not capture.isOpened():
            raise PipelineError("VIDEO_UNREADABLE", "视频文件打不开", retryable=False)
        step = 1000.0 / sample_fps
        next_ms = 0.0
        try:
            while True:
                ok = capture.grab()
                if not ok:
                    return
                pts = int(round(capture.get(cv2.CAP_PROP_POS_MSEC)))
                if pts + 0.5 < next_ms:
                    continue
                ok, frame = capture.retrieve()
                if not ok:
                    continue
                next_ms = max(next_ms + step, pts + step * 0.5)
                yield pts, frame
        finally:
            capture.release()


class YoloPersonDetector:
    """自己的 YOLO 实例，只取 person；加载或推理失败 → ModelUnavailable（任务 failed，不出任何 normal）。"""

    def __init__(self, weights: str, *, conf: float = 0.25, imgsz: int = 640, device: str = "cuda") -> None:
        path = Path(weights)
        if not path.is_file():
            raise ModelUnavailable("检测权重文件不存在")
        try:
            from ultralytics import YOLO  # noqa: WPS433
            self.model = YOLO(str(path))
        except Exception as exc:  # noqa: BLE001  任何加载错误都是模型不可用
            raise ModelUnavailable(f"检测模型加载失败：{type(exc).__name__}") from None
        self.conf, self.imgsz, self.device = conf, imgsz, device
        self._identity = {"name": "yolo-person", "weights_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "conf": conf, "imgsz": imgsz}

    def identity(self) -> dict:
        return dict(self._identity)

    def detect(self, frame) -> list[dict]:
        results = self.model.predict(frame, conf=self.conf, imgsz=self.imgsz, device=self.device, verbose=False)
        dets = []
        if results and len(results[0].boxes):
            names = results[0].names
            for box in results[0].boxes:
                if names[int(box.cls)] == "person":
                    dets.append({"class": "person", "conf": float(box.conf), "bbox": [float(v) for v in box.xyxy[0]]})
        return dets


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _segment_at(segments: list[dict], media_ms: int) -> Optional[dict]:
    for segment in segments:
        if segment["from_ms"] <= media_ms and (segment["to_ms"] is None or media_ms < segment["to_ms"]):
            return segment
    return None


class OfflinePipeline:
    def __init__(self, *, progress_every_ms: int = 5000, clock: Callable[[], float] = time.monotonic) -> None:
        self.progress_every_ms = progress_every_ms
        self.clock = clock

    def run(self, job: dict, source: FrameSource, detector: Detector, *, on_progress: Callable[[int], str] = lambda ms: "ok") -> dict:
        """on_progress(media_ms) 返回 ok / cancel / lost；cancel 抛 Cancelled，lost 抛 ClaimLost。"""
        ensure_rule_paths()
        from service.stage_dispatcher import DispatchError, HandlerFailed, dispatch          # noqa: WPS433  实时同一份派发
        from worker.absence_rules import AbsenceRules                                         # noqa: WPS433  实时同一份 S03 规则
        from worker.stage_health import FrameContext, StageHealthMonitor                       # noqa: WPS433
        from worker.zone_observer import ZoneObserver, normalize_detections                    # noqa: WPS433

        frozen = job["frozen"]
        started_at = _parse_utc(frozen["media_started_at"])
        segments = frozen.get("segments") or []
        duration = frozen.get("duration_ms")
        sample_fps = float(frozen.get("sample_fps") or 2.0)
        media_run_id = f"offline:{job['job_id']}:{job['attempts']}"
        health = StageHealthMonitor()
        observer = ZoneObserver(frozen["zone_config"]) if frozen.get("zone_config") else None
        handlers = {"S03": AbsenceRules()} if observer else {}
        attempted = ["S10"] + (["S03"] if observer else [])
        # 与 10.1 实时入口同形状的规则上下文：授权事实、工位身份等由建任务时冻结（6.3 交付前为空，规则按事实缺失出 insufficient）
        facts = dict(frozen.get("facts") or {})
        clues: dict[str, dict] = {}
        failures: list[dict] = []
        frames = observations = 0
        last_ms = 0
        next_progress = self.progress_every_ms
        began = self.clock()
        for media_ms, frame in source.frames(sample_fps):
            if duration is not None and media_ms >= duration:
                break                                  # 时间轴段是左闭右开，素材末尾那一刻不属于任何段
            segment = _segment_at(segments, media_ms)
            snapshot = (segment or {}).get("snapshot")
            ctx = FrameContext(source_id=job["source_id"], occurred_at=_iso(started_at + timedelta(milliseconds=media_ms)),
                               media_run_id=media_run_id, start_ms=int(media_ms), frame=frame, asset_id=job["asset_id"],
                               project_id=job["project_id"], room_id=job["room_id"], session_id=job["session_id"],
                               stage_snapshot_id=snapshot["stage_snapshot_id"] if snapshot else None,
                               phase_id=snapshot["phase_id"] if snapshot else None, phase_version=snapshot["phase_version"] if snapshot else None)
            try:
                raw = detector.detect(frame)
            except Exception as exc:  # noqa: BLE001  推理失败 = 模型不可用，任务 failed，不拿空检测当「没人」
                raise ModelUnavailable(f"检测推理失败：{type(exc).__name__}") from None
            height, width = frame.shape[:2]
            detections = normalize_detections(raw, width, height)
            produced = list(health.update(ctx, detections))
            if observer is not None:
                status = health.status()
                flags = (["camera_offline"] if status["stream"] != "online" else []) + (["camera_degraded"] if status["quality"] == "degraded" else [])
                produced += observer.update(ctx, detections, quality_flags=flags)
            for observation in produced:
                observations += 1
                bound_snapshot = snapshot if observation.get("binding_status") == "bound" else None
                try:
                    for clue in dispatch(observation, bound_snapshot, handlers, context=facts):
                        clues.setdefault(clue["clue_id"], clue)
                except (DispatchError, HandlerFailed) as exc:
                    failures.append({"observation_id": observation.get("observation_id"), "scene_id": getattr(exc, "scene_id", None),
                                     "code": getattr(exc, "code", type(exc).__name__), "message": str(exc)[:300]})
            frames += 1
            last_ms = int(media_ms)
            if last_ms >= next_progress:
                next_progress = last_ms + self.progress_every_ms
                signal = on_progress(last_ms)
                if signal == "cancel":
                    raise Cancelled()
                if signal == "lost":
                    raise ClaimLost()
        if frames == 0:
            raise PipelineError("NO_FRAMES", "一帧都没解出来", retryable=False)
        wall = max(self.clock() - began, 1e-9)
        stats = {"frames_processed": frames, "media_ms_processed": last_ms, "observations": observations, "clues": len(clues),
                 "failures": len(failures), "wall_seconds": round(wall, 3), "media_seconds_per_wall_second": round(last_ms / 1000.0 / wall, 3)}
        tolerance = 2 * 1000.0 / sample_fps + 1
        if duration is not None and last_ms < duration - tolerance:
            # 素材没读完（截断 / 损坏 / 解码提前退出）：后段「没有线索」不能当成「没有问题」
            raise PipelineError("INCOMPLETE_MEDIA", f"只处理到 {last_ms} ms，素材时长 {duration} ms", retryable=False, partial={"stats": stats})
        scene_failures: dict[str, int] = {}
        for failure in failures:
            key = failure.get("scene_id") or "dispatch"
            scene_failures[key] = scene_failures.get(key, 0) + 1
        # 处理失败过的场景不算「已覆盖」：失败的观察没被判，计数全 0 不代表干净
        covered = [scene for scene in attempted if scene not in scene_failures and "dispatch" not in scene_failures]
        return {
            "media_run_id": media_run_id, "attempted_scenes": attempted, "covered_scenes": covered, "scene_failures": scene_failures,
            "clues": list(clues.values()), "failures": failures, "stats": stats,
        }
