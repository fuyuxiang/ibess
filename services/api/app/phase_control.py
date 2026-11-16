"""2.2控制可靠性：PG是真相，Redis只负责通知。"""
from datetime import datetime, timedelta, timezone
from copy import deepcopy
import json
import logging
import re
import threading
import uuid

from redis.exceptions import RedisError
from sqlalchemy import desc, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.phase_config import _dict
from app.phase_control_models import PhaseControlAck, PhaseControlOutbox
from app.phase_models import PhaseSnapshot
from app.models import EvalSession


class PhaseControlError(ValueError):
    def __init__(self, code, message): self.code = code; super().__init__(message)


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PhaseControlError('INVALID_ACK', '时间必须是带时区的UTC时间')
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise PhaseControlError('INVALID_ACK', '时间必须使用UTC')
    return value.astimezone(timezone.utc)


def _ack_time(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z', value):
        raise PhaseControlError('INVALID_ACK','确认时间须为UTC ISO字符串')
    try:
        return datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:
        raise PhaseControlError('INVALID_ACK','确认时间无效') from None


def _consumers(values):
    if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() or len(v)>128 for v in values):
        raise PhaseControlError('INVALID_CONSUMERS', '必需消费者必须是非空去重列表')
    if len(set(values))!=len(values):
        raise PhaseControlError('INVALID_CONSUMERS','必需消费者不得重复')
    return sorted(values)


def _status(db, snapshot_id):
    snapshot=db.get(PhaseSnapshot,snapshot_id)
    outbox=db.scalar(select(PhaseControlOutbox).where(PhaseControlOutbox.stage_snapshot_id==snapshot_id))
    if snapshot is None: raise PhaseControlError('SNAPSHOT_NOT_FOUND','阶段快照不存在')
    if outbox is None: raise PhaseControlError('CONTROL_NOT_ENQUEUED','阶段快照尚未写入控制队列')
    acks=db.scalars(select(PhaseControlAck).where(PhaseControlAck.stage_snapshot_id==snapshot_id)).all()
    by={a.consumer_id:a for a in acks}
    statuses=[by[c].status for c in outbox.required_consumers if c in by]
    if len(statuses)==len(outbox.required_consumers) and all(s=='applied' for s in statuses): sync='applied'
    elif any(s=='failed' for s in statuses): sync='failed'
    elif any(s=='missing_asset' for s in statuses): sync='missing_asset'
    elif statuses: sync='partially_applied'
    else: sync='pending'
    return {'stage_snapshot_id':snapshot_id,'session_id':snapshot.session_id,'phase_id':snapshot.phase_id,'phase_version':snapshot.phase_version,'required_consumers':list(outbox.required_consumers),'acknowledged_consumers':sorted(by),'sync_status':sync,'delivery_status':outbox.delivery_status,'attempts':outbox.attempts,'last_error_code':outbox.last_error_code}


def enqueue_snapshot(db: Session, snapshot_id, *, required_consumers):
    consumers=_consumers(required_consumers)
    snapshot=db.scalar(select(PhaseSnapshot).where(PhaseSnapshot.stage_snapshot_id==snapshot_id).with_for_update())
    if snapshot is None: raise PhaseControlError('SNAPSHOT_NOT_FOUND','阶段快照不存在')
    existing=db.scalar(select(PhaseControlOutbox).where(PhaseControlOutbox.stage_snapshot_id==snapshot_id).with_for_update())
    if existing:
        if list(existing.required_consumers)!=consumers: raise PhaseControlError('CONTROL_CONFLICT','控制消费者集合不能修改')
        return existing.notification_id
    row=PhaseControlOutbox(notification_id=str(uuid.uuid4()),stage_snapshot_id=snapshot_id,session_id=snapshot.session_id,project_id=snapshot.project_id,room_id=snapshot.room_id,source_id=snapshot.source_id,phase_version=snapshot.phase_version,required_consumers=consumers)
    db.add(row)
    db.flush()
    return row.notification_id


def _payload(snapshot):
    return _dict(snapshot)


def publish_pending(session_factory: sessionmaker, publisher, *, stream='phase-control', now=None, batch_size=100):
    if type(batch_size) is not int or not 1<=batch_size<=100:
        raise PhaseControlError('INVALID_BATCH','发布批次大小必须为1～100')
    now=_utc(now or datetime.now(timezone.utc)); count=0
    with session_factory.begin() as db:
        rows=db.scalars(select(PhaseControlOutbox).where(PhaseControlOutbox.delivery_status=='pending',
            or_(PhaseControlOutbox.next_attempt_at.is_(None),PhaseControlOutbox.next_attempt_at<=now))
            .order_by(PhaseControlOutbox.created_at).limit(batch_size).with_for_update(skip_locked=True)).all()
        for row in rows:
            snapshot=db.get(PhaseSnapshot,row.stage_snapshot_id)
            if snapshot is None: row.last_error_code='SNAPSHOT_NOT_FOUND'; continue
            row.attempts+=1
            row.last_attempt_at=now
            message={'type':'phase_changed','notification_id':row.notification_id,'session_id':row.session_id,'source_id':row.source_id,'phase_version':str(row.phase_version),'payload':json.dumps(_payload(snapshot),ensure_ascii=False,separators=(',',':'))}
            try:
                publisher.xadd(stream,message,maxlen=1000,approximate=True)
            except (RedisError, OSError, TimeoutError):
                row.last_error_code='REDIS_UNAVAILABLE'
                row.next_attempt_at=now+timedelta(seconds=min(60,2**min(row.attempts,6)))
                continue
            row.delivery_status='published';row.published_at=now;row.next_attempt_at=None;row.last_error_code=None;count+=1
    return count


def read_latest_config(db: Session, session_id):
    if db.scalar(select(EvalSession.session_id).where(EvalSession.session_id==session_id)) is None:
        raise PhaseControlError('SESSION_NOT_FOUND','场次不存在')
    snapshot=db.scalar(select(PhaseSnapshot).where(PhaseSnapshot.session_id==session_id).order_by(desc(PhaseSnapshot.phase_version)))
    if snapshot is None: raise PhaseControlError('PHASE_NOT_CONFIGURED','场次尚未配置阶段快照')
    return _dict(snapshot)


def record_ack(db: Session, value):
    required={'contract_version','project_id','room_id','session_id','source_id','stage_snapshot_id','phase_id','phase_version','consumer_id','received_at','applied_at','status','error'}
    if not isinstance(value,dict) or set(value)!=required:raise PhaseControlError('INVALID_ACK','确认字段不完整')
    if any(not isinstance(value[k],str) or not value[k].strip() or len(value[k])>256 for k in
           ('project_id','room_id','session_id','source_id','stage_snapshot_id','phase_id','consumer_id')):
        raise PhaseControlError('INVALID_ACK','确认标识为空或格式无效')
    if type(value['phase_version']) is not int or value['phase_version']<1:
        raise PhaseControlError('INVALID_ACK','确认版本必须为正整数')
    received=_ack_time(value['received_at']);applied=_ack_time(value['applied_at']) if value['applied_at'] is not None else None
    if value['contract_version']!='2.0.0' or value['status'] not in ('applied','missing_asset','failed') or (value['status']=='applied' and (applied is None or value['error'] is not None)) or (value['status']!='applied' and (applied is not None or not isinstance(value['error'],dict))):raise PhaseControlError('INVALID_ACK','确认状态、时间或错误字段不一致')
    error=value['error']
    if error is not None:
        if set(error)!= {'code','message'} or any(not isinstance(error[k],str) or not error[k].strip() for k in ('code','message')) or len(error['code'])>64 or len(error['message'])>512:
            raise PhaseControlError('INVALID_ACK','错误信息不符合脱敏错误结构')
        # 消费者报错正文可能带路径/凭据，存储固定安全文案。
        safe_codes={'ASSET_MISSING':'所需素材不可用','APPLY_FAILED':'消费者应用失败'}
        code=error['code'] if error['code'] in safe_codes else 'CONSUMER_FAILED'
        error={'code':code,'message':safe_codes.get(code,'消费者报告异常')}
    snapshot=db.get(PhaseSnapshot,value['stage_snapshot_id'])
    outbox=db.scalar(select(PhaseControlOutbox).where(PhaseControlOutbox.stage_snapshot_id==value['stage_snapshot_id']).with_for_update().execution_options(populate_existing=True))
    if snapshot is None:raise PhaseControlError('SNAPSHOT_NOT_FOUND','阶段快照不存在')
    if outbox is None:raise PhaseControlError('CONTROL_NOT_FOUND','阶段控制记录不存在')
    for field in ('project_id','room_id','session_id','source_id','phase_id','phase_version'):
        if getattr(snapshot,field)!=value[field]:raise PhaseControlError('ACK_SCOPE_MISMATCH','确认不属于该阶段快照')
    if value['consumer_id'] not in outbox.required_consumers:raise PhaseControlError('CONSUMER_NOT_REQUIRED','消费者不在必需集合')
    if received<snapshot.effective_at or (applied is not None and applied<received):raise PhaseControlError('INVALID_ACK','应用或接收时间不属于快照有效区间')
    current=db.scalar(select(PhaseControlAck).where(PhaseControlAck.stage_snapshot_id==value['stage_snapshot_id'],PhaseControlAck.consumer_id==value['consumer_id']).with_for_update())
    if current is None:
        current=PhaseControlAck(stage_snapshot_id=snapshot.stage_snapshot_id,session_id=snapshot.session_id,project_id=snapshot.project_id,room_id=snapshot.room_id,source_id=snapshot.source_id,phase_id=snapshot.phase_id,phase_version=snapshot.phase_version,consumer_id=value['consumer_id'],received_at=received,applied_at=applied,status=value['status'],error=error);db.add(current)
    elif received>current.received_at:
        current.received_at=received;current.applied_at=applied;current.status=value['status'];current.error=error
    elif received==current.received_at:
        if (current.status,current.applied_at,current.error)!=(value['status'],applied,error):
            raise PhaseControlError('ACK_CONFLICT','同一接收时间的确认内容冲突')
    # 更旧的结果不覆盖已有确认；返回数据库实际保存版本。
    db.flush()
    return _ack_dict(current)


def _ack_dict(row):
    return {'contract_version':'2.0.0',**{k:getattr(row,k) for k in ('project_id','room_id','session_id','source_id','stage_snapshot_id','phase_id','phase_version','consumer_id','status')},
        'received_at':row.received_at.astimezone(timezone.utc).isoformat().replace('+00:00','Z'),
        'applied_at':row.applied_at.astimezone(timezone.utc).isoformat().replace('+00:00','Z') if row.applied_at else None,'error':deepcopy(row.error)}


def get_control_status(db, session_id):
    if db.scalar(select(EvalSession.session_id).where(EvalSession.session_id==session_id)) is None:
        raise PhaseControlError('SESSION_NOT_FOUND','场次不存在')
    snapshot=db.scalar(select(PhaseSnapshot).where(PhaseSnapshot.session_id==session_id).order_by(desc(PhaseSnapshot.phase_version)))
    if snapshot is None:raise PhaseControlError('PHASE_NOT_CONFIGURED','场次尚未配置阶段快照')
    return _status(db,snapshot.stage_snapshot_id)


class PhaseControlPublisher:
    """每轮独立PG事务，支持可中断轮询；调用方提供有限超时Redis客户端。"""
    def __init__(self, session_factory, publisher, *, stream='phase-control', poll_seconds=1,
                 batch_size=10, close_client=False):
        if (isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int,float))
                or not 0 < poll_seconds <= 60 or type(batch_size) is not int or not 1 <= batch_size <= 100):
            raise PhaseControlError('INVALID_PUBLISHER_CONFIG','发布间隔或批次配置无效')
        self.session_factory = session_factory
        self.publisher = publisher
        self.stream = stream
        self.poll_seconds = poll_seconds
        self.batch_size = batch_size
        self.close_client = close_client
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._cycles = 0
        self._published = 0
        self._last_error_code = None
        self._last_checked_at = None

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name='phase-control-publisher', daemon=True)
            self._thread.start()

    def stop(self, *, timeout=30):
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            return not thread.is_alive()
        return True

    def status(self):
        with self._lock:
            return {'running':self._thread is not None and self._thread.is_alive(),
                'cycles':self._cycles,'published_count':self._published,
                'last_error_code':self._last_error_code,'last_checked_at':self._last_checked_at}

    def _run(self):
        log = logging.getLogger('poc.phase-control')
        try:
            while not self._stop.is_set():
                code = None
                count = 0
                try:
                    count = publish_pending(self.session_factory, self.publisher,
                        stream=self.stream, batch_size=self.batch_size)
                except SQLAlchemyError:
                    code = 'CONTROL_STORAGE_UNAVAILABLE'
                except (RedisError, OSError, TimeoutError):
                    code = 'REDIS_UNAVAILABLE'
                except Exception:
                    # 异常文本可能带凭据，只保存固定故障码；不让线程悄然退出。
                    code = 'CONTROL_PUBLISHER_FAILED'
                with self._lock:
                    self._cycles += 1
                    self._published += count
                    changed = self._last_error_code != code
                    self._last_error_code = code
                    self._last_checked_at = datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
                if code and changed:
                    log.warning('控制发布循环暂不可用；code=%s', code)
                self._stop.wait(self.poll_seconds)
        finally:
            if self.close_client:
                try:
                    self.publisher.close()
                except Exception:
                    log.warning('控制发布客户端清理失败；code=CLIENT_CLOSE_FAILED')
