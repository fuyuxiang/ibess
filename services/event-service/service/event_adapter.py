"""执行卡 2.3：线索落库与幂等事件映射。

persist_clue：先以 clue_id 唯一插入线索（方言 ON CONFLICT DO NOTHING，重放只有一条），插入成功后才建 PENDING Event
并把 event_id 写回——顺序保证并发重放不留孤儿事件；同场次 / 源 / 场景 / 规则 / 媒体轮次、同一主体（缺口 / 工位 / 轨迹 / 人员）
在冷却窗内的另一条 suspected 复用首条（链首锚定，不无限归并）；normal / insufficient 只留线索；
S10 恢复关闭同 gap_id 的 PENDING 事件并写 Decision(auto_close)。gap_id 只走结构化通道（调用方传 payload.gap_id，
或 gap 型证据的 evidence_id），不解析任何自述文本。处理失败单独落 clue_processing（failed）。
收据：seen() 在处理前查、mark_processed() 在线索落库之后记，崩溃只会重复处理（幂等），不会丢观察。
"""
from __future__ import annotations

import logging
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from service.clue_models import ClueProcessingRecord, ObservationReceipt, RiskClueRecord, utc_now
from service.models import Decision, Event
from service.stage_dispatcher import DispatchError, RECOVERY_TYPES, parse_utc, validate_clue

log = logging.getLogger("event-adapter")
MODEL_NAME = "stage_dispatcher"
MODEL_VERSION = "2.3-v1"
DEFAULT_COOLDOWN_SECONDS = 5.0


def _insert_ignore(db: Session, table, values: dict) -> int:
    """按方言生成 INSERT … ON CONFLICT DO NOTHING，返回插入行数（0 表示已存在）。"""
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError(f"不支持的数据库方言 {dialect}")
    stmt = insert(table).values(**values).on_conflict_do_nothing()
    # psycopg3 may report rowcount=-1 for INSERT. RETURNING is the durable result,
    # including the DO NOTHING branch, so delivery receipts cannot be misreported.
    primary_key = next(iter(table.primary_key.columns))
    return int(db.execute(stmt.returning(primary_key)).scalar_one_or_none() is not None)


def _naive_utc(value: str) -> datetime:
    return parse_utc(value).astimezone(timezone.utc).replace(tzinfo=None)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def gap_id_of(observation: dict) -> Optional[str]:
    """健康观察的结构化 gap_id（5.3 的 payload.gap_id）；别的类型返回 None。"""
    payload = observation.get("payload") if isinstance(observation, dict) else None
    value = payload.get("gap_id") if isinstance(payload, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def gap_id_from_clue(clue: dict) -> Optional[str]:
    """只从 gap 型证据引用推导（5.3 约定 evidence_id = f"{gap_id}:evidence"）。"""
    for ref in clue.get("evidence_refs", []):
        if isinstance(ref, dict) and ref.get("evidence_type") == "gap" and isinstance(ref.get("evidence_id"), str):
            evidence_id = ref["evidence_id"]
            return evidence_id[: -len(":evidence")] if evidence_id.endswith(":evidence") else evidence_id
    return None


def event_message(event: Event) -> dict:
    """与旧 main.py 写 `events` 流同格式，下游（media-service / SSE）不改。"""
    return {
        "event_id": event.event_id, "event_type": event.event_type, "camera_id": event.camera_id,
        "session_id": event.session_id, "risk_level": event.risk_level, "reason": event.reason,
        "status": event.status, "occurred_at": event.occurred_at.isoformat(),
    }


class EventAdapter:
    def __init__(self, session_factory, *, cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
                 publisher: Optional[Callable[[dict], None]] = None) -> None:
        if cooldown_seconds < 0:
            raise ValueError("冷却时长不能为负")
        self.session_factory = session_factory
        self.cooldown = timedelta(seconds=cooldown_seconds)
        self.publisher = publisher

    # ---- 观察收据 ----
    def seen(self, observation: dict) -> bool:
        """处理前查：已记收据的观察（处理完的重投）返回 True，调用方跳过。"""
        with self.session_factory() as db:
            return db.get(ObservationReceipt, observation["observation_id"]) is not None

    def mark_processed(self, observation: dict) -> bool:
        """线索落库之后记收据；重复记返回 False。"""
        with self.session_factory.begin() as db:
            inserted = _insert_ignore(db, ObservationReceipt.__table__, {
                "observation_id": observation["observation_id"], "session_id": observation.get("session_id"),
                "source_id": observation["source_id"], "type": observation["type"], "received_at": utc_now()})
        return inserted == 1

    record_observation = mark_processed   # 兼容旧名；语义是「处理完才记」

    # ---- 线索 ----
    def persist_clue(self, clue: dict, *, gap_id: Optional[str] = None, close_gap: Optional[bool] = None) -> str:
        """gap_id：调用方从观察 payload 传（gap_id_of(observation)），缺省只从 gap 型证据推导。
        close_gap：恢复观察（RECOVERY_TYPES）传 True 以关闭同缺口事件；缺省按「S10 且 normal」判。"""
        validate_clue(clue, None, clue.get("scene_id"))
        gap = gap_id if isinstance(gap_id, str) and gap_id.strip() else gap_id_from_clue(clue)
        should_close = close_gap if close_gap is not None else (clue["scene_id"] == "S10" and clue["conclusion"] == "normal")
        message: Optional[dict] = None
        with self.session_factory.begin() as db:
            existing = db.scalar(select(RiskClueRecord).where(RiskClueRecord.clue_id == clue["clue_id"]))
            if existing is not None:
                return existing.clue_id
            occurred = parse_utc(clue["occurred_at"])
            twin = self._cooldown_twin(db, clue, occurred, gap) if clue["conclusion"] == "suspected" else None
            row = {
                "clue_id": clue["clue_id"], "session_id": clue["session_id"], "source_id": clue["source_id"],
                "project_id": clue["project_id"], "room_id": clue["room_id"], "stage_snapshot_id": clue["stage_snapshot_id"],
                "phase_id": clue["phase_id"], "phase_version": clue["phase_version"], "binding_status": clue["binding_status"],
                "scene_id": clue["scene_id"], "rule_id": clue["rule_id"], "rule_version": clue["rule_version"],
                "conclusion": clue["conclusion"], "risk_level": clue["risk_level"], "occurred_at": occurred,
                "media_run_id": clue["media_run_id"], "asset_id": clue["asset_id"], "start_ms": clue["start_ms"],
                "end_ms": clue["end_ms"], "person_id": clue["person_id"], "track_id": clue["track_id"], "seat_id": clue["seat_id"],
                "gap_id": gap, "reason": clue["reason"], "observation_ids": list(clue["observation_ids"]),
                "missing_facts": list(clue["missing_facts"]), "quality_flags": list(clue["quality_flags"]),
                "evidence_refs": list(clue["evidence_refs"]), "clue": dict(clue),
                "event_id": twin.event_id if twin is not None else None,
                "deduped_from": twin.clue_id if twin is not None else None,
                "contract_version": clue["contract_version"], "created_at": utc_now(),
            }
            inserted = _insert_ignore(db, RiskClueRecord.__table__, row)
            if inserted == 0:
                return clue["clue_id"]             # 并发重放：别人先写了，本次什么都不建
            if clue["conclusion"] == "suspected" and twin is None:
                event = self._event_from(clue)
                db.add(event)
                db.flush()
                db.execute(update(RiskClueRecord).where(RiskClueRecord.clue_id == clue["clue_id"]).values(event_id=event.event_id))
                message = event_message(event)      # 提交前取值，提交后对象会过期
            if should_close and gap:
                self._close_gap_events(db, clue["session_id"], clue["source_id"], gap)
        if message is not None:
            self._publish(message)
        return clue["clue_id"]

    def persist_failure(self, observation: dict, *, scene_id: Optional[str], rule_id: Optional[str], code: str, message: str) -> int:
        with self.session_factory.begin() as db:
            row = ClueProcessingRecord(observation_id=observation["observation_id"], session_id=observation.get("session_id"),
                                       source_id=observation["source_id"], scene_id=scene_id, rule_id=rule_id, status="failed",
                                       error_code=code[:64], error_message=message[:512], occurred_at=parse_utc(observation["occurred_at"]))
            db.add(row)
            db.flush()
            return row.id

    def close_gap_events(self, session_id: Optional[str], source_id: str, gap_id: str) -> int:
        with self.session_factory.begin() as db:
            return self._close_gap_events(db, session_id, source_id, gap_id)

    # ---- 内部 ----
    def _cooldown_twin(self, db: Session, clue: dict, occurred: datetime, gap_id: Optional[str]) -> Optional[RiskClueRecord]:
        """同桶（场次 / 源 / 场景 / 规则 / 媒体轮次）、同主体（缺口 / 工位 / 轨迹 / 人员）、冷却窗内、**链首**（自己建了事件的）线索。"""
        if self.cooldown.total_seconds() == 0:
            return None
        subject = (gap_id, clue.get("seat_id"), clue.get("track_id"), clue.get("person_id"))
        rows = db.scalars(select(RiskClueRecord).where(
            RiskClueRecord.session_id == clue["session_id"], RiskClueRecord.source_id == clue["source_id"],
            RiskClueRecord.scene_id == clue["scene_id"], RiskClueRecord.rule_id == clue["rule_id"],
            RiskClueRecord.media_run_id == clue["media_run_id"], RiskClueRecord.conclusion == "suspected",
            RiskClueRecord.event_id.is_not(None), RiskClueRecord.deduped_from.is_(None),
            RiskClueRecord.occurred_at >= occurred - self.cooldown, RiskClueRecord.occurred_at <= occurred + self.cooldown,
        ).order_by(RiskClueRecord.occurred_at.desc())).all()
        for row in rows:
            if (row.gap_id, row.seat_id, row.track_id, row.person_id) != subject:
                continue
            if abs(_aware(row.occurred_at) - occurred) <= self.cooldown:
                return row
        return None

    @staticmethod
    def _event_from(clue: dict) -> Event:
        return Event(event_id=str(uuid.uuid4()), session_id=clue["session_id"], camera_id=clue["source_id"],
                     person_id=clue["person_id"], seat_id=clue["seat_id"], zone_id=None,
                     skill_code=clue["rule_id"], skill_version=clue["rule_version"],
                     model_name=MODEL_NAME, model_version=MODEL_VERSION, event_type=clue["rule_id"],
                     phase_id=clue["phase_id"], risk_level=clue["risk_level"], confidence=None, reason=clue["reason"][:4000],
                     occurred_at=_naive_utc(clue["occurred_at"]), status="PENDING")

    @staticmethod
    def _close_gap_events(db: Session, session_id: Optional[str], source_id: str, gap_id: str) -> int:
        linked = db.scalars(select(RiskClueRecord).where(
            RiskClueRecord.source_id == source_id, RiskClueRecord.gap_id == gap_id, RiskClueRecord.event_id.is_not(None),
            *( [RiskClueRecord.session_id == session_id] if session_id is not None else [] ))).all()
        event_ids = {row.event_id for row in linked}
        if not event_ids:
            return 0
        events = db.scalars(select(Event).where(Event.event_id.in_(event_ids), Event.status == "PENDING")).all()
        for event in events:
            event.status = "CLOSED"
            db.add(Decision(event_id=event.event_id, action="auto_close", reviewer_id="system", note=f"设备恢复关闭缺口 {gap_id}"))
        return len(events)

    def _publish(self, message: dict) -> None:
        if self.publisher is None:
            return
        try:
            self.publisher(message)
        except Exception as exc:  # noqa: BLE001  K-15：发布失败只记日志，事件已入库
            log.warning("事件通知发布失败: %s", type(exc).__name__)


def persist_clue(clue: dict, session_factory, *, gap_id: Optional[str] = None, close_gap: Optional[bool] = None, **kwargs) -> str:
    """契约签名 persist_clue(clue) -> clue_id 的便捷包装。"""
    return EventAdapter(session_factory, **kwargs).persist_clue(clue, gap_id=gap_id, close_gap=close_gap)


def process_observation(observation: dict, snapshot: Optional[dict], handlers: dict, adapter: EventAdapter, *,
                        context: Optional[dict] = None) -> list[str]:
    """10.1 接线用的标准顺序：seen → dispatch → persist_clue（带结构化 gap_id / 恢复标记）→ mark_processed。
    返回落库的 clue_id；处理失败记 clue_processing 并返回空列表。"""
    from service.stage_dispatcher import HandlerFailed, dispatch
    if adapter.seen(observation):
        return []
    try:
        clues = dispatch(observation, snapshot, handlers, context=context)
    except DispatchError as exc:
        adapter.persist_failure(observation, scene_id=None, rule_id=None, code=exc.code, message=str(exc))
        adapter.mark_processed(observation)
        return []
    except HandlerFailed as exc:
        adapter.persist_failure(observation, scene_id=exc.scene_id, rule_id=None, code="HANDLER_FAILED", message=str(exc))
        return []                                   # 规则失败不记收据：修好规则后重投可重算
    gap = gap_id_of(observation)
    recovery = observation.get("type") in RECOVERY_TYPES
    ids = [adapter.persist_clue(clue, gap_id=gap, close_gap=recovery or None) for clue in clues]
    adapter.mark_processed(observation)
    return ids


def read_group(client, stream: str, group: str, consumer: str, *, block_ms: int = 1000, count: int = 100,
               include_pending: bool = False) -> list[tuple[str, dict]]:
    """10.1 接线用：消费组读取（XREADGROUP），处理完由调用方 XACK。首次自动建组（MKSTREAM）。
    include_pending=True 时先读本消费者已投递未确认的条目（崩溃前读到的），再读新条目。"""
    try:
        client.xgroup_create(stream, group, id="$", mkstream=True)
    except Exception as exc:  # noqa: BLE001
        if "BUSYGROUP" not in str(exc):
            raise
    result = []
    ids = ["0", ">"] if include_pending else [">"]
    for start in ids:
        entries = client.xreadgroup(group, consumer, {stream: start}, count=count, block=block_ms if start == ">" else None)
        for _stream, items in entries or []:
            for message_id, data in items:
                decoded = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v) for k, v in data.items()}
                result.append((message_id.decode() if isinstance(message_id, bytes) else message_id, decoded))
    return result


def ack_failure_code(exc: BaseException) -> str:
    """把 HTTP 失败压成固定码（不含 URL / 凭据）。"""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP_{exc.code}"
    return type(exc).__name__
