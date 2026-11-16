"""人脸自动抓拍与比对：从摄像头抓帧 → 裁剪最大人脸 → 与登记库比对 → 写核验记录。

两种使用方式：
1) 后台 worker（loop）：按「规则配置」的周期与开关自动抓拍比对；
2) 手动抓拍（capture_and_match）：界面点「立即抓拍」，抓当前画面并返回比对明细。

抓拍策略：同一次连接内连抓 N 帧（默认 3 帧），逐帧提取人脸并与登记库比对，取最高分；
视频里人物姿态/侧脸变化大时，多帧择优能显著提高命中率（单帧常因侧脸而判为不匹配）。

说明：全景/远景画面人脸过小或无正脸时返回 face_found=False，不产生记录；
演示环境请选近景摄像头（如 cam10），或改用工作台上传抓拍图。
"""
import asyncio
import base64
import logging
import os
import re
import time
from datetime import datetime, timezone
from uuid import uuid4

import cv2
from sqlalchemy import select, text

from app import business_models  # noqa: F401  注册 business_persons 等表以解析外键
from app.core.face_match import FaceMatchError, get_face_engine
from app.db import SessionLocal
from app.face_models import FaceProfile, PersonVerificationResult

log = logging.getLogger('face-auto')

MEDIAMTX_BASE = os.environ.get('MEDIAMTX_RTSP_BASE', 'rtsp://mediamtx:8554')
CAMERA_URL = os.environ.get('FACE_AUTO_CAMERA', f'{MEDIAMTX_BASE}/cam10')

CAMERA_LABELS = {
    'cam1': '01 · 无异常基线',
    'cam2': '02 · 人员聚集（全景）',
    'cam3': '03 · 遮挡恢复',
    'cam4': '04 · 正常公开讨论',
    'cam5': '05 · 黑屏恢复',
    'cam6': '06 · 交头接耳',
    'cam7': '07 · 离席',
    'cam8': '08 · 资料递接',
    'cam9': '09 · 限制区域越界',
    'cam10': '10 · 通话近景（推荐）',
    'cam11': '11 · 拍摄资料',
    'cam12': '12 · 入口未寄存',
}


def camera_url(camera: str | None = None) -> str:
    """把 cam10 这类标识转成 RTSP 地址；也允许直接传完整 rtsp:// 地址。"""
    if not camera:
        return CAMERA_URL
    if camera.startswith('rtsp://'):
        return camera
    if not re.fullmatch(r'cam\d{1,2}', camera):
        raise ValueError('摄像头参数不合法')
    return f'{MEDIAMTX_BASE}/{camera}'


def _read_frames(camera: str | None = None, count: int = 3) -> list:
    """一次连接内连续读取多帧（RTSP 建连开销大，避免每帧重连）。"""
    capture = cv2.VideoCapture(camera_url(camera))
    frames = []
    try:
        if not capture.isOpened():
            return frames
        for index in range(max(1, count)):
            ok, frame = capture.read()
            if ok and frame is not None:
                frames.append(frame)
            if index + 1 < count:
                time.sleep(0.18)
    finally:
        capture.release()
    return frames


def _largest_face_crop(frame):
    """裁剪画面中面积最大的人脸（留 30% 边距），供单脸比对。"""
    try:
        engine = get_face_engine()
        height, width = frame.shape[:2]
        engine.detector.setInputSize((width, height))
        _, faces = engine.detector.detect(frame)
        if faces is None or len(faces) == 0:
            return None
        best = max(faces, key=lambda item: float(item[2]) * float(item[3]))
        x, y, fw, fh = (int(value) for value in best[:4])
        pad = int(0.3 * max(fw, fh))
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(width, x + fw + pad), min(height, y + fh + pad)
        if x1 - x0 < 40 or y1 - y0 < 40:
            return None
        return frame[y0:y1, x0:x1]
    except Exception:
        return None


def _encode(frame, max_width: int | None = None) -> str:
    """编码为 data URL；max_width 用于压缩预览体积。"""
    if max_width and frame.shape[1] > max_width:
        scale = max_width / frame.shape[1]
        frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    ok, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    if not ok:
        return ''
    return 'data:image/jpeg;base64,' + base64.b64encode(buffer.tobytes()).decode('ascii')


def _grab_frame(camera: str | None = None) -> bytes | None:
    """抓取并裁剪最大人脸（兼容旧调用）：在多帧里取第一张检测到的人脸。"""
    for frame in _read_frames(camera, count=3):
        big = cv2.resize(frame, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)
        crop = _largest_face_crop(big)
        if crop is not None:
            ok, buffer = cv2.imencode('.jpg', crop)
            if ok:
                return buffer.tobytes()
    return None


def _thresholds(engine):
    with SessionLocal() as session:
        cfg = session.execute(text(
            'select threshold_same, threshold_suspect from face_verify_config where id = 1')).mappings().first()
    same = float(cfg['threshold_same']) if cfg else float(engine.threshold)
    suspect = float(cfg['threshold_suspect']) if cfg else max(0.0, float(engine.threshold) - 0.08)
    return same, suspect


def capture_and_match(camera: str | None = None, record: bool = True,
                      source: str = '现场抓拍', attempts: int = 3) -> dict:
    """连抓多帧并比对全库，取最高分；命中（本人/疑似）写入核验记录。"""
    frames = _read_frames(camera, count=attempts)
    if not frames:
        return {'face_found': False, 'image': '', 'camera': camera or os.path.basename(CAMERA_URL),
                'message': '无法读取摄像头画面，请检查该路视频是否在线。'}

    engine = get_face_engine()
    with SessionLocal() as session:
        profiles = session.scalars(select(FaceProfile)).all()
        names: dict[str, str] = {}
        best = None
        face_seen = False
        for frame in frames:
            big = cv2.resize(frame, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)
            crop = _largest_face_crop(big)
            if crop is None:
                continue
            face_seen = True
            try:
                probe = engine.extract(cv2.imencode('.jpg', crop)[1].tobytes())
            except FaceMatchError:
                continue
            for row in profiles:
                if row.model != engine.model:
                    continue
                try:
                    similarity = engine.compare(list(row.embedding), probe.embedding)
                except FaceMatchError:
                    continue
                if best is None or similarity > best['similarity']:
                    if row.person_id not in names:
                        names[row.person_id] = session.execute(
                            text('select name from business_persons where person_id = :pid'),
                            {'pid': row.person_id}).scalar() or '未命名'
                    best = {'similarity': round(float(similarity), 6),
                            'person_id': row.person_id,
                            'person_name': names[row.person_id],
                            'reference_version': row.version,
                            'crop': crop}

        if not face_seen:
            return {'face_found': False, 'image': _encode(frames[0], max_width=720),
                    'camera': camera or os.path.basename(CAMERA_URL),
                    'message': '本帧未检测到人脸——请选近景/正脸摄像头（如 cam10），或让人物正对镜头。'}

        same, suspect = _thresholds(engine)
        if best is None:
            return {'face_found': True, 'image': _encode(frames[0], max_width=720),
                    'camera': camera or os.path.basename(CAMERA_URL),
                    'status': 'unmatched', 'similarity': None,
                    'threshold': same, 'threshold_suspect': suspect,
                    'candidates': [], 'recorded': False,
                    'message': '人脸库中尚无可用登记照，请先在「登记照片」中登记。'}

        similarity = best['similarity']
        if similarity >= same:
            status = 'matched'
        elif similarity >= suspect:
            status = 'suspect'
        else:
            status = 'unmatched'

        recorded = False
        if record and status in ('matched', 'suspect'):
            session.add(PersonVerificationResult(
                result_id=str(uuid4()), link_id=None, person_id=best['person_id'],
                status=status, matched=status == 'matched', similarity=similarity,
                threshold=same, checked_at=datetime.now(timezone.utc),
                person_name=best['person_name'], project_name=None,
                room_name=f'{camera or os.path.basename(CAMERA_URL)}（{source}）',
                model=engine.model, reference_version=best['reference_version'], error=None))
            session.commit()
            recorded = True

    label = {'matched': '本人', 'suspect': '疑似', 'unmatched': '未匹配'}[status]
    log.info('抓拍比对：%s 相似度=%.4f 结论=%s recorded=%s', best['person_name'], similarity, status, recorded)
    return {'face_found': True, 'image': _encode(best['crop']),
            'camera': camera or os.path.basename(CAMERA_URL),
            'status': status, 'conclusion': label, 'similarity': similarity,
            'threshold': same, 'threshold_suspect': suspect,
            'person_id': best['person_id'], 'person_name': best['person_name'],
            'candidates': [], 'recorded': recorded, 'frames': len(frames),
            'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'message': '抓拍比对完成' if status != 'unmatched' else '未匹配到已登记人员'}


def run_once(camera: str | None = None) -> int:
    from app.core.supervision_scope import resolve_scope
    from app.session_workflow_models import SessionWorkflow
    source_id = os.path.basename(camera or CAMERA_URL)
    with SessionLocal() as db:
        scope = resolve_scope(db, source_id, datetime.now(timezone.utc))
        if not scope:
            return 0
        workflow = db.get(SessionWorkflow, scope['session_id'])
        if workflow is not None and not workflow.identity_verification_enabled:
            return 0
    result = capture_and_match(camera, record=True, source='自动抓拍')
    return 1 if result.get('recorded') else 0


async def loop() -> None:
    log.info('人脸自动比对 worker 启动，摄像头=%s', CAMERA_URL)
    while True:
        interval = 30
        try:
            with SessionLocal() as session:
                cfg = session.execute(text(
                    'select auto_enabled, interval_seconds from face_verify_config where id = 1')).mappings().first()
            if cfg is not None:
                interval = max(10, int(cfg['interval_seconds']))
                if cfg['auto_enabled']:
                    await asyncio.to_thread(run_once)
                else:
                    interval = 10
        except Exception as exc:  # noqa: BLE001 - 循环任务需保持存活
            log.warning('自动比对周期异常：%s', type(exc).__name__)
            interval = 15
        await asyncio.sleep(interval)
