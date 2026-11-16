"""执行卡 5.2：S03 离岗 / 越界 / 许可规则。evaluate(observations, context) -> list[RiskClue]。

离岗线索 = 可信的持续空置事实 × 发生时刻有效的许可：事实不可信（画面故障、采样中断）不判；
许可只有能绑定到工位、室或经 6.2 核验的身份才适用，绑不上是 insufficient 不是 normal。
规则不计时（5.1 的进度心跳已按媒体时间扣除故障），只比阈值；每个（源、轮次、主体）只出一条 suspected，
回位 / 离开时出一条 normal 补结束时间。person_id 只来自核验事实，永远不从 track_id 或工位分配推断。
6.1 / 6.3 未交付：授权事实与核验身份按本文件冻结的桩结构读（见 Authorization / seat_identities）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

CONTRACT_VERSION = "2.0.0"
RULE_VERSION_FALLBACK = "s03-v1"
RULE_SEAT = "s03-seat-absence"
RULE_ZONE = "s03-zone-dwell"
RULE_EXIT = "s03-exit-crossing"
INPUT_TYPES = frozenset({"seat_vacancy_progress", "seat_occupied", "zone_dwell_progress", "zone_left", "line_crossed"})
UNRELIABLE_FLAGS = frozenset({"camera_degraded", "camera_offline", "video_offline", "camera_blacked", "frame_error", "sampling_gap"})
AUTH_KINDS = ("leave_approved", "break", "zone_access", "device_allowed", "contact_allowed", "photo_allowed", "document_access")   # 5.2 / 7.2 / 7.3 共用的 6.3 桩种类
SCOPE_FIELDS = ("project_id", "room_id", "session_id")
SNAPSHOT_FIELDS = ("stage_snapshot_id", "phase_id", "phase_version")
UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")


def parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not UTC_PATTERN.match(value):
        raise ValueError("时间必须是 Z 结尾的 UTC ISO 字符串")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass
class AbsenceRuleConfig:
    """工程初值（执行卡:81）：工位空置 10 秒、区域越界 2 秒。"""
    vacancy_ms: int = 10000
    boundary_ms: int = 2000

    def __post_init__(self) -> None:
        if self.vacancy_ms <= 0 or self.boundary_ms <= 0:
            raise ValueError("阈值必须为正")


@dataclass(frozen=True)
class Authorization:
    """6.3 授权事实的桩结构（冻结在交接）：kind ∈ leave_approved / break / zone_access；scope 只能是一个维度。"""
    fact_id: str
    fact_version: int
    kind: str
    scope: dict
    valid_from: datetime
    valid_until: Optional[datetime]

    @classmethod
    def from_dict(cls, data: dict) -> "Authorization":
        if not isinstance(data, dict):
            raise ValueError("授权事实必须是对象")
        fact_id, version, kind, scope = data.get("fact_id"), data.get("fact_version"), data.get("kind"), data.get("scope")
        if not isinstance(fact_id, str) or not fact_id.strip():
            raise ValueError("授权事实缺 fact_id")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("fact_version 必须是正整数")
        if kind not in AUTH_KINDS:
            raise ValueError(f"未知授权类型 {kind}")
        if not isinstance(scope, dict) or not scope:
            raise ValueError("授权事实缺 scope")
        allowed = {"room_id", "seat_id", "person_id", "zone_id"}
        if set(scope) - allowed or any(not isinstance(v, str) or not v.strip() for v in scope.values()):
            raise ValueError("scope 只能含 room_id / seat_id / person_id / zone_id 且为非空字符串")
        if len(scope) != 1:
            raise ValueError("scope 只能是一个维度（多键会让个人许可绕过身份核验）")
        valid_from = parse_utc(data.get("valid_from"))
        until = data.get("valid_until")
        valid_until = parse_utc(until) if until is not None else None
        if valid_until is not None and valid_until < valid_from:
            raise ValueError("valid_until 早于 valid_from")
        return cls(fact_id, version, kind, dict(scope), valid_from, valid_until)

    def active_at(self, moment: datetime) -> bool:
        return self.valid_from <= moment and (self.valid_until is None or moment <= self.valid_until)


# ---------------------------------------------------------------- 线索构造（与 2.3 的 new_clue 同款，K-19）
def new_clue(observation: dict, *, scene_id: str, rule_id: str, rule_version: str, conclusion: str, risk_level: str,
             reason: str, missing_facts: Iterable[str] = (), quality_flags: Iterable[str] = (), person_id: Optional[str] = None,
             track_id: Optional[str] = None, seat_id: Optional[str] = None, clue_id: Optional[str] = None,
             start_ms: Optional[int] = None, end_ms: Optional[int] = None, observation_ids: Optional[list] = None) -> dict:
    bound = observation.get("binding_status") == "bound"
    flags = list(dict.fromkeys([*observation.get("quality_flags", []), *quality_flags]))
    if not bound and "binding_missing" not in flags:
        flags.append("binding_missing")
    if not bound:
        conclusion, risk_level, person_id = "insufficient", "none", None
    facts = list(dict.fromkeys(missing_facts))
    if conclusion == "insufficient" and not facts:
        facts = ["上下文不完整"]
    return {
        "contract_version": CONTRACT_VERSION,
        "project_id": observation["project_id"], "room_id": observation["room_id"], "session_id": observation["session_id"],
        "source_id": observation["source_id"],
        "stage_snapshot_id": observation["stage_snapshot_id"] if bound else None,
        "phase_id": observation["phase_id"] if bound else None,
        "phase_version": observation["phase_version"] if bound else None,
        "clue_id": clue_id or f"clue:{scene_id}:{rule_id}:{observation['observation_id']}",
        "binding_status": "bound" if bound else "unbound",
        "scene_id": scene_id, "rule_id": rule_id, "rule_version": rule_version,
        "occurred_at": observation["occurred_at"], "media_run_id": observation["media_run_id"], "asset_id": observation["asset_id"],
        "start_ms": observation["start_ms"] if start_ms is None else start_ms,
        "end_ms": observation["end_ms"] if end_ms is None else end_ms,
        "person_id": person_id, "track_id": track_id, "seat_id": seat_id,
        "conclusion": conclusion, "risk_level": risk_level, "reason": reason,
        "observation_ids": list(observation_ids) if observation_ids else [observation["observation_id"]], "missing_facts": facts, "quality_flags": flags,
        "evidence_refs": list(observation.get("evidence_refs", [])),
    }


# ---------------------------------------------------------------- 许可判定
@dataclass(frozen=True)
class Permission:
    status: str                      # allowed / none / missing_identity / missing_fact
    fact: Optional[Authorization] = None
    person_id: Optional[str] = None
    missing: tuple = ()


def _authorizations(context: dict) -> tuple[list[Authorization], list[str]]:
    """只认快照 exceptions 引用的（fact_id, fact_version）；引用了但 context 没给内容的记为缺失。"""
    referenced = {(e.get("fact_id"), e.get("fact_version")) for e in (context.get("exceptions") or []) if isinstance(e, dict)}
    provided: dict[tuple, Authorization] = {}
    for item in context.get("authorizations") or []:
        auth = Authorization.from_dict(item)
        provided[(auth.fact_id, auth.fact_version)] = auth
    usable = [provided[key] for key in referenced if key in provided]
    missing = [f"例外事实 {fid} v{ver} 内容未提供" for fid, ver in sorted(referenced, key=str) if (fid, ver) not in provided]
    return usable, missing


def resolve_permission(context: dict, *, kinds: Iterable[str], seat_id: Optional[str], zone_id: Optional[str],
                       occurred_at: datetime, person_id: Optional[str] = None) -> Permission:
    """person_id 可由调用方传入（7.x 的核验身份）；不传则从 seat_identities 取。"""
    auths, missing = _authorizations(context)
    if missing:
        return Permission("missing_fact", missing=tuple(missing))
    snapshot = context.get("snapshot") or {}
    room_id = snapshot.get("room_id") or context.get("room_id")
    if person_id is None:
        identities = context.get("seat_identities") or {}
        identity = identities.get(seat_id) if seat_id else None
        person_id = identity.get("person_id") if isinstance(identity, dict) else None
    wanted = set(kinds)
    unresolved: list[str] = []
    for auth in auths:
        if auth.kind not in wanted or not auth.active_at(occurred_at):
            continue
        scope = auth.scope
        if "room_id" in scope and scope["room_id"] == room_id:
            return Permission("allowed", auth, person_id)
        if "seat_id" in scope:
            if seat_id is None:
                unresolved.append("存在工位级离岗许可，但本事实无法绑定到工位，无法确认当事人是否为被许可人")
            elif scope["seat_id"] == seat_id:
                return Permission("allowed", auth, person_id)
        if "zone_id" in scope and zone_id and scope["zone_id"] == zone_id:
            return Permission("allowed", auth, person_id)
        if "person_id" in scope:
            if person_id is None:
                unresolved.append("存在个人离岗许可，但人员身份未经核验绑定，不自动套用")
            elif scope["person_id"] == person_id:
                return Permission("allowed", auth, person_id)
    if unresolved:
        return Permission("missing_identity", person_id=person_id, missing=tuple(dict.fromkeys(unresolved)))
    return Permission("none", person_id=person_id)


# ---------------------------------------------------------------- 规则
class AbsenceRules:
    """可调用对象：evaluate(observations, context)。按（源、媒体轮次、主体）记已出的 suspected，不刷屏。"""

    input_types = INPUT_TYPES

    def __init__(self, config: Optional[AbsenceRuleConfig] = None) -> None:
        self.config = config or AbsenceRuleConfig()
        self._open: dict[tuple, dict] = {}      # (source, run, subject) -> {"clue_id", "since_ms"}
        self._runs: dict[str, str] = {}         # source -> 当前媒体轮次

    def __call__(self, observations: list[dict], context: dict) -> list[dict]:
        return self.evaluate(observations, context)

    def evaluate(self, observations: list[dict], context: dict) -> list[dict]:
        config = context.get("config")
        config = config if isinstance(config, AbsenceRuleConfig) else self.config     # 派发器把同一 context 交给各场景
        rule_version = context.get("rule_version") or ((context.get("snapshot") or {}).get("rule_versions") or {}).get("S03")
        if not isinstance(rule_version, str) or not rule_version.strip():
            raise ValueError("context 缺 S03 的 rule_version（须等于快照 rule_versions['S03']）")
        clues: list[dict] = []
        for observation in observations:
            if not isinstance(observation, dict) or observation.get("type") not in INPUT_TYPES:
                continue
            self._track_run(observation)
            if observation.get("binding_status") != "bound":
                payload = observation.get("payload") or {}
                subject = payload.get("seat_id") or payload.get("zone_id") or payload.get("line_id") or "all"
                key = (observation["source_id"], observation["media_run_id"], f"unbound:{subject}")
                if key in self._open:
                    continue
                self._open[key] = {"clue_id": None, "since_ms": observation["start_ms"], "conclusion": "insufficient"}
                clues.append(new_clue(observation, scene_id="S03", rule_id=self._rule_for(observation), rule_version=rule_version,
                                      conclusion="insufficient", risk_level="none", reason="场次 / 阶段未绑定，只保留技术事实",
                                      missing_facts=["场次 / 阶段未绑定"]))
                continue
            if set(observation.get("quality_flags", [])) & UNRELIABLE_FLAGS:
                continue                      # 画面不可信：不判，不把故障时段算成离岗
            kind = observation["type"]
            if kind in ("seat_vacancy_progress", "seat_occupied"):
                clues.extend(self._seat(observation, context, config, rule_version))
            elif kind in ("zone_dwell_progress", "zone_left"):
                clues.extend(self._zone(observation, context, config, rule_version))
            elif kind == "line_crossed":
                clues.extend(self._line(observation, context, rule_version))
        return clues

    def reset(self, source_id: Optional[str] = None) -> None:
        for key in [k for k in self._open if source_id is None or k[0] == source_id]:
            del self._open[key]
        if source_id is None:
            self._runs.clear()
        else:
            self._runs.pop(source_id, None)

    # ---- 内部 ----
    @staticmethod
    def _rule_for(observation: dict) -> str:
        kind = observation.get("type")
        return RULE_SEAT if kind in ("seat_vacancy_progress", "seat_occupied") else RULE_EXIT if kind == "line_crossed" else RULE_ZONE

    def _track_run(self, observation: dict) -> None:
        source, run = observation.get("source_id"), observation.get("media_run_id")
        if self._runs.get(source) not in (None, run):
            self.reset(source)              # 新媒体轮次：清掉本源的已出线索状态
        self._runs[source] = run

    def _seat(self, observation: dict, context: dict, config: AbsenceRuleConfig, rule_version: str) -> list[dict]:
        payload = observation.get("payload") or {}
        seat_id = payload.get("seat_id")
        if not isinstance(seat_id, str) or not seat_id:
            return []
        key = (observation["source_id"], observation["media_run_id"], f"seat:{seat_id}")
        occurred = parse_utc(observation["occurred_at"])
        if observation["type"] == "seat_occupied":
            vacant_ms = int(payload.get("vacant_ms") or 0)
            opened = self._open.pop(key, None)
            start = opened["since_ms"] if opened else observation["start_ms"]
            if opened:
                reason = f"工位恢复占用，空置结束：共 {vacant_ms} ms（此前已出 {opened['conclusion']} 线索 {opened['clue_id']}）"
            else:
                reason = f"工位由空置转为占用：空置 {vacant_ms} ms，未达阈值 {config.vacancy_ms} ms"
            return [new_clue(observation, scene_id="S03", rule_id=RULE_SEAT, rule_version=rule_version, conclusion="normal",
                             risk_level="none", reason=reason, seat_id=seat_id, start_ms=start, end_ms=observation["end_ms"],
                             clue_id=f"clue:S03:{RULE_SEAT}:{observation['source_id']}:{observation['media_run_id']}:seat:{seat_id}:return:{observation['end_ms']}")]
        vacant_ms = int(payload.get("vacant_ms") or 0)
        if vacant_ms < config.vacancy_ms:
            return []
        since_ms = int(payload.get("vacant_since_ms", observation["start_ms"]))
        permission = resolve_permission(context, kinds=("leave_approved", "break"), seat_id=seat_id, zone_id=None, occurred_at=occurred)
        clue_id = f"clue:S03:{RULE_SEAT}:{observation['source_id']}:{observation['media_run_id']}:seat:{seat_id}:{since_ms}:{permission.status}"
        opened = self._open.get(key)
        if opened is not None and opened.get("permission") == permission.status:
            return []                                   # 同一段空置、同一结论只出一条（任何结论都如此）
        clue = self._decide(observation, permission, rule_id=RULE_SEAT, rule_version=rule_version, seat_id=seat_id, track_id=None,
                            clue_id=clue_id, start_ms=since_ms,
                            suspected_reason=f"工位空置 {vacant_ms} ms，超过阈值 {config.vacancy_ms} ms，且发生时刻没有适用的离岗许可",
                            normal_reason=f"工位空置 {vacant_ms} ms，但发生时刻有有效许可")
        self._open[key] = {"clue_id": clue_id, "since_ms": since_ms, "conclusion": clue["conclusion"], "permission": permission.status}
        return [clue]

    def _zone(self, observation: dict, context: dict, config: AbsenceRuleConfig, rule_version: str) -> list[dict]:
        payload = observation.get("payload") or {}
        zone_id, track_id = payload.get("zone_id"), payload.get("track_id")
        if not isinstance(zone_id, str) or not isinstance(track_id, str):
            return []
        key = (observation["source_id"], observation["media_run_id"], f"zone:{zone_id}:{track_id}")
        occurred = parse_utc(observation["occurred_at"])
        dwell_ms = int(payload.get("dwell_ms") or 0)
        if observation["type"] == "zone_left":
            opened = self._open.pop(key, None)
            if opened is None:
                return []
            lost = "track_lost" in (observation.get("quality_flags") or [])
            reason = (f"轨迹在限制区内丢失，停留按最后可见时刻结束：共 {dwell_ms} ms（此前已出 {opened['conclusion']} 线索 {opened['clue_id']}）" if lost
                      else f"离开限制区，停留结束：共 {dwell_ms} ms（此前已出 {opened['conclusion']} 线索 {opened['clue_id']}）")
            return [new_clue(observation, scene_id="S03", rule_id=RULE_ZONE, rule_version=rule_version, conclusion="normal",
                             risk_level="none", reason=reason, track_id=track_id, start_ms=opened["since_ms"], end_ms=observation["end_ms"],
                             clue_id=f"{opened['clue_id']}:left:{observation['end_ms']}")]
        if dwell_ms < config.boundary_ms:
            return []
        since_ms = observation["start_ms"]
        permission = resolve_permission(context, kinds=("zone_access", "leave_approved"), seat_id=None, zone_id=zone_id, occurred_at=occurred)
        clue_id = f"clue:S03:{RULE_ZONE}:{observation['source_id']}:{observation['media_run_id']}:zone:{zone_id}:{track_id}:{since_ms}:{permission.status}"
        opened = self._open.get(key)
        if opened is not None and opened.get("permission") == permission.status:
            return []
        clue = self._decide(observation, permission, rule_id=RULE_ZONE, rule_version=rule_version, seat_id=None, track_id=track_id,
                            clue_id=clue_id, start_ms=since_ms,
                            suspected_reason=f"轨迹在限制区停留 {dwell_ms} ms，超过阈值 {config.boundary_ms} ms，且无适用的区域许可",
                            normal_reason=f"轨迹在限制区停留 {dwell_ms} ms，但有有效的区域许可")
        self._open[key] = {"clue_id": clue_id, "since_ms": since_ms, "conclusion": clue["conclusion"], "permission": permission.status}
        return [clue]

    def _line(self, observation: dict, context: dict, rule_version: str) -> list[dict]:
        payload = observation.get("payload") or {}
        if payload.get("direction") != "out":
            return []
        line_id, track_id = payload.get("line_id"), payload.get("track_id")
        if not isinstance(line_id, str) or not isinstance(track_id, str):
            return []
        occurred = parse_utc(observation["occurred_at"])
        clue_id = f"clue:S03:{RULE_EXIT}:{observation['source_id']}:{observation['media_run_id']}:line:{line_id}:{track_id}:{observation['end_ms']}"
        permission = resolve_permission(context, kinds=("leave_approved", "break"), seat_id=None, zone_id=None, occurred_at=occurred)
        return [self._decide(observation, permission, rule_id=RULE_EXIT, rule_version=rule_version, seat_id=None, track_id=track_id,
                             clue_id=clue_id, start_ms=observation["start_ms"],
                             suspected_reason=f"轨迹跨出出入口线 {line_id}，发生时刻没有适用的离岗许可",
                             normal_reason=f"轨迹跨出出入口线 {line_id}，但发生时刻有有效许可")]

    @staticmethod
    def _decide(observation: dict, permission: Permission, *, rule_id: str, rule_version: str, seat_id: Optional[str],
                track_id: Optional[str], clue_id: str, start_ms: int, suspected_reason: str, normal_reason: str) -> dict:
        common = dict(scene_id="S03", rule_id=rule_id, rule_version=rule_version, seat_id=seat_id, track_id=track_id,
                      clue_id=clue_id, start_ms=start_ms, end_ms=observation["end_ms"])
        if permission.status == "allowed":
            fact = permission.fact
            return new_clue(observation, conclusion="normal", risk_level="none", person_id=permission.person_id,
                            reason=f"{normal_reason}：{fact.kind} {fact.fact_id} v{fact.fact_version}", **common)
        if permission.status == "missing_fact":
            return new_clue(observation, conclusion="insufficient", risk_level="none", missing_facts=permission.missing,
                            reason="快照引用的例外事实内容未提供，无法判断是否有许可", **common)
        if permission.status == "missing_identity":
            return new_clue(observation, conclusion="insufficient", risk_level="none", missing_facts=permission.missing,
                            reason="存在离岗许可但无法绑定到当事人（工位 / 核验身份），不自动套用", **common)
        return new_clue(observation, conclusion="suspected", risk_level="medium", person_id=permission.person_id,
                        reason=suspected_reason, **common)


_default = AbsenceRules()


def evaluate(observations: list[dict], context: dict) -> list[dict]:
    """契约签名：模块级默认实例（一个进程一份状态）。离线 / 多源请各自 `AbsenceRules()`。"""
    return _default.evaluate(observations, context)


evaluate.input_types = INPUT_TYPES  # type: ignore[attr-defined]
