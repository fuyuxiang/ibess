import hashlib
import io
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4
from pydantic import BaseModel
import wave

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audio_models import AudioRecording, AudioSegment
from app.db import get_session
from app.routers.participant_verification import PrivateResponseRoute
from app.routers.faces import iso

router = APIRouter(prefix='/audio', route_class=PrivateResponseRoute)
MAX_BYTES = 20 * 1024 * 1024


def validate_wav(data):
    try:
        with wave.open(io.BytesIO(data), 'rb') as w:
            frames, rate, width, channels = w.getnframes(), w.getframerate(), w.getsampwidth(), w.getnchannels()
            if w.getcomptype() != 'NONE' or channels not in range(1, 9) or width not in (1, 2, 3, 4) or not 8000 <= rate <= 192000:
                raise ValueError()
            if frames <= 0 or frames / rate > 600:
                raise ValueError()
            if len(w.readframes(frames)) != frames * width * channels:
                raise ValueError()
            return round(frames * 1000 / rate)
    except (wave.Error, EOFError, ValueError):
        raise HTTPException(422, '请上传完整的 PCM WAV 音频，时长大于0且不超过10分钟') from None


def data_path(name):
    root = Path(os.environ.get('AUDIO_DATA_DIR', '/audio-data')).resolve()
    target = (root / name).resolve()
    if target.parent != root or target.name != name:
        raise HTTPException(404, '音频文件无效')
    return target


def get_record(session, recording_id, lock=False):
    statement = select(AudioRecording).where(AudioRecording.recording_id == str(recording_id))
    if lock:
        statement = statement.with_for_update()
    row = session.scalar(statement)
    if row is None:
        raise HTTPException(404, '录音不存在')
    return row


def recording_json(row):
    return {key: getattr(row, key) for key in ('recording_id','title','source_id','source_type','status','duration_ms',
        'model','speaker_model','speaker_count','segment_count','sha256','error')} | {
        'created_at': iso(row.created_at), 'processed_at': iso(row.processed_at) if row.processed_at else None,
        'playback_url': f'/api/audio/recordings/{row.recording_id}/playback' if row.status == 'ready' and row.pcm_path else None}


@router.get('/recordings')
def list_recordings(limit: int = Query(30, ge=1, le=100), session: Session = Depends(get_session)):
    rows = session.scalars(select(AudioRecording).order_by(AudioRecording.created_at.desc()).limit(limit)).all()
    return {'items': [recording_json(row) for row in rows]}


@router.post('/recordings', status_code=202)
def upload_recording(file: UploadFile, session: Session = Depends(get_session)):
    try:
        if not (file.filename or '').lower().endswith('.wav'):
            raise HTTPException(422, '当前支持 PCM WAV 文件，请先转换为 WAV')
        data = file.file.read(MAX_BYTES + 1)
    finally:
        file.file.close()
    if len(data) > MAX_BYTES:
        raise HTTPException(413, '音频不能超过20 MiB')
    duration = validate_wav(data)
    recording_id = str(uuid4())
    path = data_path(recording_id + '.original.wav')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    row = AudioRecording(recording_id=recording_id, title=Path(file.filename.replace('\\', '/')).name[:128],
        source_id='upload', source_type='upload', status='queued', duration_ms=duration,
        created_at=datetime.now(timezone.utc), segment_count=0, sha256=hashlib.sha256(data).hexdigest(), original_path=path.name)
    session.add(row)
    result = recording_json(row)
    try:
        session.commit()
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return result


@router.get('/recordings/{recording_id}')
def detail(recording_id: UUID, session: Session = Depends(get_session)):
    row = get_record(session, recording_id)
    segments = session.scalars(select(AudioSegment).where(AudioSegment.recording_id == str(recording_id)).order_by(AudioSegment.ordinal)).all()
    from app.supervision_models import RecordingCaptureTime
    capture = session.get(RecordingCaptureTime,str(recording_id))
    return recording_json(row) | {'capture_started_at':iso(capture.started_at) if capture else None, 'segments': [{key: getattr(segment, key) for key in ('segment_id','speaker','start_ms','end_ms','text')} | {
        'playback_url': f'/api/audio/recordings/{recording_id}/segments/{segment.segment_id}/playback'} for segment in segments]}


class CaptureTimeIn(BaseModel):
    started_at: datetime | None


@router.put('/recordings/{recording_id}/capture-time')
def set_capture_time(recording_id:UUID, body:CaptureTimeIn, request:Request, session:Session=Depends(get_session)):
    from app.supervision_models import RecordingCaptureTime
    get_record(session,recording_id,lock=True)
    value = body.started_at
    if value and (value.tzinfo is None or value > datetime.now(timezone.utc)):
        raise HTTPException(422,'实际采集开始时间须包含时区且不能晚于当前时间')
    capture = session.get(RecordingCaptureTime,str(recording_id))
    if value:
        if capture is None:
            capture = RecordingCaptureTime(recording_id=str(recording_id))
            session.add(capture)
        capture.started_at,capture.actor,capture.updated_at = value,request.state.admin['username'],datetime.utcnow()
    elif capture:
        session.delete(capture)
    session.commit()
    return {'capture_started_at':iso(value) if value else None}


@router.post('/recordings/{recording_id}/retry')
def retry(recording_id: UUID, session: Session = Depends(get_session)):
    row = get_record(session, recording_id, lock=True)
    if row.status != 'failed':
        raise HTTPException(409, '只有失败录音可以重试，处理中请等待')
    if not data_path(row.original_path).is_file():
        raise HTTPException(404, '原始录音已缺失，请重新上传')
    row.status, row.error, row.claim_at, row.claim_id, row.processed_at = 'queued', None, None, None, None
    result = recording_json(row)
    session.commit()
    return result


@router.get('/recordings/{recording_id}/playback')
def playback(recording_id: UUID, session: Session = Depends(get_session)):
    row = get_record(session, recording_id)
    if row.status != 'ready' or not row.pcm_path:
        raise HTTPException(409, '录音尚未处理完成，暂不能回放')
    path = data_path(row.pcm_path)
    if not path.is_file():
        raise HTTPException(404, '音频文件已缺失，请联系管理员')
    return FileResponse(path, media_type='audio/wav', headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})


@router.get('/recordings/{recording_id}/original')
def original(recording_id: UUID, session: Session = Depends(get_session)):
    from app.core.media_preservation import _verified
    row = get_record(session,recording_id)
    path = data_path(row.original_path)
    try:
        stat = path.stat()
        if not _verified(str(path),stat.st_size,stat.st_mtime_ns,row.sha256):
            raise HTTPException(409,'原始录音完整性校验不一致，请核对留存原件')
    except OSError:
        raise HTTPException(404,'原始录音已缺失，请核对留存原件')
    return FileResponse(path,media_type='audio/wav',headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})


@router.get('/recordings/{recording_id}/segments/{segment_id}/playback')
def segment_playback(recording_id: UUID, segment_id: UUID, session: Session = Depends(get_session)):
    row = get_record(session, recording_id)
    segment = session.scalar(select(AudioSegment).where(AudioSegment.recording_id == str(recording_id), AudioSegment.segment_id == str(segment_id)))
    if segment is None:
        raise HTTPException(404, '录音片段不存在')
    if row.status != 'ready' or not row.pcm_path:
        raise HTTPException(409, '录音尚未处理完成')
    path = data_path(row.pcm_path)
    if not path.is_file():
        raise HTTPException(404, '音频文件已缺失，请联系管理员')
    try:
        with wave.open(str(path), 'rb') as source:
            start = round(segment.start_ms * source.getframerate() / 1000)
            end = min(source.getnframes(), round(segment.end_ms * source.getframerate() / 1000))
            if not 0 <= start < end:
                raise ValueError()
            source.setpos(start)
            frames, parameters = source.readframes(end - start), source.getparams()
        output = io.BytesIO()
        with wave.open(output, 'wb') as target:
            target.setparams(parameters); target.writeframes(frames)
    except (wave.Error, EOFError, ValueError):
        raise HTTPException(422, '片段时间与原音不一致，无法回放') from None
    return Response(output.getvalue(), media_type='audio/wav', headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
