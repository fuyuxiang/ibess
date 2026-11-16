"""现场拾音：从 RTSP 流按固定时长切片，登记进录音队列，复用同一套 ASR/说话人分离流程。

只做「切音频 + 入队」，转写与事件仍由 recordings_worker 的主循环完成，
因此现场流和上传录音走的是同一条链路、同一份词库、同一套事件格式。
"""
import hashlib
import logging
import os
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import soundfile as sf
from sqlalchemy import create_engine, func, insert, select, text

from worker.recordings_worker import safe_path, tables

log = logging.getLogger('audio-stream')
SAMPLE_RATE = 16000
# 低于这个 RMS 视为无人说话的环境噪声，不入队，避免把静音片段跑成“转写失败”
SILENCE_RMS = float(os.environ.get('AUDIO_STREAM_SILENCE_RMS', '0.004'))


def _capture(url: str, seconds: int, target: Path) -> bool:
    command = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-rtsp_transport', 'tcp',
        '-i', url, '-vn', '-ac', '1', '-ar', str(SAMPLE_RATE), '-t', str(seconds),
        '-y', str(target),
    ]
    try:
        subprocess.run(command, check=True, timeout=seconds + 20,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return target.is_file() and target.stat().st_size > 44
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b'').decode('utf-8', 'ignore').strip().splitlines()
        log.warning('拾音失败（%s）：%s', url, detail[-1][:160] if detail else 'ffmpeg 退出')
    except subprocess.TimeoutExpired:
        log.warning('拾音超时（%ss）', seconds)
    return False


_SCOPE: dict = {"at": 0.0, "project": None, "room": None}


def _current_scope(engine) -> tuple:
    """当前运行场次绑定的项目/评标室；缓存 5 分钟。

    现场拾音如果不挂业务绑定，语义复核会以"不知道属于哪个评标室"为由拒绝执行，
    于是所有敏感词事件都绕过人工/模型复核直接进队列。这里跟着运行场次自动绑定。
    """
    now = time.time()
    if _SCOPE["project"] and now - _SCOPE["at"] < 300:
        return _SCOPE["project"], _SCOPE["room"]
    try:
        with engine.begin() as connection:
            row = connection.execute(text(
                "select b.project_id, b.room_id from phase_bindings b "
                "join eval_sessions s on s.session_id = b.session_id "
                "where s.status = 'running' limit 1")).first()
        if row:
            _SCOPE.update(at=now, project=row[0], room=row[1])
    except Exception:
        pass
    return _SCOPE["project"], _SCOPE["room"]


def _record_capture_time(connection, recording_id: str, started_at) -> None:
    """登记录音的采集起点；表可能不存在（旧库），失败不影响入队。"""
    try:
        from sqlalchemy import text
        connection.execute(text(
            "insert into recording_capture_times(recording_id, started_at, actor, updated_at) "
            "values (:rid, :st, 'audio-worker', (now() at time zone 'utc')) "
            "on conflict (recording_id) do nothing"),
            {'rid': recording_id, 'st': started_at})
    except Exception:
        pass


def _enqueue(engine, recordings, directory: Path, chunk: Path, camera_id: str) -> bool:
    try:
        samples, rate = sf.read(str(chunk), dtype='float32', always_2d=True)
        if rate != SAMPLE_RATE or len(samples) == 0:
            return False
        mono = samples.mean(axis=1)
        if float(np.sqrt(np.mean(mono ** 2))) < SILENCE_RMS:
            return False
        with wave.open(str(chunk), 'rb') as handle:
            duration = round(handle.getnframes() * 1000 / handle.getframerate())
        data = chunk.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        identifier = str(uuid.uuid4())
        private = safe_path(directory, identifier + '.original.wav')
        private.write_bytes(data)
        # 标题按北京时间命名（容器时区可能是 UTC，直接用 now() 会差 8 小时）
        captured = datetime.now(timezone.utc)
        stamp = (captured + timedelta(hours=8)).strftime('%H:%M:%S')
        with engine.begin() as connection:
            connection.execute(insert(recordings).values(
                recording_id=identifier, title=f'{camera_id} 现场拾音 {stamp}'[:128],
                source_id=camera_id, source_type='file',
                source_key=f'{camera_id}:{time.time_ns()}',
                status='queued', duration_ms=duration, created_at=captured,
                segment_count=0, sha256=digest, original_path=private.name))
            project, room = _current_scope(engine)
            if project and room:
                connection.execute(text(
                    "insert into media_bindings(binding_id, recording_id, project_id, room_id, actor, created_at) "
                    "values (gen_random_uuid()::text, :rid, :pid, :room, 'audio-worker', now()) "
                    "on conflict (recording_id) do nothing"),
                    {'rid': identifier, 'pid': project, 'room': room})
            # 登记采集起点，前端才能显示"采集起点"而不是"未登记"
            _record_capture_time(connection, identifier, captured - timedelta(milliseconds=duration))
        return True
    except Exception as exc:
        log.warning('拾音入队失败：%s', type(exc).__name__)
        return False


def _loop(url: str, camera_id: str, directory: Path, seconds: int) -> None:
    engine = create_engine(os.environ['DATABASE_URL'], pool_pre_ping=True)
    recordings, _ = tables(engine)
    failures = 0
    while True:
        try:
            with engine.connect() as connection:
                pending = connection.scalar(select(func.count()).select_from(recordings)
                                            .where(recordings.c.status.in_(('queued', 'processing'))))
            # 排队超过两条就先停手，等 ASR 追上来，避免音频队列无限堆积
            if pending is not None and pending >= 2:
                time.sleep(3)
                continue
            chunk = Path(tempfile.gettempdir()) / f'live-{uuid.uuid4().hex}.wav'
            ok = _capture(url, seconds, chunk)
            if ok:
                _enqueue(engine, recordings, directory, chunk, camera_id)
                failures = 0
            else:
                failures += 1
                time.sleep(min(30, 3 * failures))
            chunk.unlink(missing_ok=True)
        except Exception as exc:
            log.error('拾音循环异常：%s', type(exc).__name__)
            time.sleep(5)


def maybe_start(directory: Path) -> bool:
    url = (os.environ.get('AUDIO_STREAM_URL') or '').strip()
    if not url:
        return False
    camera_id = (os.environ.get('AUDIO_STREAM_CAMERA') or 'camera-007').strip()
    seconds = max(8, int(os.environ.get('AUDIO_STREAM_CHUNK_SEC', '20')))
    threading.Thread(target=_loop, args=(url, camera_id, directory, seconds),
                     name='live-audio-capture', daemon=True).start()
    log.info('现场拾音已启动：%s → %s，每 %s 秒一片', url, camera_id, seconds)
    return True
