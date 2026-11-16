"""vision-worker：RTSP 拉流 → 抽帧 → YOLO 人员检测 + 设备异常诊断 → Redis Streams。

最小垂直切片实现：
- 从 RTSP_URL 拉流（cv2.VideoCapture，支持断线重连）
- 按 VIDEO_FPS 频率抽帧
- YOLO 检测 person → observation 写入 Redis
- V08 视频断流：3 秒无有效帧 → video_offline 观察
- V07 遮挡/黑屏：灰度方差低于阈值持续 1 秒 → camera_blacked 观察
"""
import io
import json
import logging
import os
import queue
import threading
import time
import uuid
import urllib.error
import urllib.request

# cv2 拉 RTSP 默认走 UDP。容器网络下 RTP 丢包会让解码卡住（实测单次读帧阻塞 3 秒以上），
# 触发「3 秒无帧」的误判断流 → 重连 → 检测频率掉到 0.1~0.3fps，前端框自然跟不上。
# 强制 TCP 后读帧间隔稳定在 1 秒内。必须在 import cv2 之前设置。
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
# torch 默认按 CPU 核心数开线程。多路 worker 并发时线程数远超实际可用核数，
# 调度开销把推理拖垮：实测同一帧 2 线程 101ms、8 线程 8924ms（88 倍退化）。
# 固定小线程数，让多进程之间并行而不是进程内互抢。
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import cv2
import numpy as np
import redis
from minio import Minio

from sqlalchemy import create_engine, text


from worker.detector import PersonDetector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vision-worker")

STREAM = os.getenv("REDIS_STREAM", "observations")
STREAM_MAXLEN = int(os.getenv("STREAM_MAXLEN", "2000"))
KEYFRAME_INTERVAL = float(os.getenv("KEYFRAME_INTERVAL", "5"))

ABSENCE_EVENT_SECONDS = float(os.getenv("ABSENCE_EVENT_SECONDS", "10"))
# 手机专项扫描：整帧里手机只有十几像素，YOLO 必漏；裁到人身上放大再检，召回才够用。
# 只对入口/通话/桌拍这类机位开，避免每路都多付一份算力。
PHONE_SCAN = os.getenv("PHONE_SCAN", "0") == "1"
PHONE_SCAN_INTERVAL = float(os.getenv("PHONE_SCAN_INTERVAL", "2.0"))
PHONE_SCAN_IMGSZ = int(os.getenv("PHONE_SCAN_IMGSZ", "640"))
PHONE_SCAN_CONF = float(os.getenv("PHONE_SCAN_CONF", "0.12"))
PHONE_SCAN_MAX_PERSONS = int(os.getenv("PHONE_SCAN_MAX_PERSONS", "4"))
# 稳定化：同一位置连续命中才算数，避免单帧抖动决定事件
PHONE_VOTE_WINDOW = float(os.getenv("PHONE_VOTE_WINDOW", "30"))
PHONE_VOTE_MIN_HITS = int(os.getenv("PHONE_VOTE_MIN_HITS", "2"))
# 大模型复核：只对"模糊候选"调用，用来否掉水杯/证件这类误检
VLM_REVIEW = os.getenv("VLM_REVIEW", "0") == "1"
VLM_BASE_URL = os.getenv("VLM_BASE_URL", "")
VLM_API_KEY = os.getenv("VLM_API_KEY", "")
VLM_MODEL = os.getenv("VLM_MODEL", "MiniMax-M3.1-Flash")
VLM_TIMEOUT = float(os.getenv("VLM_TIMEOUT", "20"))
VLM_CONF_MIN = float(os.getenv("VLM_CONF_MIN", "0.10"))
VLM_CONF_MAX = float(os.getenv("VLM_CONF_MAX", "0.40"))
# 收紧的提示词：要求先给结论再补姿态，实测比开放式提问稳定（开放式会犹豫成"无"）
VLM_QUESTION = ("图中是否清楚可见这个人拿着手机？只回答「有」或「无」，再从以下动作选一项："
                "贴耳通话、对准文件拍摄、普通手持。仅明确举起手机朝向文件或屏幕拍摄时选对准文件拍摄；"
                "手机靠近桌面、低头看屏幕、手机在胸前不能判断为拍摄或通话。看不清回答无。")

_zone_engine = create_engine(os.getenv("DATABASE_URL", "postgresql+psycopg://demo@postgres:5432/demo"), pool_pre_ping=True)


def point_in_polygon(x: float, y: float, poly: list) -> bool:
    """射线法；坐标 0~1 归一化。"""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i][0], poly[i][1]
        xj, yj = poly[j][0], poly[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-9) + xi):
            inside = not inside
        j = i
    return inside


def zone_occupied(boxes: list, poly: list, width: int, height: int) -> bool:
    for x1, y1, x2, y2 in boxes:
        if point_in_polygon((x1 + x2) / 2 / width, (y1 + y2) / 2 / height, poly):
            return True
    return False


_PHONE_TRACKS: dict[tuple[str, str], list[float]] = {}

# 大模型复核异步化：主循环只入队，复核结果由后台线程回填，
# 否则一次 3~4 秒的调用会把采样率从 1 秒拖到 5 秒，反而更漏。
_VLM_QUEUE: "queue.Queue[dict]" = queue.Queue(maxsize=8)
_VLM_READY: list[dict] = []
_VLM_LOCK = threading.Lock()
_VLM_INFLIGHT: dict[str, float] = {}
_VLM_STARTED = False


def _phone_vote(source_id: str, person_key: str, now: float) -> bool:
    return __phone_vote(_context, source_id, person_key, now)


def _vlm_judge(crop) -> tuple[bool, str]:
    return __vlm_judge(_context, crop)


POSTURE_LABELS = {"ear": "贴耳", "chest": "胸前", "desk": "对桌面", "hand": "手持", "": "未知"}


def _posture_of(text: str) -> str:
    return __posture_of(_context, text)


def _vlm_loop():
    return __vlm_loop(_context)


def _ensure_vlm_thread():
    return __ensure_vlm_thread(_context)


def _drain_ready(source_id: str, now: float) -> list[dict]:
    return __drain_ready(_context, source_id, now)


def scan_person_phones(detector, frame, persons, source_id: str = ""):
    return _scan_person_phones(_context, detector, frame, persons, source_id)


def merge_phones(base, extra, min_gap=12.0):
    return _merge_phones(_context, base, extra, min_gap)


def load_zones(camera_id: str) -> list[dict]:
    return _load_zones(_context, camera_id)


OFFLINE_SECONDS = float(os.getenv("OFFLINE_SECONDS", "20"))  # 手机扫描一次可能几秒，8 秒会把“忙”误判成“断流”  # 3s 太敏感，read 抖动会误判重连
BLACK_SECONDS = float(os.getenv("BLACK_SECONDS", "1"))
BLACK_VAR_THRESHOLD = float(os.getenv("BLACK_VAR_THRESHOLD", "10"))
# 每轮检测前丢弃的积压帧数：源 15fps / 检测 5fps 时取 2 可对齐实时，
# 避免 RTSP 读缓冲持续堆积被服务端判定 "reader is too slow" 后整帧丢弃。
FRAME_DROP = max(0, int(os.getenv("FRAME_DROP", "0")))  # grab 可能阻塞等帧，默认关闭


def publish(r, obs_type, source_id, session_id, detail, occurred_at=None):
    return _publish(_context, r, obs_type, source_id, session_id, detail, occurred_at)


def init_minio():
    return _init_minio(_context)


def save_keyframe(minio_client, bucket, frame, source_id):
    return _save_keyframe(_context, minio_client, bucket, frame, source_id)


def main():
    return _main(_context)


from ibess_runtime.context import Namespace
from worker.runtime import main as _main
from worker.infrastructure.evidence import init_minio as _init_minio
from worker.infrastructure.evidence import save_keyframe as _save_keyframe
from worker.infrastructure.observations import publish as _publish
from worker.infrastructure.observations import load_zones as _load_zones


from worker.application.phone_candidates import _phone_vote as __phone_vote
from worker.application.phone_candidates import _vlm_judge as __vlm_judge
from worker.application.phone_candidates import _posture_of as __posture_of
from worker.application.phone_candidates import _vlm_loop as __vlm_loop
from worker.application.phone_candidates import _ensure_vlm_thread as __ensure_vlm_thread
from worker.application.phone_candidates import _drain_ready as __drain_ready
from worker.application.phone_candidates import scan_person_phones as _scan_person_phones
from worker.application.phone_candidates import merge_phones as _merge_phones

_context = Namespace(globals())

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("退出")
