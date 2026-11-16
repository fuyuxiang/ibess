"""多来源事件录像保全：边采集边复制已封口片段，持久化清单与捕获时哈希。"""
import hashlib
import os
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from uuid import UUID, uuid4
from fastapi import HTTPException
from sqlalchemy import func, or_, text
from app.models import Event, POCEventMeta
from app.supervision_models import SupervisionMediaHold, SemanticEventLink, SupervisionEvidenceWindow

HOLD_ROOT = Path(os.getenv('SUPERVISION_EVIDENCE_DIR','/supervision-evidence'))


def hold_dict(db, event_id):
    row = db.get(SupervisionMediaHold,event_id)
    status = row.status if row else 'PENDING'
    if row and status=='READY':
        saved = preserved_segments(db,event_id)
        try:
            if len(saved)!=len(row.manifest):
                raise HTTPException(409,'保全文件缺失')
            for item in saved:
                verify_preserved(item)
        except HTTPException:
            status = 'UNAVAILABLE'
    error = '保全文件缺失或完整性校验不一致，请核对原件与归档清单' if row and row.status=='READY' and status!='READY' else row.error if row else None
    return {'status':status, 'files':len(row.manifest or []) if row else 0,
            'error':error, 'updated_at':row.updated_at.replace(tzinfo=timezone.utc).isoformat() if row else None}


def evidence_readiness(db,event,verify=False):
    if db.get(POCEventMeta,event.event_id):
        return 'NOT_APPLICABLE'
    from app.core.supervision_evidence import audio_links,video_windows
    from app.audio_models import AudioRecording
    from app.routers.audio_recordings import data_path
    audio = audio_links(db,event)
    for item in audio:
        recording = db.get(AudioRecording,item['recording_id'])
        try:
            path = data_path(recording.original_path)
            stat = path.stat()
            if not _verified(str(path),stat.st_size,stat.st_mtime_ns,recording.sha256):
                return 'UNAVAILABLE'
        except (OSError,HTTPException,AttributeError):
            return 'UNAVAILABLE'
    if video_windows(db,event):
        return hold_dict(db,event.event_id)['status']
    return 'READY' if audio else 'UNAVAILABLE'


def preserved_segments(db,event_id):
    row = db.get(SupervisionMediaHold,event_id)
    result = []
    for index,item in enumerate(row.manifest or [] if row else []):
        file = HOLD_ROOT / item['path']
        if not file.resolve().is_relative_to(HOLD_ROOT.resolve()) or not file.is_file():
            continue
        result.append({**item,'file':file,'preserved':True,'manifest_index':index,
                       'duration_estimated':False,'playback_url':f'/api/events/{event_id}/preserved-recordings/{index}'})
    return result


@lru_cache(maxsize=4096)
def _verified(path,size,mtime,expected):
    digest = hashlib.sha256()
    with open(path,'rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()==expected


def verify_preserved(item):
    try:
        stat = item['file'].stat()
        if _verified(str(item['file']),stat.st_size,stat.st_mtime_ns,item['sha256']):
            return
    except OSError:
        pass
    raise HTTPException(409,'保全录像缺失或完整性校验失败，请联系管理员核查')


def _covered(items,start,end):
    cursor = start
    for item in sorted(items,key=lambda s:s['epoch']):
        if item['epoch'] > cursor+0.25:
            return False
        cursor = max(cursor,item['epoch']+item['duration'])
    return cursor >= end


def preserve_event(db,event,now=None):
    from app.routers.media_clips import _live_event_recordings, _segment_files, _mediamtx_path
    from app.core.supervision_evidence import video_windows
    now = now or datetime.utcnow()
    row = db.get(SupervisionMediaHold,event.event_id)
    if row is None:
        row = SupervisionMediaHold(event_id=event.event_id,manifest=[],attempts=0)
        db.add(row)
    if row.status=='READY' and hold_dict(db,event.event_id)['status']=='READY':
        return row
    row.attempts += 1
    row.updated_at = now
    row.next_retry_at = now+timedelta(seconds=15)
    windows = video_windows(db,event)
    if not windows:
        row.status,row.error = 'UNAVAILABLE','缺少实际发生时间或已关联机位，不能定位录像'
        row.next_retry_at = now+timedelta(days=1)
        return row
    # 兼容上一版本仅按epoch登记的清单；新清单按来源+epoch区分同一时刻的多机位。
    known = {(item.get('source_id',event.camera_id),item['epoch']):item for item in row.manifest or []}
    folder = HOLD_ROOT/str(UUID(event.event_id))
    folder.mkdir(parents=True,exist_ok=True)
    errors = []
    for window in windows:
        source_id = window['source_id']
        try:
            _,_,candidates = _live_event_recordings(UUID(event.event_id),db,source_id)
        except HTTPException as exc:
            errors.append(f"{source_id}：{exc.detail}")
            continue
        all_epochs = [epoch for epoch,_ in _segment_files(_mediamtx_path(source_id))]
        for item in candidates:
            if item['duration_estimated']:
                continue
            key = (source_id,item['epoch'])
            prior = known.get(key)
            if prior:
                try:
                    verify_preserved({**prior,'file':HOLD_ROOT/prior['path']})
                    continue
                except HTTPException:
                    pass
            try:
                stat = item['file'].stat()
            except OSError:
                continue
            sealed = any(epoch > item['epoch'] for epoch in all_epochs) or stat.st_mtime < time.time()-25
            if not sealed or stat.st_mtime > time.time()-2:
                continue
            temporary = folder/(uuid4().hex+'.tmp')
            digest = hashlib.sha256()
            try:
                with item['file'].open('rb') as source,temporary.open('wb') as target:
                    for chunk in iter(lambda:source.read(1024*1024),b''):
                        digest.update(chunk);target.write(chunk)
                    target.flush();os.fsync(target.fileno())
                after = item['file'].stat()
                if (stat.st_size,stat.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
                    continue
                sha = digest.hexdigest()
                if prior and prior['sha256']!=sha:
                    continue
                source_digest = hashlib.sha256(source_id.encode()).hexdigest()[:10]
                target = folder/f'{source_digest}-{item["epoch"]}-{sha[:12]}.mp4'
                os.replace(temporary,target)
                known[key] = {k:item[k] for k in ('epoch','duration','offset','contains_event')} | {
                    'source_id':source_id,'path':target.relative_to(HOLD_ROOT).as_posix(),'sha256':sha,'size':stat.st_size}
            except OSError:
                errors.append(f'{source_id}：原始片段轮转或读取失败')
            finally:
                temporary.unlink(missing_ok=True)
    row.manifest = sorted(known.values(),key=lambda item:(item.get('source_id',event.camera_id),item['epoch']))
    available = []
    for item in row.manifest:
        try:
            verify_preserved({**item,'file':HOLD_ROOT/item['path']})
            available.append(item)
        except HTTPException:
            pass
    def epoch(value):
        return value.replace(tzinfo=timezone.utc).timestamp()
    complete = all(_covered([s for s in available if s.get('source_id',event.camera_id)==w['source_id']],
        epoch(w['started_at']),epoch(w['ended_at'])) for w in windows)
    row.status = 'READY' if complete else 'PARTIAL' if row.manifest else 'UNAVAILABLE' if errors else 'PENDING'
    row.error = None if complete else ('；'.join(dict.fromkeys(errors)) or '行为完整时段及前后上下文尚未保全，可能仍在录制或存在缺口')
    if not complete and now-max(w['ended_at'] for w in windows) > timedelta(hours=6):
        row.next_retry_at = now+timedelta(days=1)
    return row


def preserve_due_events(factory,limit=20):
    with factory() as db:
        due = (db.query(Event.event_id).outerjoin(SupervisionMediaHold,SupervisionMediaHold.event_id==Event.event_id)
            .outerjoin(SemanticEventLink,SemanticEventLink.event_id==Event.event_id)
            .outerjoin(POCEventMeta,POCEventMeta.event_id==Event.event_id)
            .filter(Event.risk_level.in_(['low','medium','high']),POCEventMeta.event_id.is_(None),
                    or_(db.query(SupervisionEvidenceWindow).filter(SupervisionEvidenceWindow.event_id==Event.event_id).exists(),
                        (SemanticEventLink.event_id.is_(None)) & (~func.coalesce(Event.camera_id,'').like('audio%')) & (~func.coalesce(Event.skill_code,'').in_(['manual_audio']))),
                    or_(SupervisionMediaHold.event_id.is_(None), (SupervisionMediaHold.status!='READY') & (SupervisionMediaHold.next_retry_at<=datetime.utcnow())))
            .order_by(Event.created_at.desc()).limit(limit).all())
    for (event_id,) in due:
        with factory() as db:
            try:
                if not db.execute(text('select pg_try_advisory_xact_lock(hashtext(:id))'),{'id':'hold:'+event_id}).scalar():
                    continue
                event = db.query(Event).filter_by(event_id=event_id).one()
                from app.core.supervision_scope import capture_context
                capture_context(event,db)
                preserve_event(db,event)
                db.commit()
            except Exception:
                db.rollback()
                import logging
                logging.getLogger(__name__).exception('事件录像保全失败：event_id=%s',event_id)
                try:
                    hold = db.get(SupervisionMediaHold,event_id)
                    if hold is None:
                        hold = SupervisionMediaHold(event_id=event_id,manifest=[],attempts=0)
                        db.add(hold)
                    hold.status,hold.error = 'UNAVAILABLE','录像保全暂不可用，请核对原始录像与保全存储服务'
                    hold.next_retry_at = datetime.utcnow()+timedelta(seconds=60)
                    db.commit()
                except Exception:
                    db.rollback()
