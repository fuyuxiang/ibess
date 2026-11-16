"""事件前后视频（9.2 最小路径）：MediaMTX 录制片段按事件时刻定位 + 鉴权流式回放。

录制由 mediamtx 持续写 fMP4 片段（10秒/段），事件详情页按事件时刻 ±窗口取片段列表，
前端按序播放。摄像头映射：source_id camera-007 → 录制目录 cam7。
"""
import os
import re
import json
import subprocess
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID
from bisect import bisect_left, bisect_right

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_session

router = APIRouter()
RECORD_ROOT = Path(os.getenv("RECORDINGS_DIR", "/recordings"))
WINDOW_SECONDS = 45
SEGMENT_SECONDS = 10
CAMERA_SUFFIX = re.compile(r"^camera-0*(\d+)$")
SEGMENT_EPOCH = re.compile(r"-(\d{10,})\.mp4$")


def _mediamtx_path(camera_id: str) -> str | None:
    from app.core.supervision import channel
    return channel(camera_id)


@lru_cache(maxsize=4096)
def _duration(path: str, size: int, modified: float):
    try:
        data = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', path], capture_output=True, text=True, timeout=2, check=True)
        value = float(json.loads(data.stdout)['format']['duration'])
        if 0 < value < 3600:
            return value, False
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        pass
    return SEGMENT_SECONDS, True


def _segment_files(path: str) -> list[tuple[int, Path]]:
    """返回 [(起始epoch秒, 文件绝对路径)]，按时间升序。"""
    base = RECORD_ROOT / path
    if not base.is_dir():
        return []
    items = []
    for file in base.rglob("*.mp4"):
        match = SEGMENT_EPOCH.search(file.name)
        if match:
            items.append((int(match.group(1)), file))
    items.sort(key=lambda pair: pair[0])
    return items


def _live_event_recordings(event_id: UUID, session: Session, source_id: str | None = None):
    row = session.execute(text(
        "select camera_id, occurred_at from events where event_id = :eid"),
        {"eid": str(event_id)}).first()
    if row is None:
        raise HTTPException(404, "事件不存在")
    from app.models import Event
    from app.core.supervision_evidence import video_windows
    event = session.query(Event).filter_by(event_id=str(event_id)).one()
    windows = video_windows(session,event)
    selected = next((w for w in windows if w['source_id']==source_id),None) if source_id else next(iter(windows),None)
    if not selected:
        raise HTTPException(422,'该事件尚未关联此机位的证据时段')
    camera_id, occurred = selected['source_id'], row[1]
    if camera_id is None or occurred is None:
        raise HTTPException(422, "事件缺少摄像头或发生时间，无法定位录像")
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)
    path = _mediamtx_path(str(camera_id))
    if path is None:
        raise HTTPException(422, f"事件摄像头 {camera_id} 无对应录制通道")
    segments = _segment_files(path)
    if not segments:
        raise HTTPException(404, "该摄像头暂无录制片段（录制开启后产生）")
    start = selected['started_at'].replace(tzinfo=timezone.utc)
    end = selected['ended_at'].replace(tzinfo=timezone.utc)
    matched = []
    epochs = [item[0] for item in segments]
    # MediaMTX 按时间顺序分段；检查窗口内片段及紧邻前段即可覆盖长片段。
    left = max(0, bisect_left(epochs,start.timestamp())-1)
    right = bisect_right(epochs,end.timestamp())
    for epoch, file in segments[left:right]:
        if not file.resolve().is_relative_to(RECORD_ROOT.resolve()) or not file.is_file():
            continue
        try:
            stat = file.stat()
        except OSError:
            continue  # 与录制轮转并发删除时，按缺失片段处理。
        duration, estimated = _duration(str(file), stat.st_size, stat.st_mtime)
        if epoch + duration > start.timestamp() and epoch < end.timestamp():
            matched.append({'source_id':camera_id,'epoch':epoch, 'file':file, 'duration':duration, 'duration_estimated':estimated,
                'offset':epoch - occurred.timestamp(), 'contains_event':epoch <= occurred.timestamp() < epoch + duration})
    if not matched:
        raise HTTPException(404, '事件时间窗口内没有对应录像，不能使用其他时间的片段替代证据')
    return camera_id, occurred, matched


def _event_recordings(event_id: UUID, session: Session, source_id: str | None = None):
    from app.core.media_preservation import preserved_segments
    from app.core.supervision_evidence import video_windows
    from app.models import Event
    event = session.query(Event).filter_by(event_id=str(event_id)).first()
    if event is None:
        raise HTTPException(404,'事件不存在')
    windows = video_windows(session,event)
    selected = next((w for w in windows if w['source_id']==source_id),None) if source_id else next(iter(windows),None)
    if not selected:
        raise HTTPException(422,'该事件尚未关联此机位的证据时段')
    source_id = selected['source_id']
    saved = preserved_segments(session, str(event_id))
    start=selected['started_at'].replace(tzinfo=timezone.utc).timestamp()
    end=selected['ended_at'].replace(tzinfo=timezone.utc).timestamp()
    saved = [s for s in saved if s.get('source_id',event.camera_id)==source_id and s['epoch']<end and s['epoch']+s['duration']>start]
    if saved:
        row = session.execute(text('select camera_id,occurred_at from events where event_id=:id'),{'id':str(event_id)}).first()
        when = row[1].replace(tzinfo=timezone.utc) if row[1].tzinfo is None else row[1]
        return source_id, when, saved
    return _live_event_recordings(event_id,session,source_id)


def _all_event_recordings(event_id,session):
    from app.models import Event
    from app.core.supervision_evidence import video_windows
    event = session.query(Event).filter_by(event_id=str(event_id)).one()
    result = []
    failures = []
    for window in video_windows(session,event):
        try:
            result.extend(_event_recordings(event_id,session,window['source_id'])[2])
        except HTTPException as exc:
            failures.append({'source_id':window['source_id'],'reason':str(exc.detail)})
    return result,failures


@router.get("/events/{event_id}/recordings")
def event_recordings(event_id: UUID, source_id: str | None = None, session: Session = Depends(get_session)):
    """只返回与事件窗口重叠的录像，并明确标示事件时刻是否有覆盖。"""
    camera_id, occurred, matched = _event_recordings(event_id, session, source_id)
    items = []
    for index, segment in enumerate(matched):
        relative = (segment['file'].name if segment.get('preserved') else f"/recordings/{segment['file'].relative_to(RECORD_ROOT).as_posix()}")
        items.append({
            "index": index,
            "file_name": segment["file"].name,
            "relative_path": relative,
            "start_epoch": segment["epoch"],
            "offset_seconds": segment["offset"],
            "duration_seconds":segment['duration'], 'duration_estimated':segment['duration_estimated'],
            'contains_event':segment['contains_event'], 'seek_seconds':max(0, -segment['offset']) if segment['contains_event'] else 0,
            'preserved': bool(segment.get('preserved')),
            "playback_url": segment.get('playback_url') or f"/api/recordings/file?relative_path={relative}",
        })
    return {"camera_id": camera_id, "occurred_at": occurred.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "window_seconds": WINDOW_SECONDS, 'event_covered':any(s['contains_event'] and not s['duration_estimated'] for s in matched), "items": items, "total": len(items)}


@router.get("/recordings/file")
def recording_file(relative_path: str, session: Session = Depends(get_session)):
    """录像片段鉴权流式回放：仅允许 /recordings 下 .mp4，防目录穿越。"""
    if ".." in relative_path or not relative_path.startswith("/recordings/") or not relative_path.endswith(".mp4"):
        raise HTTPException(422, "非法片段路径")
    file = RECORD_ROOT / Path(relative_path).relative_to("/recordings")
    if not file.resolve().is_relative_to(RECORD_ROOT.resolve()):
        raise HTTPException(422, '非法片段路径')
    if not file.is_file():
        raise HTTPException(404, "录像片段不存在")
    return FileResponse(file, media_type="video/mp4",
                        headers={"Cache-Control": "private, max-age=300"})


@router.get('/events/{event_id}/preserved-recordings/{index}')
def preserved_recording(event_id: UUID, index: int, session: Session = Depends(get_session)):
    from app.core.media_preservation import preserved_segments, verify_preserved
    segments = preserved_segments(session,str(event_id))
    item = next((s for s in segments if s['manifest_index']==index),None)
    if not item:
        raise HTTPException(404,'保全录像不存在')
    verify_preserved(item)
    return FileResponse(item['file'],media_type='video/mp4',headers={'Cache-Control':'no-store'})
