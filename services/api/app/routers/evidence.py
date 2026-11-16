"""私有证据完整性 API（9.1）：哈希清单、录音哈希校验、关键帧私有预览（presigned）。

证据范围（最小路径）：音频录音（0.2，sha256 已存 DB）、视觉关键帧（vision-worker 上传至
MinIO evidence/keyframes/{camera}/）、语义研判结果（8.x，task 记录）。全部仅管理员可读。
"""
import hashlib
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_session
from app.config import settings

router = APIRouter()
log = logging.getLogger(__name__)
KEYFRAME_PREFIX = "keyframes/"


def _minio_client(session: Session):
    from minio import Minio
    import urllib3
    endpoint = settings.minio_endpoint
    return Minio(endpoint, access_key=settings.minio_root_user,
                 secret_key=settings.minio_root_password, secure=False,
                 http_client=urllib3.PoolManager(timeout=urllib3.Timeout(connect=2,read=8),retries=False)), settings.minio_bucket


@router.get('/evidence/objects/{evidence_id}/content')
def evidence_content(evidence_id: int, request: Request, session: Session = Depends(get_session)):
    """Private same-origin media playback, including byte ranges for native seeking."""
    import re
    from app.models import Evidence
    _actor(request)
    evidence = session.get(Evidence, evidence_id)
    if evidence is None:
        raise HTTPException(404, '证据文件记录不存在')
    client, bucket = _minio_client(session)
    try:
        size = client.stat_object(bucket, evidence.object_key).size
    except Exception:
        raise HTTPException(503, '证据文件暂不可读取，请检查存储服务') from None
    start, end, partial = 0, size-1, False
    value = request.headers.get('range')
    if value:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', value.strip())
        if not match or (not match[1] and not match[2]):
            raise HTTPException(416, '无效的媒体范围', headers={'Content-Range':f'bytes */{size}'})
        if match[1]:
            start, end = int(match[1]), min(int(match[2]),size-1) if match[2] else size-1
        else:
            start = max(0,size-int(match[2]))
        if size == 0 or start > end or start >= size:
            raise HTTPException(416, '媒体范围超出文件长度', headers={'Content-Range':f'bytes */{size}'})
        partial = True
    try:
        upstream = client.get_object(bucket,evidence.object_key,offset=start,length=max(0,end-start+1))
    except Exception:
        raise HTTPException(503, '证据文件暂不可读取，请重试') from None
    def content():
        try:
            while chunk := upstream.read(64*1024):
                yield chunk
        finally:
            upstream.close(); upstream.release_conn()
    headers = {'Cache-Control':'no-store', 'Accept-Ranges':'bytes', 'Content-Length':str(max(0,end-start+1)), 'X-Content-Type-Options':'nosniff'}
    if partial:
        headers['Content-Range'] = f'bytes {start}-{end}/{size}'
    media_type = {'video':'video/mp4', 'audio':'audio/wav', 'keyframe':'image/jpeg'}.get(evidence.evidence_type,'application/octet-stream')
    return StreamingResponse(content(),media_type=media_type,status_code=206 if partial else 200,headers=headers)


@router.get("/evidence/manifest")
def manifest(session: Session = Depends(get_session)):
    """证据资产清单：音频录音（含登记哈希）、关键帧对象统计。"""
    recordings = session.execute(text(
        "select recording_id, title, sha256, duration_ms, status, pcm_path from audio_recordings order by created_at")).mappings().all()
    client, bucket = _minio_client(session)
    keyframes = []
    try:
        objects = client.list_objects(bucket, prefix=KEYFRAME_PREFIX, recursive=True)
        for item in objects:
            keyframes.append({"object_key": item.object_name, "size": item.size,
                              "etag": item.etag, "last_modified": item.last_modified.isoformat() if item.last_modified else None})
            if len(keyframes) >= 200:
                break
    except Exception as exc:  # MinIO不可用时清单仍返回音频部分，并如实标注
        keyframes = [{"error": f"关键帧对象列举失败：{exc}"}]
    return {
        "recordings": [{"recording_id": row["recording_id"], "title": row["title"], "sha256": row["sha256"],
                        "duration_ms": row["duration_ms"], "status": row["status"]} for row in recordings],
        "keyframes": keyframes,
    }


@router.get("/evidence/verify/recording/{recording_id}")
def verify_recording(recording_id: UUID, session: Session = Depends(get_session)):
    """重算录音存储文件 SHA-256 并与登记哈希比对，返回完整性结论。"""
    row = session.execute(text(
        "select recording_id, title, sha256, original_path from audio_recordings where recording_id = :rid"),
        {"rid": str(recording_id)}).mappings().first()
    if row is None:
        raise HTTPException(404, "录音证据不存在")
    # sha256 在 0.2 登记时按"原始上传文件"计算；校验必须针对同一文件，而非转换后的PCM。
    path = row["original_path"]
    if not path:
        raise HTTPException(404, "该录音无原始文件记录，无法校验")
    import os
    if not os.path.isabs(path):
        path = os.path.join("/audio-data", path)
    if not os.path.exists(path):
        raise HTTPException(404, f"存储文件不存在：{os.path.basename(path)}")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    return {"recording_id": row["recording_id"], "title": row["title"],
            "expected": row["sha256"], "actual": actual,
            "verified": actual == row["sha256"]}


@router.get("/evidence/keyframes")
def keyframes(camera_id: str | None = None, limit: int = 24, session: Session = Depends(get_session)):
    """关键帧私有预览：presigned GET URL（15分钟有效），仅管理员可获取。"""
    from datetime import timedelta
    client, bucket = _minio_client(session)
    prefix = KEYFRAME_PREFIX + (f"{camera_id}/" if camera_id else "")
    _ensure_keyframe_refresher(client, bucket)
    with _KEYFRAME_LOCK:
        cached = _KEYFRAME_CACHE.get("all")
    if not cached:
        try:
            objects = sorted(client.list_objects(bucket, prefix="keyframes/", recursive=True),
                             key=lambda item: item.object_name, reverse=True)
            with _KEYFRAME_LOCK:
                _KEYFRAME_CACHE["all"] = (time.time(), objects)
        except Exception as exc:
            raise HTTPException(503, f"关键帧对象存储不可用：{exc}")
    else:
        objects = cached[1]
    items = []
    for item in objects:
        if prefix and not item.object_name.startswith(prefix):
            continue
        if len(items) >= min(max(limit, 1), 100):
            break
        items.append({"object_key": item.object_name, "size": item.size,
                      "last_modified": item.last_modified.isoformat() if item.last_modified else None,
                      "preview_url": f"/api/evidence/keyframes/object?key={item.object_name}"})
    matched = sum(1 for item in objects if not prefix or item.object_name.startswith(prefix))
    return {"items": items, "total": len(items), "matched": matched}


_THUMB_CACHE: dict[tuple[str, int], bytes] = {}
_THUMB_CACHE_MAX = 300


def _resize_jpeg(blob: bytes, width: int) -> bytes:
    """按宽度等比缩小并压成 JPEG；列表缩略图从 175KB 降到 10KB 左右。"""
    import io as _io
    from PIL import Image
    image = Image.open(_io.BytesIO(blob))
    image = image.convert("RGB")
    ratio = width / float(image.width or width)
    if ratio < 1:
        image = image.resize((width, max(1, int(image.height * ratio))), Image.LANCZOS)
    out = _io.BytesIO()
    image.save(out, "JPEG", quality=78, optimize=True)
    return out.getvalue()


@router.get("/evidence/keyframes/object")
def keyframe_object(key: str, w: int = 0, session: Session = Depends(get_session)):
    """关键帧对象鉴权流式回放（私有回放）：仅管理员，键必须位于 keyframes/ 或 audit/ 前缀内。

    w>0 时返回等比缩放的 JPEG 缩略图（列表用），结果按 (key,w) 缓存。
    """
    allowed = (KEYFRAME_PREFIX, "audit/")
    if not key.startswith(allowed) or ".." in key:
        raise HTTPException(422, "非法对象键")
    from fastapi import Response
    if w and w <= 640:
        cached = _THUMB_CACHE.get((key, w))
        if cached:
            return Response(cached, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=3600"})
    client, bucket = _minio_client(session)
    try:
        response = client.get_object(bucket, key)
    except Exception as exc:
        raise HTTPException(404, f"关键帧对象不存在或不可读取：{exc}")

    if w and w <= 640:
        try:
            blob = response.read()
        finally:
            response.close()
            response.release_conn()
        try:
            thumb = _resize_jpeg(blob, w)
        except Exception:
            thumb = blob
        if len(_THUMB_CACHE) >= _THUMB_CACHE_MAX:
            _THUMB_CACHE.pop(next(iter(_THUMB_CACHE)))
        _THUMB_CACHE[(key, w)] = thumb
        return Response(thumb, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=3600"})

    def _stream():
        try:
            for chunk in response.stream(65536):
                yield chunk
        finally:
            try:
                response.close()
                response.release_conn()
            except Exception:
                pass

    from urllib.parse import quote
    return StreamingResponse(
        _stream(), media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=300",
                 "Content-Disposition": f"inline; filename*=UTF-8''{quote(key.split('/')[-1])}"})


# ---- 视频证据资产（示例视频登记与校验）----

import threading
import time
import uuid
from datetime import datetime, timezone


def _actor(request: Request):
    admin = getattr(request.state, "admin", None)
    if not isinstance(admin, dict) or not admin.get("username"):
        raise HTTPException(403, "管理员身份缺失，无法访问视频证据")
    return admin["username"]


VIDEO_DIR_ENV = ("VIDEO_EVIDENCE_DIR", "/video-evidence")
VIDEO_SUFFIXES = (".mp4", ".wav", ".mkv")


def _video_dir():
    import os
    directory = os.getenv(*VIDEO_DIR_ENV)
    if not directory or not os.path.isdir(directory):
        raise HTTPException(503, "视频证据目录不可用（未挂载或不存在）")
    return directory


# 关键帧对象清单缓存：MinIO 全量列举 2 万+ 对象约4秒；60秒TTL+后台刷新保证列表秒回。
_KEYFRAME_CACHE: dict[str, tuple[float, list]] = {}
_KEYFRAME_CACHE_TTL = 60.0
_KEYFRAME_REFRESHER_STARTED = False
_KEYFRAME_LOCK = threading.Lock()


def _refresh_keyframe_cache(client, bucket):
    """后台线程：每60秒刷新各前缀对象清单（成功才替换，失败保留旧数据）。"""
    global _KEYFRAME_REFRESHER_STARTED
    while True:
        try:
            objects = list(client.list_objects(bucket, prefix="keyframes/", recursive=True))
            objects.sort(key=lambda item: item.object_name, reverse=True)
            stamp = time.time()
            with _KEYFRAME_LOCK:
                _KEYFRAME_CACHE["all"] = (stamp, objects)
        except Exception:
            pass
        time.sleep(_KEYFRAME_CACHE_TTL)


def _ensure_keyframe_refresher(client, bucket):
    global _KEYFRAME_REFRESHER_STARTED
    if _KEYFRAME_REFRESHER_STARTED:
        return
    with _KEYFRAME_LOCK:
        if _KEYFRAME_REFRESHER_STARTED:
            return
        _KEYFRAME_REFRESHER_STARTED = True
        threading.Thread(target=_refresh_keyframe_cache, args=(client, bucket), daemon=True).start()


def _file_sha256(path: str) -> str:
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@router.get("/evidence/videos")
def list_video_assets(request: Request, session: Session = Depends(get_session)):
    """列出视频证据：首次发现的文件自动登记SHA-256（登记人=管理员），后续校验以首次登记为准。"""
    from app.video_evidence_models import EvidenceVideoAsset
    import os
    actor = _actor(request)
    directory = _video_dir()
    registered = {row.file_name: row for row in session.query(EvidenceVideoAsset).all()}
    items = []
    new_files = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(VIDEO_SUFFIXES):
            continue
        path = os.path.join(directory, name)
        size = os.path.getsize(path)
        row = registered.get(name)
        if row is None:
            digest = _file_sha256(path)
            row = EvidenceVideoAsset(asset_id=str(uuid.uuid4()), file_name=name, sha256=digest,
                                     size=str(size), source_label="示例视频素材", actor=actor,
                                     registered_at=datetime.now(timezone.utc))
            session.add(row)
            new_files.append(name)
        items.append({"asset_id": row.asset_id, "file_name": row.file_name, "sha256": row.sha256,
                      "size": int(row.size), "registered_at": row.registered_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                      "actor": row.actor, "current_size": size})
    if new_files:
        session.commit()
    return {"items": items, "total": len(items), "newly_registered": len(new_files)}


@router.get("/evidence/verify/video")
def verify_video(name: str, session: Session = Depends(get_session)):
    """重算视频文件SHA-256与首次登记哈希比对，检测登记后的篡改。"""
    from app.video_evidence_models import EvidenceVideoAsset
    import os
    if "/" in name or ".." in name or not name.endswith(VIDEO_SUFFIXES):
        raise HTTPException(422, "非法文件名")
    row = session.query(EvidenceVideoAsset).filter_by(file_name=name).first()
    if row is None:
        raise HTTPException(404, "该视频未登记为证据资产，请先调用 /api/evidence/videos")
    directory = _video_dir()
    path = os.path.join(directory, name)
    if not os.path.exists(path):
        raise HTTPException(404, "视频文件已不存在")
    actual = _file_sha256(path)
    return {"file_name": name, "expected": row.sha256, "actual": actual,
            "verified": actual == row.sha256,
            "registered_at": row.registered_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}

@router.get("/events/{event_id}/evidence-package")
def evidence_package(event_id: str, request: Request, session: Session = Depends(get_session)):
    """流式构建证据包，保全录像使用捕获时哈希核验，缺失与异常明确入清单。"""
    import io
    import json
    import shutil
    import tempfile
    import zipfile
    from datetime import datetime, timezone
    from pathlib import PurePosixPath
    from app.core.workflow import event_detail, find_event, add_audit
    from app.routers.media_clips import _all_event_recordings
    from app.routers.audio_recordings import data_path
    from app.audio_models import AudioRecording
    from app.models import Evidence
    from uuid import UUID

    who = _actor(request)
    find_event(session,event_id)
    add_audit(session,event_id,'evidence_export','发起导出事件证据包，包含文件完整性与缺失清单',who)
    session.commit()
    detail = event_detail(session,event_id)
    output = tempfile.SpooledTemporaryFile(max_size=8*1024*1024)
    integrity, manifest = [], []
    used = 0
    max_bytes = 200*1024*1024
    try:
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
            def add_stream(name,source,expected=None):
                nonlocal used
                digest = hashlib.sha256()
                size = 0
                with tempfile.SpooledTemporaryFile(max_size=4*1024*1024) as content:
                    while True:
                        chunk = source.read(64*1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if used+size > max_bytes:
                            raise HTTPException(413,'事件媒体合计超过200 MiB，请按片段分别导出')
                        digest.update(chunk);content.write(chunk)
                    sha = digest.hexdigest()
                    if expected and sha!=expected:
                        integrity.append({'file':name,'status':'hash_mismatch','expected':expected,'actual':sha})
                        return None
                    content.seek(0)
                    with archive.open(name,'w') as target:
                        shutil.copyfileobj(content,target,64*1024)
                    used += size
                    manifest.append((name,sha))
                    integrity.append({'file':name,'status':'verified' if expected else 'hashed_at_export','sha256':sha})
                    return sha
            def add(name,content):
                return add_stream(name,io.BytesIO(content))
            def missing(name,reason):
                integrity.append({'file':name,'status':'unavailable','reason':reason})
            client,bucket = _minio_client(session)
            for evidence in session.query(Evidence).filter(Evidence.event_id==event_id).order_by(Evidence.id).all():
                name = f'media/{evidence.id}_{PurePosixPath(evidence.object_key).name}'
                response = None
                try:
                    response = client.get_object(bucket,evidence.object_key)
                    digest = add_stream(name,response,evidence.sha256)
                    if digest and not evidence.sha256:
                        evidence.sha256 = digest
                except HTTPException:
                    raise
                except Exception:
                    missing(name,'关联对象无法读取，请核对证据存储服务')
                finally:
                    if response is not None:
                        response.close();response.release_conn()
            if not detail['is_demo'] and detail.get('context_evidence',{}).get('videos'):
                try:
                    segments,failures = _all_event_recordings(UUID(event_id),session)
                    for failure in failures:
                        missing('recordings/'+failure['source_id'],failure['reason'])
                    timeline = []
                    for i,segment in enumerate(segments):
                        name = f'recordings/{i:03d}_{segment["file"].name}'
                        try:
                            with segment['file'].open('rb') as source:
                                add_stream(name,source,segment.get('sha256'))
                        except OSError:
                            missing(name,'对应录像文件已缺失或暂不可读取')
                        timeline.append({'file':name,'source_id':segment.get('source_id',detail['camera_id']),'start_epoch':segment['epoch'],'offset_seconds':segment['offset'],
                            'duration_seconds':segment['duration'],'duration_estimated':segment['duration_estimated'],
                            'contains_event':segment['contains_event'],'preserved':bool(segment.get('preserved'))})
                    add('recordings/timeline.json',json.dumps(timeline,ensure_ascii=False,indent=2).encode())
                except (HTTPException,ValueError) as exc:
                    if isinstance(exc,HTTPException) and exc.status_code==413:
                        raise
                    missing('recordings','事件窗口没有可读取的对应录像')
            for audio_index,linked_audio in enumerate(detail.get('context_evidence',{}).get('audio',[])):
                recording = session.get(AudioRecording,linked_audio['recording_id'])
                audio_name = 'audio/original.wav' if audio_index==0 else f'audio/{linked_audio["recording_id"]}.wav'
                try:
                    if not recording:
                        raise OSError()
                    with data_path(recording.original_path).open('rb') as source:
                        add_stream(audio_name,source,recording.sha256)
                except HTTPException as exc:
                    if exc.status_code==413:
                        raise
                    missing(audio_name,'原始录音文件不可读取')
                except OSError:
                    missing(audio_name,'原始录音文件不可读取')
            session.commit()
            detail = event_detail(session,event_id)
            add('event.json',json.dumps(detail,ensure_ascii=False,indent=2,default=str).encode())
            add('integrity.json',json.dumps({'exported_at':datetime.now(timezone.utc).isoformat(),'items':list(integrity)},ensure_ascii=False,indent=2).encode())
            add('README.txt',('智能评标监管系统 · 事件证据包\n'
                'event.json 包含人工结论、历次归档版本、资料缺失说明及操作留痕。\n'
                'media/ 为对象存储证据，recordings/ 为事件窗口录像，audio/ 为原始录音。\n'
                'verified 表示与已有登记哈希一致；hashed_at_export 仅表示导出时计算哈希。\n'
                '缺失和哈希不一致详见 integrity.json，不一致文件不会进入证据包。\n'
                '机器线索不等同于违规认定，模拟事件不构成真实现场证据。\n').encode())
            archive.writestr('manifest.sha256',''.join(f'{sha}  {name}\n' for name,sha in manifest))
        output.seek(0)
    except BaseException:
        output.close()
        raise
    def chunks():
        try:
            while chunk := output.read(64*1024):
                yield chunk
        finally:
            output.close()
    return StreamingResponse(chunks(),media_type='application/zip',headers={
        'Content-Disposition':f'attachment; filename="evidence-{event_id}.zip"','Cache-Control':'no-store'})
