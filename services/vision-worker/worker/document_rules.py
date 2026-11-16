"""执行卡 7.3：S08 疑似拍摄 / 携离资料。evaluate(observations, context) -> list[RiskClue]。

拍摄：7.1 的 visual_fact（phone + activity=photographing、看得清）+ 6.3 拍摄许可 → 三态。
携离：纸张候选持有人（visual_fact object_kind=paper）在窗口内跨出出入口（5.1 line_crossed out）+ 6.1 交接记录 → 三态。
纸张候选不能证明是受控资料：携离最高 low，reason 写明需人工核对原片；看不清 → insufficient。
复核失败是处理状态，不出线索（契约「禁止伪装normal或insufficient」，与 D-25 同一口径）。画面故障标记的观察不判。
同一持有人同一结论的拍摄只出一条（换媒体轮次清零）；一次纸张记忆只对应一次携离判定。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from worker.absence_rules import new_clue, parse_utc
from worker.device_rules import _exemption, _rule_version, facts_of, resolve_identity

RULE_PHOTO = "s08-photographing"
RULE_CARRY = "s08-document-carry"
INPUT_TYPES = frozenset({"visual_fact", "line_crossed"})
UNRELIABLE_FLAGS = frozenset({"camera_degraded", "camera_offline", "video_offline", "camera_blacked", "frame_error", "sampling_gap"})
CARRY_NOTE = "纸张候选不能证明是受控资料，需人工核对原片"


@dataclass
class DocumentRuleConfig:
    carry_window_ms: int = 15000          # 纸张候选可见到跨出的最长间隔

    def __post_init__(self) -> None:
        if self.carry_window_ms <= 0:
            raise ValueError("carry_window_ms 必须为正")


@dataclass(frozen=True)
class Handover:
    """6.1 交接记录桩结构：谁、哪份资料、何时交出、何时归还。"""
    fact_id: str
    fact_version: int
    person_id: str
    document_id: str
    handed_at: datetime
    returned_at: Optional[datetime]

    @classmethod
    def from_dict(cls, data: dict) -> "Handover":
        if not isinstance(data, dict):
            raise ValueError("交接记录必须是对象")
        for key in ("fact_id", "person_id", "document_id"):
            if not isinstance(data.get(key), str) or not data[key].strip():
                raise ValueError(f"交接记录缺 {key}")
        version = data.get("fact_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("fact_version 必须是正整数")
        handed = parse_utc(data.get("handed_at"))
        returned = parse_utc(data["returned_at"]) if data.get("returned_at") is not None else None
        if returned is not None and returned < handed:
            raise ValueError("returned_at 早于 handed_at")
        return cls(data["fact_id"], version, data["person_id"], data["document_id"], handed, returned)

    def active_at(self, moment: datetime) -> bool:
        return self.handed_at <= moment and (self.returned_at is None or moment < self.returned_at)


class DocumentRules:
    input_types = INPUT_TYPES

    def __init__(self, config: Optional[DocumentRuleConfig] = None) -> None:
        self.config = config or DocumentRuleConfig()
        self._paper: dict[tuple, dict] = {}       # (source, run, holder) -> {"last_ms", "observation_id", "unclear"}
        self._open: dict[tuple, dict] = {}        # 已出线索（拍摄按观察、携离按跨线）
        self._runs: dict[str, str] = {}

    def __call__(self, observations: list[dict], context: dict) -> list[dict]:
        return self.evaluate(observations, context)

    def reset(self, source_id: Optional[str] = None) -> None:
        for store in (self._paper, self._open):
            for key in [k for k in store if source_id is None or k[0] == source_id]:
                del store[key]
        if source_id is None:
            self._runs.clear()
        else:
            self._runs.pop(source_id, None)

    def evaluate(self, observations: list[dict], context: dict) -> list[dict]:
        config = context.get("config")
        config = config if isinstance(config, DocumentRuleConfig) else self.config     # 派发器把同一 context 交给各场景
        rule_version = _rule_version(context, "S08")
        clues: list[dict] = []
        for observation in observations:
            if not isinstance(observation, dict) or observation.get("type") not in INPUT_TYPES:
                continue
            self._track_run(observation)
            if observation.get("binding_status") != "bound":
                key = (observation["source_id"], observation["media_run_id"], f"unbound:{observation['type']}")
                if key in self._open:
                    continue
                self._open[key] = {"conclusion": "insufficient"}
                clues.append(new_clue(observation, scene_id="S08", rule_id=RULE_PHOTO if observation["type"] == "visual_fact" else RULE_CARRY,
                                      rule_version=rule_version, conclusion="insufficient", risk_level="none",
                                      reason="场次 / 阶段未绑定，只保留技术事实", missing_facts=["场次 / 阶段未绑定"]))
                continue
            if set(observation.get("quality_flags") or []) & UNRELIABLE_FLAGS:
                continue                                   # 画面故障时的跨线 / 复核不可靠，不判
            if observation["type"] == "visual_fact":
                found = self._visual(observation, context, rule_version)
            else:
                found = self._crossing(observation, context, config, rule_version)
            for clue in found:
                key = (observation["source_id"], observation["media_run_id"], f"{clue['rule_id']}:{clue['track_id'] or clue['clue_id']}:{clue['conclusion']}")
                if clue["rule_id"] == RULE_PHOTO and key in self._open:
                    continue                               # 同一持有人连续多帧拍摄姿态：同一结论只出一条
                self._open[key] = {"conclusion": clue["conclusion"]}
                clues.append(clue)
        return clues

    def _track_run(self, observation: dict) -> None:
        source, run = observation.get("source_id"), observation.get("media_run_id")
        if self._runs.get(source) not in (None, run):
            self.reset(source)
        self._runs[source] = run

    def _visual(self, observation: dict, context: dict, rule_version: str) -> list[dict]:
        payload = observation.get("payload") or {}
        candidate = payload.get("candidate") or {}
        holder = candidate.get("holder_track_id")
        identity = resolve_identity(context, holder)
        person_id = identity["person_id"] if identity else None
        seat_id = identity["seat_id"] if identity else None
        key = (observation["source_id"], observation["media_run_id"], holder or "nobody")
        facts, unusable = facts_of(observation)
        common = dict(scene_id="S08", rule_id=RULE_PHOTO, rule_version=rule_version, track_id=holder, seat_id=seat_id)
        if facts is None:
            return []                                      # 复核失败是处理状态，由 7.1 的 failed 观察记录，规则不出线索
        kind = facts.get("object_kind")
        if kind == "paper":
            if not unusable and facts.get('holding') != 'in_hand':
                self._paper.pop(key, None)
                return []  # a document left on the desk is not carried out by its reader
            self._paper[key] = {"last_ms": int(observation["start_ms"]), "observation_id": observation["observation_id"], "unclear": bool(unusable)}
            return []                                      # 低头看纸 / 书写是正常事实，不出线索；只记住「此人手里有纸」
        if kind == "phone" and facts.get("activity") == "photographing":
            if unusable:
                return [new_clue(observation, conclusion="insufficient", risk_level="none", missing_facts=unusable,
                                 reason="疑似举起设备拍摄但细节看不清，不定性", **common)]
            occurred = parse_utc(observation["occurred_at"])
            exemption = _exemption(context, person_id=person_id, seat_id=seat_id, occurred_at=occurred, kind="photo_allowed")
            if exemption is not None:
                return [new_clue(observation, conclusion="normal", risk_level="none", person_id=person_id,
                                 reason=f"可见拍摄姿态，但有有效拍摄许可 photo_allowed {exemption.fact_id} v{exemption.fact_version}", **common)]
            referenced = {(e.get("fact_id"), e.get("fact_version")) for e in (context.get("exceptions") or []) if isinstance(e, dict)}
            personal_only = person_id is None and any(
                isinstance(a, dict) and a.get("kind") == "photo_allowed" and "person_id" in (a.get("scope") or {})
                and (a.get("fact_id"), a.get("fact_version")) in referenced for a in context.get("authorizations") or [])
            if personal_only:
                return [new_clue(observation, conclusion="insufficient", risk_level="none",
                                 missing_facts=["存在个人拍摄许可，但持有人身份未经核验绑定，不自动套用"],
                                 reason="可见拍摄姿态，存在个人拍摄许可但无法绑定到当事人", **common)]
            return [new_clue(observation, conclusion="suspected", risk_level="medium", person_id=person_id,
                             reason="可见举起设备对着资料或他人的拍摄姿态，发生时刻无拍摄许可；引用原片，不推断所摄内容", **common)]
        if kind == "screen" and unusable:
            return [new_clue(observation, conclusion="insufficient", risk_level="none", missing_facts=unusable,
                             reason="屏幕候选看不清，不能判断是否展示或录制资料", **common)]
        return []                                          # 普通拿手机、杯子、看不清的其他物品：S08 不出线索

    def _crossing(self, observation: dict, context: dict, config: DocumentRuleConfig, rule_version: str) -> list[dict]:
        payload = observation.get("payload") or {}
        if payload.get("direction") != "out":
            return []
        track_id, line_id = payload.get("track_id"), payload.get("line_id")
        if not isinstance(track_id, str) or not isinstance(line_id, str):
            return []
        key = (observation["source_id"], observation["media_run_id"], track_id)
        paper = self._paper.get(key)
        if paper is None or int(observation["start_ms"]) - paper["last_ms"] > config.carry_window_ms:
            return []                                      # 跨出前没见到纸张候选：不是本规则的事
        del self._paper[key]                               # 这次纸张记忆已用于判定；再次跨出要重新见到纸张
        identity = resolve_identity(context, track_id)
        person_id = identity["person_id"] if identity else None
        seat_id = identity["seat_id"] if identity else None
        clue_id = f"clue:S08:{RULE_CARRY}:{observation['source_id']}:{observation['media_run_id']}:{track_id}:{observation['end_ms']}"
        common = dict(scene_id="S08", rule_id=RULE_CARRY, rule_version=rule_version, track_id=track_id, seat_id=seat_id, clue_id=clue_id,
                      start_ms=paper["last_ms"], observation_ids=[paper["observation_id"], observation["observation_id"]])
        if paper["unclear"]:
            return [new_clue(observation, conclusion="insufficient", risk_level="none", missing_facts=["纸张候选细节看不清（近景缺失）"],
                             reason="持有纸张候选的轨迹跨出出入口，但候选看不清，不定性", **common)]
        if person_id is None:
            return [new_clue(observation, conclusion="insufficient", risk_level="none",
                             missing_facts=["持有人身份未经核验绑定", "交接记录无法核对"],
                             reason=f"持有纸张候选的轨迹跨出出入口 {line_id}，但身份未核验，无法核对交接记录；{CARRY_NOTE}", **common)]
        occurred = parse_utc(observation["occurred_at"])
        handovers = [Handover.from_dict(h) for h in context.get("handovers") or []]
        mine = [h for h in handovers if h.person_id == person_id and h.active_at(occurred)]
        if mine:
            fact = mine[0]
            return [new_clue(observation, conclusion="normal", risk_level="none", person_id=person_id,
                             reason=f"持有纸张候选跨出出入口，但有有效资料交接记录 {fact.fact_id} v{fact.fact_version}（{fact.document_id}）", **common)]
        access = _exemption(context, person_id=person_id, seat_id=seat_id, occurred_at=occurred, kind="document_access")
        if access is not None:
            return [new_clue(observation, conclusion="normal", risk_level="none", person_id=person_id,
                             reason=f"持有纸张候选跨出出入口，但有资料携带权限 document_access {access.fact_id} v{access.fact_version}", **common)]
        return [new_clue(observation, conclusion="suspected", risk_level="low", person_id=person_id,
                         reason=f"持有纸张候选的已核验人员跨出出入口 {line_id}，无交接记录与携带权限；{CARRY_NOTE}", **common)]


_default = DocumentRules()


def evaluate(observations: list[dict], context: dict) -> list[dict]:
    return _default.evaluate(observations, context)


evaluate.input_types = INPUT_TYPES  # type: ignore[attr-defined]
