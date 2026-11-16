"""5.01：A 的扩展从 B 的库里只读取规则上下文（区域配置、阶段快照、工位身份、离岗许可）。

读的全是 B 已有的表：source_zone_configs（3.3）、phase_snapshots（2.1）、business_authorization_snapshots / entries（6.3）、
business_attendance_facts 的 leave_permit（6.1）。一律只读；读失败记日志并沿用上一份，从没读到过就是「无数据」——
无区域就不出区域类事实，无快照就是 unbound（规则只出证据不足），无许可就按无许可判。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Protocol

log = logging.getLogger("vision-worker.a_context")

LEAVE_KIND = "leave_approved"     # A 的 Authorization 桩里离岗许可的种类（absence_rules.AUTH_KINDS）


def _json(value, default):
    """PG 的 JSONB 读出来已是 Python 对象；其他驱动可能给 JSON 字符串。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def _when(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class RuleContext:
    """一个源当前可用的上下文。字段缺失就是 None / 空，规则据此出证据不足或不出。"""
    zone_config: Optional[dict] = None
    snapshot: Optional[dict] = None
    seat_identities: dict = field(default_factory=dict)
    authorizations: list = field(default_factory=list)
    exceptions: list = field(default_factory=list)
    loaded_at: float = 0.0
    identity_verification_enabled: bool = True  # Legacy unmanaged sessions keep their configured behavior.

    @property
    def scope(self) -> dict:
        """FrameContext 需要的场次与快照字段；没有快照时只填区域配置里的场次范围。"""
        snap = self.snapshot or {}
        zone = self.zone_config or {}
        return {
            "project_id": snap.get("project_id") or zone.get("project_id"),
            "room_id": snap.get("room_id") or zone.get("room_id"),
            "session_id": snap.get("session_id") or zone.get("session_id"),
            "stage_snapshot_id": snap.get("stage_snapshot_id"),
            "phase_id": snap.get("phase_id"),
            "phase_version": snap.get("phase_version"),
        }

    def rule_context(self, scene_id: str, fallback_version: str) -> dict:
        snap = self.snapshot or {}
        versions = snap.get("rule_versions") or {}
        return {
            "snapshot": snap, "scene_id": scene_id,
            "rule_version": versions.get(scene_id) or fallback_version,
            "exceptions": list(self.exceptions), "authorizations": list(self.authorizations),
            "seat_identities": dict(self.seat_identities), "room_id": self.scope["room_id"],
        }


class ContextSource(Protocol):
    def load(self, source_id: str) -> RuleContext: ...


def permits_to_authorizations(rows: list[dict], default_minutes: float) -> tuple[list[dict], list[dict]]:
    """B 的 leave_permit 行 → A 的 Authorization 字典与快照 exceptions 引用。

    B 的离岗许可没有结束时间字段：payload.valid_until 有就用，否则按登记时刻起 default_minutes 分钟有效（5.01 设计决策）。
    作用域只能一个维度：有工位按工位（不需要身份核验），否则按人员。
    """
    auths, refs = [], []
    for row in rows:
        start = _when(row.get("occurred_at"))
        if start is None:
            continue
        start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
        payload = _json(row.get("payload"), {})
        until = None
        raw_until = payload.get("valid_until") if isinstance(payload, dict) else None
        if isinstance(raw_until, str) and raw_until.strip():
            try:
                until = datetime.fromisoformat(raw_until.replace("Z", "+00:00"))
            except ValueError:
                until = None
        if until is None:
            until = start + timedelta(minutes=default_minutes)
        if row.get("seat_id"):
            scope = {"seat_id": str(row["seat_id"])}
        elif row.get("person_id"):
            scope = {"person_id": str(row["person_id"])}
        else:
            continue
        fact_id = str(row["fact_id"])
        auths.append({"fact_id": fact_id, "fact_version": 1, "kind": LEAVE_KIND, "scope": scope,
                      "valid_from": utc_iso(start), "valid_until": utc_iso(until)})
        refs.append({"fact_id": fact_id, "fact_version": 1})
    return auths, refs


class DbContextSource:
    """用 vision-worker 已有的 DATABASE_URL 只读 B 的表。engine 懒创建，连不上就返回空上下文。"""

    def __init__(self, database_url: str, *, permit_minutes: float = 30.0, engine_factory: Optional[Callable] = None) -> None:
        self.database_url = database_url
        self.permit_minutes = permit_minutes
        self._engine_factory = engine_factory
        self._engine = None

    def _connect(self):
        if self._engine is None:
            if self._engine_factory is not None:
                self._engine = self._engine_factory()
            else:
                from sqlalchemy import create_engine
                connect_args = {}
                if self.database_url.startswith("postgresql"):
                    from sqlalchemy.engine import make_url
                    original_options = make_url(self.database_url).query.get('options', '')
                    # 库卡住时不能拖住读取：连接与单条语句各限 2 秒（5.01 复核）
                    connect_args = {"connect_timeout": 2, "options": (original_options+' -c statement_timeout=2000').strip()}
                self._engine = create_engine(self.database_url, pool_pre_ping=True, pool_size=1, max_overflow=0, connect_args=connect_args)
        connection = self._engine.connect()
        if connection.dialect.name == 'sqlite':
            import re
            def channel(value):
                found = re.fullmatch(r'(?:camera-0*|cam)(\d+)', value or '')
                return 'cam'+str(int(found[1])) if found else value
            connection.connection.driver_connection.create_function('supervision_channel', 1, channel)
        return connection

    def load(self, source_id: str) -> RuleContext:
        from sqlalchemy import text
        ctx = RuleContext(loaded_at=time.time())
        with self._connect() as conn:
            # 快照只认「绑定仍有效、场次未关闭」的（5.01 复核：已结束场次的旧快照不能一直算 bound）
            snap = conn.execute(text(
                "SELECT s.stage_snapshot_id, s.session_id, s.project_id, s.room_id, s.source_id, s.phase_id, s.phase_version, "
                "s.enabled_scenes, s.rule_versions, s.exceptions FROM phase_snapshots s "
                "WHERE supervision_channel(s.source_id) = supervision_channel(:s) AND s.effective_at <= :now "
                "AND EXISTS (SELECT 1 FROM phase_bindings b WHERE b.session_id = s.session_id AND b.active) "
                "AND NOT EXISTS (SELECT 1 FROM eval_sessions e WHERE e.session_id = s.session_id AND e.status = 'closed') "
                "ORDER BY s.effective_at DESC, s.phase_version DESC LIMIT 1"),
                {"s": source_id, "now": datetime.now(timezone.utc)}).mappings().first()
            if conn.dialect.name == 'postgresql':
                # The resolver rejects ambiguous bindings and includes auxiliary cameras.
                resolved = _json(conn.execute(text('select supervision_scope(:source,:at,NULL)'),
                    {'source': source_id, 'at': datetime.now(timezone.utc)}).scalar(), {})
                if not resolved:
                    snap = None
                elif snap is None or snap['session_id'] != resolved['session_id']:
                    snap = conn.execute(text('SELECT * FROM phase_snapshots WHERE session_id=:sid AND effective_at<=:at ORDER BY effective_at DESC LIMIT 1'),
                                        {'sid': resolved['session_id'], 'at': datetime.now(timezone.utc)}).mappings().first()
            if snap:
                ctx.snapshot = {k: snap[k] for k in snap.keys()}
                ctx.snapshot["enabled_scenes"] = list(_json(snap["enabled_scenes"], []))
                ctx.snapshot["rule_versions"] = dict(_json(snap["rule_versions"], {}))
                ctx.snapshot["exceptions"] = list(_json(snap["exceptions"], []))
                ctx.snapshot['source_id'] = source_id
                configured = conn.execute(text(
                    "SELECT identity_verification_enabled FROM supervision_session_workflows WHERE session_id=:sid"),
                    {'sid': snap['session_id']}).first()
                ctx.identity_verification_enabled = bool(configured[0]) if configured else True
            # 有快照时区域配置必须是同一场次的；没有快照时取本源最新的一份（只用于 inside 等区域事实）
            zone_sql = ("SELECT session_id, project_id, room_id, source_id, config_version, zones, entry_lines FROM source_zone_configs "
                        "WHERE supervision_channel(source_id) = supervision_channel(:s)" + (" AND session_id = :sess" if snap else "") + " ORDER BY updated_at DESC LIMIT 1")
            zone = conn.execute(text(zone_sql), {"s": source_id, **({"sess": snap["session_id"]} if snap else {})}).mappings().first()
            if zone:
                ctx.zone_config = {k: zone[k] for k in ("session_id", "project_id", "room_id", "source_id", "config_version")}
                ctx.zone_config["zones"] = list(_json(zone["zones"], []))
                ctx.zone_config["entry_lines"] = list(_json(zone["entry_lines"], []))
            elif snap and conn.dialect.name == 'postgresql':
                legacy = conn.execute(text('SELECT zone_id,name,type,polygon FROM zones WHERE supervision_channel(camera_id)=supervision_channel(:s) AND polygon IS NOT NULL'), {'s': source_id}).mappings().all()
                if legacy:
                    ctx.zone_config = {**ctx.scope, 'source_id': source_id, 'config_version': 0, 'entry_lines': [],
                        'zones': [{'zone_id': str(z['zone_id']), 'seat_id': str(z['zone_id']) if (z['type'] or 'seat') == 'seat' else None,
                                   'name': z['name'], 'kind': z['type'] or 'seat', 'polygon': _json(z['polygon'], [])} for z in legacy]}
            room_id = ctx.scope["room_id"]
            if room_id:
                for row in conn.execute(text(
                        "SELECT e.seat_id, e.person_id, e.identity_verified FROM business_authorization_entries e "
                        "JOIN business_authorization_snapshots s ON s.snapshot_id = e.snapshot_id "
                        "WHERE s.room_id = :r AND s.status = 'active' AND e.seat_id IS NOT NULL"), {"r": room_id}).mappings():
                    if ctx.identity_verification_enabled and row["identity_verified"]:
                        ctx.seat_identities[str(row["seat_id"])] = {"person_id": str(row["person_id"]), "verified": True}
                permits = [dict(r) for r in conn.execute(text(
                    "SELECT fact_id, person_id, seat_id, occurred_at, payload FROM business_attendance_facts "
                    "WHERE room_id = :r AND fact_type = 'leave_permit' AND occurred_at >= :since"),
                    {"r": room_id, "since": datetime.now(timezone.utc) - timedelta(days=1)}).mappings()]
                ctx.authorizations, refs = permits_to_authorizations(permits, self.permit_minutes)
                # Explicit, immutable business facts referenced by this phase can grant scoped exceptions.
                referenced = [e['fact_id'] for e in (ctx.snapshot or {}).get('exceptions', [])
                              if e.get('fact_version') == 1]
                if referenced:
                    from sqlalchemy import bindparam
                    from worker.absence_rules import Authorization
                    query = text("SELECT fact_id,person_id,seat_id,occurred_at,payload FROM business_attendance_facts "
                                 "WHERE room_id=:room AND fact_type='clarification' AND fact_id IN :ids").bindparams(bindparam('ids', expanding=True))
                    for row in conn.execute(query, {'room': room_id, 'ids': referenced}).mappings():
                        payload = _json(row['payload'], {})
                        kind = payload.get('authorization_kind')
                        if kind not in ('contact_allowed','document_access','photo_allowed','device_allowed','break'):
                            continue
                        begin = _when(row['occurred_at'])
                        if begin is None:
                            continue
                        begin = begin if begin.tzinfo else begin.replace(tzinfo=timezone.utc)
                        until = _when(payload.get('valid_until'))
                        if payload.get('valid_until') and until is None:
                            continue
                        until = until or begin+timedelta(minutes=self.permit_minutes)
                        until = until if until.tzinfo else until.replace(tzinfo=timezone.utc)
                        scope = {'seat_id': str(row['seat_id'])} if row['seat_id'] else {'person_id': str(row['person_id'])} if row['person_id'] else {'room_id': room_id}
                        auth = {'fact_id': row['fact_id'], 'fact_version': 1, 'kind': kind, 'scope': scope,
                                'valid_from': utc_iso(begin), 'valid_until': utc_iso(until)}
                        Authorization.from_dict(auth)
                        ctx.authorizations.append(auth)
                # 快照里登记的例外 + 本室近一天的离岗许可：B 的快照不一定引用 6.1 许可，不并进来许可就永远用不上
                seen = {(e.get("fact_id"), e.get("fact_version")) for e in (ctx.snapshot or {}).get("exceptions", []) if isinstance(e, dict)}
                ctx.exceptions = [e for e in (ctx.snapshot or {}).get("exceptions", []) if isinstance(e, dict)]
                ctx.exceptions += [r for r in refs if (r["fact_id"], r["fact_version"]) not in seen]
        return ctx


class CachedContext:
    """按间隔刷新；刷新失败沿用上一份（首次失败就是空上下文），只在状态变化时记日志，不刷屏。

    background=True（生产用）时读库放在后台线程：get() 从不等库，库慢或卡住只会让上下文晚一点更新，不拖住 B 的逐帧循环（5.01 复核）。
    """

    def __init__(self, source: ContextSource, source_id: str, *, refresh_seconds: float = 30.0, clock: Callable[[], float] = time.monotonic,
                 background: bool = False) -> None:
        self.source = source
        self.source_id = source_id
        self.refresh_seconds = refresh_seconds
        self.clock = clock
        self.background = background
        self._current = RuleContext()
        self._next_at = 0.0
        self._failing = False
        self._loading = False
        self._lock = threading.Lock()

    def get(self) -> RuleContext:
        now = self.clock()
        if now >= self._next_at:
            self._next_at = now + self.refresh_seconds
            if not self.background:
                self._refresh()
            else:
                with self._lock:
                    start = not self._loading
                    self._loading = True
                if start:
                    threading.Thread(target=self._refresh, name="a-context-refresh", daemon=True).start()
        return self._current

    def _refresh(self) -> None:
        try:
            loaded = self.source.load(self.source_id)
            self._current = loaded          # 整体替换引用：读线程拿到的总是完整的一份
            if self._failing:
                log.info("A 扩展上下文恢复读取：%s", self.source_id)
            self._failing = False
        except Exception as exc:     # noqa: BLE001 — 读 B 的库失败不能影响主循环
            if not self._failing:
                log.warning("A 扩展读取上下文失败，沿用上一份：%s", str(exc).splitlines()[0][:200])
            self._failing = True
        finally:
            with self._lock:
                self._loading = False


__all__ = ["RuleContext", "ContextSource", "DbContextSource", "CachedContext", "permits_to_authorizations", "utc_iso"]
