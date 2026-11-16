"""media-service：FFmpeg 循环缓存 + 事件前后视频片段截取取证。

- 持续用 FFmpeg segment 录制循环缓存（2 秒/段，保留最近 30 秒）
- 监听 Redis events 流，收到事件后从缓存截取事件前后各 10 秒视频
- 上传 MinIO 并写入 Evidence 记录（type=video）
"""
import asyncio
import glob
import hashlib
import logging
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone

import redis.asyncio as redis
from minio import Minio
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from service.models import Base, Evidence
from service.event_scope import configured_source, should_capture

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("media-service")

RTSP_URL = os.getenv("RTSP_URL", "")
MEDIA_SOURCE_ID = configured_source(RTSP_URL, os.getenv('MEDIA_SOURCE_ID'))
CACHE_DIR = os.getenv("CACHE_DIR", "/cache")
SEGMENT_SECONDS = int(os.getenv("SEGMENT_SECONDS", "2"))
SEGMENT_WRAP = int(os.getenv("SEGMENT_WRAP", "15"))          # 2s x 15 = 30 秒缓存
PRE_SECONDS = float(os.getenv("PRE_SECONDS", "2"))
POST_SECONDS = float(os.getenv("POST_SECONDS", "2"))
RECORDER_LOG = os.getenv("RECORDER_LOG", "/tmp/recorder.log")
RECORDER_RETRY_SECONDS = int(os.getenv("RECORDER_RETRY_SECONDS", "5"))
CACHE_MAX_AGE = int(os.getenv("CACHE_MAX_AGE", "120"))

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
EVENTS_STREAM = os.getenv("EVENTS_STREAM", "events")

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS = os.getenv("MINIO_ROOT_USER", "demo")
MINIO_SECRET = os.getenv("MINIO_ROOT_PASSWORD", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "evidence")

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg://demo@localhost:5432/demo"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)

RECORDER = None
RECORDER_LOGF = None


def _tail_recorder_log(n: int = 3) -> list[str]:
    """读取 ffmpeg 尾部日志，便于定位录制器退出原因。"""
    try:
        if not os.path.exists(RECORDER_LOG):
            return []
        with open(RECORDER_LOG, "r", errors="replace") as f:
            lines = [ln.strip() for ln in f.readlines()[-n:]]
        return [ln for ln in lines if ln]
    except OSError:
        return []


def start_recorder():
    """启动 FFmpeg 循环缓存录制（segment 循环覆盖）。"""
    global RECORDER_LOGF
    os.makedirs(CACHE_DIR, exist_ok=True)
    if RECORDER_LOGF is not None:
        try:
            RECORDER_LOGF.close()
        except OSError:
            pass
    if os.path.exists(RECORDER_LOG) and os.path.getsize(RECORDER_LOG) > 2 * 1024 * 1024:
        try:
            os.remove(RECORDER_LOG)
        except OSError:
            pass
    cmd = [
        "ffmpeg", "-rtsp_transport", "tcp", "-i", RTSP_URL,
        "-c", "copy", "-f", "segment",
        "-segment_time", str(SEGMENT_SECONDS),
        "-segment_wrap", str(SEGMENT_WRAP),
        "-strftime", "1",
        "-segment_format", "mp4",
        f"{CACHE_DIR}/seg_%Y%m%d%H%M%S.mp4",
    ]
    RECORDER_LOGF = open(RECORDER_LOG, "ab", buffering=0)
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=RECORDER_LOGF)


def cleanup_cache(max_age: int = CACHE_MAX_AGE):
    """回收过期分段。

    -strftime 1 与 -segment_wrap 同时使用时 segment_wrap 不生效，
    分段会无限累积；/cache 是 tmpfs，必须主动回收，否则吃满内存。
    """
    now = time.time()
    for path in glob.glob(f"{CACHE_DIR}/seg_*.mp4"):
        ts = _seg_time(os.path.basename(path))
        if ts is not None and now - ts > max_age:
            try:
                os.remove(path)
            except OSError:
                pass


async def recorder_watchdog():
    """守护 ffmpeg 录制子进程：断流或启动顺序导致的退出会自动拉起。"""
    global RECORDER
    while True:
        try:
            if RECORDER is None or RECORDER.poll() is not None:
                code = None if RECORDER is None else RECORDER.returncode
                tail = _tail_recorder_log()
                log.warning(
                    "循环缓存录制器未运行(code=%s)，重启中；ffmpeg尾部: %s",
                    code, " | ".join(tail) or "-",
                )
                try:
                    RECORDER = start_recorder()
                    log.info("循环缓存录制已拉起: %s → %s", RTSP_URL, CACHE_DIR)
                except Exception as e:  # 二进制缺失等
                    log.error("录制器拉起失败: %s", e)
            cleanup_cache()
        except Exception as e:
            log.warning("录制器守护异常: %s", e)
        await asyncio.sleep(RECORDER_RETRY_SECONDS)


def _seg_time(name: str):
    """解析段文件名 seg_YYYYmmddHHMMSS.mp4 为时间戳。"""
    try:
        return datetime.strptime(name, f"seg_%Y%m%d%H%M%S.mp4").timestamp()
    except ValueError:
        return None


def extract_clip(event_time: float, out_path: str) -> bool:
    """取事件前后 PRE/POST 秒：先整段拼接，再按时间轴裁到精确窗口。

    分段按关键帧切、文件名只精确到秒，单段实际长短不一，直接拼整段会多出
    半段到一段（实测 2.5+2.5 的窗口拼出 4 秒）。这里多取两段做余量，
    最后用 ffmpeg 从拼接结果里裁出恰好 PRE+POST 秒。
    """
    segs = sorted(glob.glob(f"{CACHE_DIR}/seg_*.mp4"))
    if not segs:
        log.warning("循环缓存为空")
        return False

    t_start = event_time - PRE_SECONDS
    t_end = event_time + POST_SECONDS
    pad = max(SEGMENT_SECONDS * 2, 4.0)
    picked = [(t, s) for s in segs if (t := _seg_time(os.path.basename(s))) is not None
              and t_start - pad <= t <= t_end + pad]
    if not picked:
        log.warning("事件时刻附近无缓存分段（事件时间 %.1f）", event_time)
        return False

    first_t = picked[0][0]
    list_file = f"{CACHE_DIR}/concat_{uuid.uuid4().hex}.txt"
    joined = f"{CACHE_DIR}/joined_{uuid.uuid4().hex}.mp4"
    with open(list_file, "w") as f:
        for _, s in picked:
            f.write(f"file '{s}'\n")

    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
             "-c", "copy", joined],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if r.returncode != 0 or not os.path.exists(joined):
            return False
        # 拼接结果的时间轴从第一段开头起算，窗口起点减它就是事件在片中的偏移
        offset = max(0.0, t_start - first_t)
        want = t_end - t_start
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{offset:.3f}", "-i", joined,
             "-t", f"{want:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-c:a", "aac", "-movflags", "+faststart", out_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        return r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0
    finally:
        for p in (list_file, joined):
            try:
                os.remove(p)
            except OSError:
                pass


def upload_evidence(event_id: str, clip_path: str) -> str | None:
    """上传片段到 MinIO 并返回 object_key（含 sha256）。"""
    try:
        m = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS, secret_key=MINIO_SECRET, secure=False)
        if not m.bucket_exists(MINIO_BUCKET):
            m.make_bucket(MINIO_BUCKET)
        with open(clip_path, "rb") as f:
            data = f.read()
        object_key = f"videos/{event_id}/{int(time.time() * 1000)}.mp4"
        m.put_object(MINIO_BUCKET, object_key, __import__("io").BytesIO(data), len(data))
        return object_key
    except Exception as e:
        log.warning("上传失败: %s", e)
        return None


def write_evidence(event_id: str, object_key: str):
    session = Session()
    try:
        session.add(Evidence(event_id=event_id, evidence_type="video", object_key=object_key))
        session.commit()
        log.info("证据记录: %s → %s", event_id[:8], object_key)
    except Exception as e:
        session.rollback()
        log.warning("Evidence 写入失败: %s", e)
    finally:
        session.close()


def _decode(data: dict) -> dict:
    return {
        (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
        for k, v in data.items()
    }


async def handle_event(data: dict):
    if not should_capture(data, MEDIA_SOURCE_ID):
        return
    event_id = data.get("event_id")
    camera_id = data.get("camera_id", "")
    occurred_at = data.get("occurred_at")
    risk_level = data.get("risk_level", "")
    # 只对摄像头视频相关的非 info 风险事件取证（person_detected 等基础检测不存视频）
    # should_capture 已按规范化来源校验，cam1 与 camera-001 使用同一缓存。
    if risk_level == "info":
        return
    if not occurred_at:
        return
    with Session() as session:
        if session.query(Evidence).filter(Evidence.event_id == event_id, Evidence.evidence_type == 'video').first():
            return
    try:
        occurred = datetime.fromisoformat(occurred_at)
        event_time = (occurred if occurred.tzinfo else occurred.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return

    clip_path = f"/tmp/{event_id}.mp4"
    if POST_SECONDS > 0:
        # 事件之后的画面要等它整段写完才能裁到准确时长：分段按关键帧切，
        # 正在写的那段取不到，只等 POST_SECONDS 会短一截，故多等一个分段。
        await asyncio.sleep(POST_SECONDS + SEGMENT_SECONDS)
    log.info("事件 %s 触发截取（时刻 %.0f）", event_id[:8], event_time)
    if extract_clip(event_time, clip_path):
        object_key = upload_evidence(event_id, clip_path)
        if object_key:
            write_evidence(event_id, object_key)
        try:
            os.remove(clip_path)
        except OSError:
            pass


async def main():
    from ibess_db.schema import require_schema
    require_schema(engine, ("evidences",))
    global RECORDER
    if not RTSP_URL:
        log.error("未配置 RTSP_URL")
        return

    try:
        RECORDER = start_recorder()
        log.info("循环缓存录制启动: %s → %s (保留 %d 秒)", RTSP_URL, CACHE_DIR, SEGMENT_SECONDS * SEGMENT_WRAP)
    except Exception as e:
        log.error("录制器启动失败，交由守护任务重试: %s", e)
    asyncio.create_task(recorder_watchdog())

    r = redis.from_url(REDIS_URL)
    last_id = "$"
    log.info("监听事件流: %s", EVENTS_STREAM)
    while True:
        try:
            entries = await r.xread({EVENTS_STREAM: last_id}, block=1000, count=50)
            for _, items in entries:
                for msg_id, data in items:
                    last_id = msg_id
                    await handle_event(_decode(data))
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("事件消费异常: %s", e)
            await asyncio.sleep(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        if RECORDER:
            RECORDER.terminate()
