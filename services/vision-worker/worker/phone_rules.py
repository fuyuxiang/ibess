"""执行卡 7.2：S02 评审期间疑似使用通讯设备。evaluate(observations, context) -> list[RiskClue]。

输入 7.1 的 visual_fact，只看手机候选（纸张等其他候选与 S02 无关，不打断也不计数）。正例 = 手机在手 / 贴耳且在操作 / 通话 / 拍摄，
且 visibility=clear（partial 是「物品能认、动作不确定」，不算正例）；同持有人连续 ≥ 2 次复核才出 suspected（贴耳 / 通话 high，其余 medium），
同段只出一条，段内后来出现贴耳 / 通话时再补一条 high；同一观察重复投递不重复计数；豁免 → normal；看不清 → insufficient。
复核失败（payload.status=failed）是处理状态，不出线索、不打断计数（D-25，契约「禁止伪装normal或insufficient」）。
不判断电话是否接通、对方是谁——这是看不见的事，reason 里明说。person_id 只来自核验事实。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from worker.absence_rules import new_clue, parse_utc
from worker.device_rules import _exemption, _rule_version, facts_of, resolve_identity

RULE_ID = "s02-device-use"
INPUT_TYPES = frozenset({"visual_fact"})
LIMITATION = "不能判断是否接通及对方身份"


@dataclass
class PhoneRuleConfig:
    min_consecutive: int = 2          # 连续多少次复核为正例才出线索（7.1 去重 5 秒 → 约 10 秒）
    window_ms: int = 20000            # 相邻正例间隔超过此值视为中断
    positive_holdings: frozenset = frozenset({"in_hand", "at_ear"})
    positive_activities: frozenset = frozenset({"operating_device", "phone_call", "photographing"})
    high_holdings: frozenset = frozenset({"at_ear"})
    high_activities: frozenset = frozenset({"phone_call"})

    def __post_init__(self) -> None:
        if self.min_consecutive < 1 or self.window_ms <= 0:
            raise ValueError("阈值必须为正")


class PhoneRules:
    input_types = INPUT_TYPES

    def __init__(self, config: Optional[PhoneRuleConfig] = None) -> None:
        self.config = config or PhoneRuleConfig()
        self._streak: dict[tuple, dict] = {}     # (source, run, holder) -> {"count", "last_ms", "observation_ids", "since_ms", "emitted"}
        self._runs: dict[str, str] = {}

    def __call__(self, observations: list[dict], context: dict) -> list[dict]:
        return self.evaluate(observations, context)

    def reset(self, source_id: Optional[str] = None) -> None:
        for key in [k for k in self._streak if source_id is None or k[0] == source_id]:
            del self._streak[key]
        if source_id is None:
            self._runs.clear()
        else:
            self._runs.pop(source_id, None)

    def evaluate(self, observations: list[dict], context: dict) -> list[dict]:
        config = context.get("config")
        config = config if isinstance(config, PhoneRuleConfig) else self.config     # 派发器把同一 context 交给各场景，别的场景的 config 不能用
        rule_version = _rule_version(context, "S02")
        clues = []
        for observation in observations:
            if not isinstance(observation, dict) or observation.get("type") != "visual_fact":
                continue
            self._track_run(observation)
            clues.extend(self._one(observation, context, config, rule_version))
        return clues

    def _track_run(self, observation: dict) -> None:
        source, run = observation.get("source_id"), observation.get("media_run_id")
        if self._runs.get(source) not in (None, run):
            self.reset(source)
        self._runs[source] = run

    def _one(self, observation: dict, context: dict, config: PhoneRuleConfig, rule_version: str) -> list[dict]:
        payload = observation.get("payload") or {}
        candidate = payload.get("candidate") or {}
        if candidate.get("kind") != "phone":
            return []                               # 纸张 / 水杯等候选与 S02 无关：不计数、不打断手机的连续性
        holder = candidate.get("holder_track_id")
        identity = resolve_identity(context, holder)
        person_id = identity["person_id"] if identity else None
        seat_id = identity["seat_id"] if identity else None
        key = (observation.get("source_id"), observation.get("media_run_id"), holder or "nobody")
        common = dict(scene_id="S02", rule_id=RULE_ID, rule_version=rule_version, track_id=holder, seat_id=seat_id)
        if observation.get("binding_status") != "bound":
            return [new_clue(observation, conclusion="insufficient", risk_level="none", reason="场次 / 阶段未绑定，只保留技术事实",
                             missing_facts=["场次 / 阶段未绑定"], **common)]
        facts, unusable = facts_of(observation)
        if facts is None:
            return []                               # 复核失败是处理状态（D-25）：不出线索，也不打断已有计数
        if unusable:
            self._streak.pop(key, None)
            return [new_clue(observation, conclusion="insufficient", risk_level="none",
                             reason="手部与设备细节看不清，不定性", missing_facts=unusable, **common)]
        positive = (facts.get("visibility") == "clear" and facts.get("object_kind") == "phone"
                    and facts.get("holding") in config.positive_holdings and facts.get("activity") in config.positive_activities)
        if not positive:
            self._streak.pop(key, None)            # 手机放桌上 / 动作看不准 / 没有设备：连续性中断，不出线索
            return []
        now_ms = int(observation["start_ms"])
        streak = self._streak.get(key)
        if streak is not None and observation["observation_id"] in streak["observation_ids"]:
            return []                               # 同一观察重复投递：不重复计数
        if streak is None or now_ms - streak["last_ms"] > config.window_ms:
            streak = {"count": 0, "last_ms": now_ms, "observation_ids": [], "evidence_refs": [], "since_ms": now_ms,
                      "emitted": False, "high": False, "high_emitted": False}
        streak["count"] += 1
        streak["last_ms"] = now_ms
        streak["observation_ids"].append(observation["observation_id"])
        streak["evidence_refs"].extend(observation.get("evidence_refs") or [])
        streak["high"] = streak["high"] or facts.get("holding") in config.high_holdings or facts.get("activity") in config.high_activities
        self._streak[key] = streak
        if streak["count"] < config.min_consecutive:
            return []
        upgrade = streak["emitted"] and streak["high"] and not streak["high_emitted"]
        if streak["emitted"] and not upgrade:
            return []
        streak["emitted"] = True
        streak["high_emitted"] = streak["high"]
        occurred = parse_utc(observation["occurred_at"])
        exemption = _exemption(context, person_id=person_id, seat_id=seat_id, occurred_at=occurred)
        if exemption is not None and upgrade:
            return []                               # 已出过豁免 normal，升级不改变结论
        ids = list(dict.fromkeys(streak["observation_ids"]))
        clue_id = f"clue:S02:{RULE_ID}:{observation['source_id']}:{observation['media_run_id']}:{holder or 'nobody'}:{streak['since_ms']}"
        if upgrade:
            clue_id += ":high"
        if exemption is not None:
            clue = new_clue(observation, conclusion="normal", risk_level="none", person_id=person_id, observation_ids=ids, clue_id=clue_id,
                            start_ms=streak["since_ms"], reason=f"连续 {streak['count']} 次复核可见手机使用，但有有效豁免 device_allowed {exemption.fact_id} v{exemption.fact_version}", **common)
        else:
            level = "high" if streak["high"] else "medium"
            what = "贴耳 / 通话姿态" if streak["high"] else "手持操作或拍摄"
            clue = new_clue(observation, conclusion="suspected", risk_level=level, person_id=person_id, observation_ids=ids, clue_id=clue_id,
                            start_ms=streak["since_ms"],
                            reason=f"同一持有人连续 {streak['count']} 次复核可见手机{what}，发生时刻无豁免；{LIMITATION}，需人工复核", **common)
        clue["evidence_refs"] = list({ref.get("evidence_id"): ref for ref in streak["evidence_refs"] if isinstance(ref, dict)}.values())
        return [clue]


_default = PhoneRules()


def evaluate(observations: list[dict], context: dict) -> list[dict]:
    return _default.evaluate(observations, context)


evaluate.input_types = INPUT_TYPES  # type: ignore[attr-defined]
