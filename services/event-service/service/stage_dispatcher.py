"""Scene dispatch and S10 behavior; wire contracts are owned by ibess_contracts."""
from __future__ import annotations
from datetime import datetime
from typing import Callable, Iterable, Optional
from ibess_contracts.protocol import (
    DispatchError,
    HandlerFailed,
    parse_utc,
    _strings,
    _text,
    _ms,
    is_bound,
    validate_observation,
    check_scope,
    allowed_scenes,
    validate_clue,
    CONTRACT_VERSION,
    SCENES,
    PHASES,
    CONCLUSIONS,
    RISK_LEVELS,
    FAULT_TYPES,
    RECOVERY_TYPES,
    HEALTH_TYPES,
    SCOPE,
    PHASE_FIELDS,
    UTC_PATTERN,
    OBSERVATION_REQUIRED,
    CLUE_REQUIRED,
    S10_RULE_ID,
    S10_RULE_VERSION_FALLBACK
)

def new_clue(observation: dict, *, scene_id: str, rule_id: str, rule_version: str, conclusion: str, risk_level: str,
             reason: str, missing_facts: Iterable[str] = (), quality_flags: Iterable[str] = (), person_id: Optional[str] = None,
             track_id: Optional[str] = None, seat_id: Optional[str] = None, evidence_refs: Optional[list] = None,
             observation_ids: Optional[list] = None, clue_id: Optional[str] = None, start_ms: Optional[int] = None,
             end_ms: Optional[int] = None) -> dict:
    """规则用它从触发观察构造线索：作用域、快照、媒体字段抄观察，不让规则自己拼。"""
    bound = is_bound(observation)
    flags = list(dict.fromkeys([*observation.get("quality_flags", []), *quality_flags]))
    if not bound and "binding_missing" not in flags:
        flags.append("binding_missing")
    if not bound:
        conclusion, risk_level, person_id = "insufficient", "none", None
    facts = list(dict.fromkeys(missing_facts))
    if conclusion == "insufficient" and not facts:
        facts = ["上下文不完整"]
    ids = list(observation_ids) if observation_ids is not None else [observation["observation_id"]]
    start = observation["start_ms"] if start_ms is None else start_ms
    end = observation["end_ms"] if end_ms is None else end_ms
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
        "start_ms": start, "end_ms": end,
        "person_id": person_id, "track_id": track_id, "seat_id": seat_id,
        "conclusion": conclusion, "risk_level": risk_level, "reason": reason,
        "observation_ids": ids, "missing_facts": facts, "quality_flags": flags,
        "evidence_refs": list(evidence_refs) if evidence_refs is not None else list(observation.get("evidence_refs", [])),
    }


def s10_health_rule(observations: list[dict], context: dict) -> list[dict]:
    """内置 S10：故障开口 → suspected（见证缺口，不推断人为）；恢复 → normal；缺口起点未知 → insufficient。"""
    rule_version = context.get("rule_version") or S10_RULE_VERSION_FALLBACK
    clues = []
    for observation in observations:
        kind = observation["type"]
        if kind not in HEALTH_TYPES:
            continue
        payload = observation.get("payload") or {}
        gap_id = payload.get("gap_id")
        flags = set(observation.get("quality_flags", []))
        if kind in RECOVERY_TYPES:
            clues.append(new_clue(observation, scene_id="S10", rule_id=S10_RULE_ID, rule_version=rule_version,
                                  conclusion="normal", risk_level="none",
                                  reason=f"设备恢复（{kind}），关闭缺口 {gap_id or '未知'}；历史缺口保留"))
            continue
        if not gap_id or "context_incomplete" in flags or payload.get("gap_start_unknown"):
            clues.append(new_clue(observation, scene_id="S10", rule_id=S10_RULE_ID, rule_version=rule_version,
                                  conclusion="insufficient", risk_level="none",
                                  reason=f"设备故障（{kind}）但缺口起点或采样不完整，范围未知",
                                  missing_facts=["缺健康采样或故障起点，缺口范围未知"]))
            continue
        clues.append(new_clue(observation, scene_id="S10", rule_id=S10_RULE_ID, rule_version=rule_version,
                              conclusion="suspected" if is_bound(observation) else "insufficient",
                              risk_level="medium" if is_bound(observation) else "none",
                              reason=f"设备故障（{kind}）形成见证缺口 {gap_id}；只是技术事实，不推断人为破坏",
                              missing_facts=[] if is_bound(observation) else ["场次 / 阶段未绑定"]))
    return clues


s10_health_rule.input_types = HEALTH_TYPES  # type: ignore[attr-defined]


# ---------------------------------------------------------------- 派发
def dispatch(observation: dict, snapshot: Optional[dict], handlers: dict[str, Callable], *, context: Optional[dict] = None) -> list[dict]:
    """返回校验过的线索列表。作用域不符抛 DispatchError；规则异常抛 HandlerFailed；两者都是处理失败。"""
    validate_observation(observation)
    if not isinstance(handlers, dict):
        raise DispatchError("HANDLERS_INVALID", "handlers 必须是 {scene_id: evaluate}")
    for scene_id in handlers:
        if scene_id not in SCENES:
            raise DispatchError("HANDLERS_INVALID", f"未知场景 {scene_id}")
    if is_bound(observation) and snapshot is None:
        raise DispatchError("SNAPSHOT_MISSING", "bound 观察必须附发生时刻的快照；缺快照不能被格式合法替代")
    bound = is_bound(observation)
    if bound:
        check_scope(observation, snapshot)
    base = dict(context or {})
    base.update({"snapshot": snapshot if bound else None, "observation": observation,
                 "exceptions": list((snapshot or {}).get("exceptions") or []) if bound else []})
    results: list[dict] = []
    is_health = observation["type"] in HEALTH_TYPES
    # 1. S10 故障 / 恢复先处理，不做场景过滤（S10 全程启用，恢复任何阶段都能关缺口）。
    if is_health:
        handler = handlers.get("S10", s10_health_rule)
        declared = getattr(handler, "input_types", None)
        if declared is not None and observation["type"] not in declared:
            handler = s10_health_rule              # 自定义 S10 规则不吃这种健康观察时回落内置规则
        rule_version = (snapshot or {}).get("rule_versions", {}).get("S10") if bound else None
        ctx = {**base, "scene_id": "S10", "rule_version": rule_version or S10_RULE_VERSION_FALLBACK}
        for clue in _run(handler, "S10", observation, ctx):
            validate_clue(clue, snapshot if bound else None, "S10", observation)
            results.append(clue)
    if not bound:
        return results                       # unbound：只保留技术事实，不进任何场景规则
    allowed = allowed_scenes(snapshot)
    rule_versions = snapshot.get("rule_versions") or {}
    for scene_id, handler in handlers.items():
        if scene_id == "S10" and is_health:
            continue
        if scene_id not in allowed:
            continue
        input_types = getattr(handler, "input_types", None)
        if input_types is not None and observation["type"] not in input_types:
            continue
        if scene_id == "S10" and input_types is None:
            continue                         # 没声明类型的 S10 规则只吃健康观察
        ctx = {**base, "scene_id": scene_id, "rule_version": rule_versions.get(scene_id)}
        for clue in _run(handler, scene_id, observation, ctx):
            validate_clue(clue, snapshot, scene_id, observation)
            results.append(clue)
    return results


def _run(handler: Callable, scene_id: str, observation: dict, context: dict) -> list[dict]:
    try:
        clues = handler([observation], context)
    except Exception as exc:  # noqa: BLE001  规则异常是处理失败
        raise HandlerFailed(scene_id, exc) from exc
    if clues is None:
        return []
    if not isinstance(clues, list):
        raise DispatchError("CLUE_INVALID", f"{scene_id} 规则必须返回线索列表")
    return clues
