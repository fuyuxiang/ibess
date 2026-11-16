"""执行卡 7.2：S01 通讯设备寄存规则。evaluate(observations, context) -> list[RiskClue]。

输入 7.1 的 visual_fact。设备看得清 + 有效豁免 → normal；看得清 + 身份核验 + 无豁免 → suspected
（本人已寄存同类设备、手里又见设备，按另有未寄存设备处理，D-26）；看不清 / 身份未核验 → insufficient。
复核失败（payload.status=failed）是处理状态，不出线索（D-25）。同一主体（源、轮次、持有人或无人时的位置格、种类）
同一结论只出一条，换媒体轮次清零。
身份只来自 6.2 核验事实（context.identities 或 track_seats + seat_identities），不从工位分配推断。
6.1 寄存记录与 6.3 豁免是桩结构（交接冻结）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from worker.absence_rules import Authorization, new_clue, parse_utc

RULE_ID = "s01-device-deposit"
INPUT_TYPES = frozenset({"visual_fact"})
DEVICE_KINDS = frozenset({"phone", "watch"})
UNCLEAR_FLAGS = frozenset({"visibility_unclear", "candidate_too_small"})


@dataclass
class DeviceRuleConfig:
    device_kinds: frozenset = DEVICE_KINDS


@dataclass(frozen=True)
class Deposit:
    """6.1 寄存记录桩结构：谁、什么设备、何时寄存、何时取回。"""
    fact_id: str
    fact_version: int
    person_id: str
    device_kind: str
    deposited_at: datetime
    returned_at: Optional[datetime]

    @classmethod
    def from_dict(cls, data: dict) -> "Deposit":
        if not isinstance(data, dict):
            raise ValueError("寄存记录必须是对象")
        for key in ("fact_id", "person_id", "device_kind"):
            if not isinstance(data.get(key), str) or not data[key].strip():
                raise ValueError(f"寄存记录缺 {key}")
        version = data.get("fact_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("fact_version 必须是正整数")
        deposited = parse_utc(data.get("deposited_at"))
        returned = parse_utc(data["returned_at"]) if data.get("returned_at") is not None else None
        if returned is not None and returned < deposited:
            raise ValueError("returned_at 早于 deposited_at")
        return cls(data["fact_id"], version, data["person_id"], data["device_kind"], deposited, returned)

    def holds_at(self, moment: datetime) -> bool:
        return self.deposited_at <= moment and (self.returned_at is None or moment < self.returned_at)


def resolve_identity(context: dict, holder_track_id: Optional[str]) -> Optional[dict]:
    """核验身份：identities[track] 直接给；或 track_seats[track] → seat_identities[seat]。都没有 → None。"""
    if not holder_track_id:
        return None
    direct = (context.get("identities") or {}).get(holder_track_id)
    if isinstance(direct, dict) and direct.get("person_id"):
        return {"person_id": direct["person_id"], "seat_id": (context.get("track_seats") or {}).get(holder_track_id), "fact_id": direct.get("fact_id")}
    seat_id = (context.get("track_seats") or {}).get(holder_track_id)
    seat_identity = (context.get("seat_identities") or {}).get(seat_id) if seat_id else None
    if isinstance(seat_identity, dict) and seat_identity.get("person_id"):
        return {"person_id": seat_identity["person_id"], "seat_id": seat_id, "fact_id": seat_identity.get("fact_id")}
    return {"person_id": None, "seat_id": seat_id, "fact_id": None} if seat_id else None


def _rule_version(context: dict, scene: str) -> str:
    version = context.get("rule_version") or ((context.get("snapshot") or {}).get("rule_versions") or {}).get(scene)
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"context 缺 {scene} 的 rule_version")
    return version


def _exemption(context: dict, *, person_id: Optional[str], seat_id: Optional[str], occurred_at: datetime, kind: str = "device_allowed") -> Optional[Authorization]:
    referenced = {(e.get("fact_id"), e.get("fact_version")) for e in (context.get("exceptions") or []) if isinstance(e, dict)}
    room_id = (context.get("snapshot") or {}).get("room_id")
    for item in context.get("authorizations") or []:
        auth = Authorization.from_dict(item)
        if (auth.fact_id, auth.fact_version) not in referenced or auth.kind != kind or not auth.active_at(occurred_at):
            continue
        scope = auth.scope
        if scope.get("room_id") == room_id and room_id:
            return auth
        if seat_id and scope.get("seat_id") == seat_id:
            return auth
        if person_id and scope.get("person_id") == person_id:
            return auth
    return None


def facts_of(observation: dict) -> tuple[Optional[dict], list[str]]:
    """返回 (facts, 不可用原因列表)。"""
    payload = observation.get("payload") or {}
    flags = set(observation.get("quality_flags") or [])
    if payload.get("status") != "ok" or not isinstance(payload.get("facts"), dict):
        return None, [f"视觉复核失败：{payload.get('error_code') or 'unknown'}"]
    facts = payload["facts"]
    reasons = []
    if facts.get("visibility") == "unclear" or flags & UNCLEAR_FLAGS:
        reasons.append("设备细节看不清（入口不可见、距离过远或过小）")
    return facts, reasons


class DeviceRules:
    input_types = INPUT_TYPES

    def __init__(self, config: Optional[DeviceRuleConfig] = None) -> None:
        self.config = config or DeviceRuleConfig()
        self._emitted: set[tuple] = set()          # (source, run, 主体, 种类, 结论)
        self._runs: dict[str, str] = {}

    def __call__(self, observations: list[dict], context: dict) -> list[dict]:
        return self.evaluate(observations, context)

    def reset(self, source_id: Optional[str] = None) -> None:
        self._emitted = {k for k in self._emitted if source_id is not None and k[0] != source_id}
        if source_id is None:
            self._runs.clear()
        else:
            self._runs.pop(source_id, None)

    def evaluate(self, observations: list[dict], context: dict) -> list[dict]:
        rule_version = _rule_version(context, "S01")
        clues = []
        for observation in observations:
            if not isinstance(observation, dict) or observation.get("type") != "visual_fact":
                continue
            source, run = observation.get("source_id"), observation.get("media_run_id")
            if self._runs.get(source) not in (None, run):
                self.reset(source)
            self._runs[source] = run
            for clue in self._one(observation, context, rule_version):
                key = (source, run, _subject(observation), _kind(observation), clue["conclusion"])
                if key in self._emitted:
                    continue                        # 同一主体同一结论已出过：持续可见不重复建人工复核任务
                self._emitted.add(key)
                clues.append(clue)
        return clues

    def _one(self, observation: dict, context: dict, rule_version: str) -> list[dict]:
        payload = observation.get("payload") or {}
        candidate = payload.get("candidate") or {}
        holder = candidate.get("holder_track_id")
        identity = resolve_identity(context, holder)
        person_id = identity["person_id"] if identity else None
        seat_id = identity["seat_id"] if identity else None
        common = dict(scene_id="S01", rule_id=RULE_ID, rule_version=rule_version, track_id=holder, seat_id=seat_id)
        if observation.get("binding_status") != "bound":
            return [new_clue(observation, conclusion="insufficient", risk_level="none", reason="场次 / 阶段未绑定，只保留技术事实",
                             missing_facts=["场次 / 阶段未绑定"], **common)]
        facts, unusable = facts_of(observation)
        if facts is None:
            return []                               # 复核失败是处理状态（D-25），由 7.1 的 failed 观察记录，规则不出线索
        kind = facts.get("object_kind")
        if kind not in self.config.device_kinds:
            if unusable:
                return [new_clue(observation, conclusion="insufficient", risk_level="none", reason="候选看不清，无法确认是否为通讯设备", missing_facts=unusable, **common)]
            return [new_clue(observation, conclusion="normal", risk_level="none", reason=f"可见物品为 {kind}，不是通讯设备", **common)]
        if unusable:
            return [new_clue(observation, conclusion="insufficient", risk_level="none", reason="疑似通讯设备但看不清，不定性", missing_facts=unusable, **common)]
        occurred = parse_utc(observation["occurred_at"])
        exemption = _exemption(context, person_id=person_id, seat_id=seat_id, occurred_at=occurred)
        if exemption is not None:
            return [new_clue(observation, conclusion="normal", risk_level="none", person_id=person_id,
                             reason=f"通讯设备（{kind}）可见，但有有效豁免：device_allowed {exemption.fact_id} v{exemption.fact_version}", **common)]
        if person_id is None:
            return [new_clue(observation, conclusion="insufficient", risk_level="none",
                             reason=f"通讯设备（{kind}）可见，但持有人身份未经核验绑定，无法核对寄存记录",
                             missing_facts=["持有人身份未经核验绑定", "寄存记录无法核对"], **common)]
        deposits = [Deposit.from_dict(d) for d in context.get("deposits") or []]
        mine = [d for d in deposits if d.person_id == person_id and d.holds_at(occurred)]
        same_kind = [d for d in mine if d.device_kind == kind]
        if same_kind:
            fact = same_kind[0]
            return [new_clue(observation, conclusion="suspected", risk_level="medium", person_id=person_id,
                             reason=f"本人已寄存 {kind}（{fact.fact_id} v{fact.fact_version}），手中仍可见同类设备，疑似另有未寄存设备", **common)]
        if mine:
            return [new_clue(observation, conclusion="suspected", risk_level="medium", person_id=person_id,
                             reason=f"可见通讯设备（{kind}）与本人寄存记录的种类（{', '.join(sorted({d.device_kind for d in mine}))}）不一致，疑似另有未寄存设备", **common)]
        return [new_clue(observation, conclusion="suspected", risk_level="medium", person_id=person_id,
                         reason=f"通讯设备（{kind}）可见，发生时刻无本人寄存记录、无豁免", **common)]


def _subject(observation: dict) -> str:
    candidate = (observation.get("payload") or {}).get("candidate") or {}
    if candidate.get("holder_track_id"):
        return candidate["holder_track_id"]
    bbox = candidate.get("bbox") or [0, 0, 0, 0]
    return f"cell:{round((bbox[0] + bbox[2]) * 5)}:{round((bbox[1] + bbox[3]) * 5)}"   # 无人持有：按框中心落在 0.1 网格的格子区分


def _kind(observation: dict) -> Optional[str]:
    payload = observation.get("payload") or {}
    return (payload.get("facts") or {}).get("object_kind") or (payload.get("candidate") or {}).get("kind")


_default = DeviceRules()


def evaluate(observations: list[dict], context: dict) -> list[dict]:
    return _default.evaluate(observations, context)


evaluate.input_types = INPUT_TYPES  # type: ignore[attr-defined]
