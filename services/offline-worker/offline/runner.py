"""离线质检 runner：认领 → 素材摘要复核 → 加载检测器 → 版本冻结 / 核对 → 流水线 → 写结果。单并发（计划「模型先单并发」）。

失败分类（error.retryable 决定 api 能不能一键重试）：
ASSET_NOT_FOUND（可重试，挂载好再跑）、ASSET_SHA256_MISMATCH（不可重试）、MODEL_UNAVAILABLE（可重试，模型就绪后再跑）、
VERSION_MISMATCH（不可重试：冻结版本与当前代码不同，不静默换版本）、VIDEO_UNREADABLE / NO_FRAMES（不可重试）、OFFLINE_WORKER_ERROR（可重试）。
数据库连接由 10.2 用环境项注入；本模块不写任何实时表。
"""
from __future__ import annotations

import hashlib
import logging
import os
import socket
import time
from pathlib import Path
from typing import Callable, Optional

from offline.jobs import SqlAlchemyJobRepository
from offline.pipeline import (Cancelled, ClaimLost, Detector, FrameSource, ModelUnavailable, OfflinePipeline, OpenCvFrameSource, PipelineError,
                              YoloPersonDetector, component_manifest, manifest_diff)

log = logging.getLogger("offline-worker")
HASH_CHUNK = 1024 * 1024


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def locate_asset(root: Path, relative: str) -> Optional[Path]:
    """只在素材根内找；4.1 登记时已做过路径规范检查，这里再挡一次越界。"""
    root = Path(root).resolve()
    candidate = root.joinpath(*relative.split("/")).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


class OfflineRunner:
    def __init__(self, repository: SqlAlchemyJobRepository, *, worker_id: str, assets_root: Path,
                 detector_factory: Callable[[], Detector], source_factory: Callable[[Path], FrameSource] = OpenCvFrameSource,
                 pipeline: Optional[OfflinePipeline] = None) -> None:
        if not worker_id:
            raise ValueError("worker_id 必填")
        self.repository = repository
        self.worker_id = worker_id
        self.assets_root = Path(assets_root)
        self.detector_factory = detector_factory
        self.source_factory = source_factory
        self.pipeline = pipeline or OfflinePipeline()

    def run_once(self) -> bool:
        job = self.repository.claim_next(self.worker_id)
        if job is None:
            return False
        job_id, claim_id = job["job_id"], job["claim_id"]
        frozen = job["frozen"]

        def fail(code: str, retryable: bool, message: str = "", result: Optional[dict] = None) -> bool:
            self.repository.fail(job_id, claim_id, code, retryable=retryable, message=message, result=result)
            return True

        try:
            return self._run_claimed(job, fail)
        except Exception:  # noqa: BLE001  准备阶段的未预期错误（IO、库加载、数据库抖动）：记可重试失败，worker 不退出
            log.exception("离线任务准备失败；job_id=%s", job_id)
            try:
                return fail("OFFLINE_WORKER_ERROR", True, "worker 内部错误，详见 worker 日志")
            except Exception:  # noqa: BLE001  连写失败都做不到：留给租约过期后重认领
                log.exception("离线任务失败状态写不进库；job_id=%s", job_id)
                return True

    def _run_claimed(self, job: dict, fail: Callable[..., bool]) -> bool:
        job_id, claim_id = job["job_id"], job["claim_id"]
        frozen = job["frozen"]
        asset = frozen.get("asset") or {}
        path = locate_asset(self.assets_root, asset.get("relative_path") or "")
        if path is None:
            return fail("ASSET_NOT_FOUND", True, "素材文件不在素材根内或未挂载")
        actual = file_sha256(path)
        asset_check = {"status": "match" if actual == asset.get("sha256") else "mismatch", "expected_sha256": asset.get("sha256"), "actual_sha256": actual}
        if asset_check["status"] != "match":
            return fail("ASSET_SHA256_MISMATCH", False, "素材文件摘要与登记时不同，不重跑", {"asset_check": asset_check})
        try:
            detector = self.detector_factory()
            components = component_manifest(detector, frozen.get("sample_fps") or 2.0)
        except ModelUnavailable as exc:
            return fail(exc.code, True, str(exc), {"asset_check": asset_check})
        signal = self.repository.heartbeat(job_id, claim_id, 0)      # 算摘要、加载模型可能很久：开跑前先续一次租
        if signal == "lost":
            return True
        if signal == "cancel":
            self.repository.cancel(job_id, claim_id)
            return True
        pinned = frozen.get("components")
        if pinned is None:
            self.repository.pin_components(job_id, claim_id, components)
        elif pinned != components:
            changed = manifest_diff(pinned, components)
            return fail("VERSION_MISMATCH", False, f"冻结版本与当前代码 / 模型不同：{', '.join(changed)}",
                        {"asset_check": asset_check, "version_diff": changed})
        try:
            result = self.pipeline.run(job, self.source_factory(path), detector,
                                       on_progress=lambda ms: self.repository.heartbeat(job_id, claim_id, ms))
        except Cancelled:
            self.repository.cancel(job_id, claim_id)
            return True
        except ClaimLost:
            log.warning("离线任务认领已失效，放弃写结果；job_id=%s", job_id)
            return True
        except PipelineError as exc:
            return fail(exc.code, exc.retryable, str(exc), {"asset_check": asset_check, **exc.partial})
        except Exception:  # noqa: BLE001  未预期错误：记可重试失败，不把异常正文写进库
            log.exception("离线任务处理失败；job_id=%s", job_id)
            return fail("OFFLINE_WORKER_ERROR", True, "worker 内部错误，详见 worker 日志")
        result.update(asset_check=asset_check, components=components)
        if not self.repository.complete(job_id, claim_id, result, result["stats"]["media_ms_processed"]):
            self.repository.cancel(job_id, claim_id)   # 跑完前刚被请求取消：取消优先，不写结果
        return True


def build_runner_from_environment(env: Optional[dict] = None) -> tuple[OfflineRunner, object]:
    from sqlalchemy import create_engine  # noqa: WPS433
    env = os.environ if env is None else env
    database_url, assets_root, weights = env.get("DATABASE_URL"), env.get("STAGE_ASSETS_ROOT"), env.get("OFFLINE_DETECTOR_WEIGHTS")
    if not database_url or not assets_root or not weights:
        raise RuntimeError("离线 worker 配置缺失；需要 DATABASE_URL、STAGE_ASSETS_ROOT、OFFLINE_DETECTOR_WEIGHTS")
    engine = create_engine(database_url, pool_pre_ping=True)
    repository = SqlAlchemyJobRepository(engine, lease_seconds=int(env.get("OFFLINE_LEASE_SECONDS", "120")))
    device = env.get("OFFLINE_DETECTOR_DEVICE", "cuda")
    runner = OfflineRunner(repository, worker_id=env.get("OFFLINE_WORKER_ID") or f"offline-{socket.gethostname()}",
                           assets_root=Path(assets_root), detector_factory=lambda: YoloPersonDetector(weights, device=device))
    return runner, engine


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    runner, _engine = build_runner_from_environment()
    backoff = 2
    try:
        while True:
            try:
                worked = runner.run_once()
                backoff = 2
            except Exception:  # noqa: BLE001  认领本身出错（如数据库暂时不可用）：记日志、退避，进程不退出
                log.exception("离线 worker 认领失败，%s 秒后重试", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if not worked:
                time.sleep(2)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
