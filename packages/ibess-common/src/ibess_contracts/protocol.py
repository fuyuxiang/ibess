"""执行卡 2.3：按场景派发观察（契约 2.0.0），规则出三态线索，服务层逐条校验。

顺序固定：验观察形状 → 验作用域与发生时刻快照 → S10 故障 / 恢复先处理（不做场景过滤）→
其余观察只交给快照启用、且声明消费该类型的场景规则 → 校验每条线索。
派发器本身不认识任何业务字段，也没有「类型 → 场景」硬编码表：规则自己用 `input_types` 声明吃什么。
规则签名：evaluate(observations, context) -> list[RiskClue]。规则抛异常 → HandlerFailed（处理失败，不是结论）。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Callable, Iterable, Optional

CONTRACT_VERSION = "2.0.0"
SCENES = tuple(f"S{i:02}" for i in range(1, 11))
PHASES = ("SIGNIN", "DISCIPLINE", "PRELIM", "DETAIL", "CLARIFY", "OPENING", "ATTENDANCE", "RECUSAL", "PRELIMINARY", "DETAILED", "CLARIFICATION", "SCORING", "REPORT", "OFFLINE_QA")
CONCLUSIONS = ("normal", "suspected", "insufficient")
RISK_LEVELS = ("none", "low", "medium", "high")
FAULT_TYPES = {"video_offline", "camera_blacked", "audio_offline"}
RECOVERY_TYPES = {"video_online", "camera_normal", "audio_online"}
HEALTH_TYPES = FAULT_TYPES | RECOVERY_TYPES
SCOPE = ("project_id", "room_id", "session_id")
PHASE_FIELDS = ("stage_snapshot_id", "phase_id", "phase_version")
UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
OBSERVATION_REQUIRED = ("contract_version", "project_id", "room_id", "session_id", "source_id", "stage_snapshot_id", "phase_id",
                        "phase_version", "observation_id", "binding_status", "occurred_at", "media_run_id", "asset_id",
                        "start_ms", "end_ms", "type", "payload", "model_name", "model_version", "quality_flags", "evidence_refs")
CLUE_REQUIRED = ("contract_version", "project_id", "room_id", "session_id", "source_id", "stage_snapshot_id", "phase_id",
                 "phase_version", "clue_id", "binding_status", "scene_id", "rule_id", "rule_version", "occurred_at",
                 "media_run_id", "asset_id", "start_ms", "end_ms", "person_id", "track_id", "seat_id", "conclusion",
                 "risk_level", "reason", "observation_ids", "missing_facts", "quality_flags", "evidence_refs")
S10_RULE_ID = "s10-witness-gap"
S10_RULE_VERSION_FALLBACK = "s10-v1"


class DispatchError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class HandlerFailed(RuntimeError):
    def __init__(self, scene_id: str, exc: BaseException):
        self.scene_id = scene_id
        self.cause = exc
        super().__init__(f"{scene_id} 规则执行失败: {type(exc).__name__}")


def parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not UTC_PATTERN.match(value):
        raise DispatchError("TIME_INVALID", "时间必须是 Z 结尾的 UTC ISO 字符串")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DispatchError("TIME_INVALID", "时间不是合法日期") from None


def _strings(values) -> bool:
    return isinstance(values, list) and all(isinstance(v, str) and v for v in values) and len(set(values)) == len(values)


def _text(value, limit=256) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _ms(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def is_bound(obj: dict) -> bool:
    return obj.get("binding_status") == "bound"


# ---------------------------------------------------------------- 校验
def validate_observation(observation: dict) -> None:
    if not isinstance(observation, dict):
        raise DispatchError("OBSERVATION_INVALID", "观察必须是对象")
    missing = [k for k in OBSERVATION_REQUIRED if k not in observation]
    if missing:
        raise DispatchError("OBSERVATION_INVALID", f"观察缺字段 {missing[:3]}")
    if observation["contract_version"] != CONTRACT_VERSION:
        raise DispatchError("OBSERVATION_INVALID", "契约版本不是 2.0.0")
    for key in ("source_id", "observation_id", "media_run_id", "type", "model_name", "model_version"):
        if not _text(observation[key]):
            raise DispatchError("OBSERVATION_INVALID", f"{key} 必须是非空字符串")
    parse_utc(observation["occurred_at"])
    if not _ms(observation["start_ms"]) or not _ms(observation["end_ms"]) or observation["end_ms"] < observation["start_ms"]:
        raise DispatchError("OBSERVATION_INVALID", "start_ms / end_ms 非法")
    if observation["binding_status"] not in ("bound", "unbound"):
        raise DispatchError("OBSERVATION_INVALID", "binding_status 非法")
    if not isinstance(observation["payload"], dict) or not _strings(observation["quality_flags"]) or not isinstance(observation["evidence_refs"], list):
        raise DispatchError("OBSERVATION_INVALID", "payload / quality_flags / evidence_refs 类型非法")
    if is_bound(observation):
        if any(not _text(observation[k]) for k in SCOPE) or not _text(observation["stage_snapshot_id"]):
            raise DispatchError("OBSERVATION_INVALID", "bound 观察必须有完整作用域与快照")
        if observation["phase_id"] not in PHASES or not isinstance(observation["phase_version"], int) or observation["phase_version"] < 1:
            raise DispatchError("OBSERVATION_INVALID", "bound 观察阶段字段非法")
    else:
        if any(observation[k] is not None for k in PHASE_FIELDS) or "binding_missing" not in observation["quality_flags"]:
            raise DispatchError("OBSERVATION_INVALID", "unbound 观察阶段字段必须为 null 且带 binding_missing")


def check_scope(observation: dict, snapshot: dict) -> None:
    """观察必须属于这份快照：同场次 / 项目 / 室，源一致（快照 source_id 为 null 表示场次级），快照 id 一致且已生效。"""
    if not isinstance(snapshot, dict) or not _text(snapshot.get("stage_snapshot_id")):
        raise DispatchError("SNAPSHOT_INVALID", "快照缺 stage_snapshot_id")
    for key in SCOPE:
        if observation.get(key) != snapshot.get(key):
            raise DispatchError("SCOPE_MISMATCH", f"观察与快照的 {key} 不一致")
    if snapshot.get("source_id") is not None and observation["source_id"] != snapshot["source_id"]:
        raise DispatchError("SCOPE_MISMATCH", "观察的源不属于该快照")
    for key in PHASE_FIELDS:
        if observation.get(key) != snapshot.get(key):
            raise DispatchError("SNAPSHOT_MISMATCH", f"观察引用的 {key} 与快照不一致")
    effective = snapshot.get("effective_at")
    if not _text(effective):
        raise DispatchError("SNAPSHOT_INVALID", "快照缺 effective_at")
    if parse_utc(observation["occurred_at"]) < parse_utc(effective):
        raise DispatchError("SNAPSHOT_NOT_EFFECTIVE", "媒体发生时快照尚未生效")


def allowed_scenes(snapshot: dict) -> set[str]:
    return set(snapshot.get("enabled_scenes") or []) | set(snapshot.get("always_on_scenes") or ["S10"])


def validate_clue(clue: dict, snapshot: Optional[dict], scene_id: str, observation: Optional[dict] = None) -> None:
    if not isinstance(clue, dict):
        raise DispatchError("CLUE_INVALID", "线索必须是对象")
    missing = [k for k in CLUE_REQUIRED if k not in clue]
    if missing:
        raise DispatchError("CLUE_INVALID", f"线索缺字段 {missing[:3]}")
    if clue["contract_version"] != CONTRACT_VERSION or clue["scene_id"] != scene_id or clue["scene_id"] not in SCENES:
        raise DispatchError("CLUE_INVALID", "线索场景或契约版本与派发不一致")
    if clue["conclusion"] not in CONCLUSIONS or clue["risk_level"] not in RISK_LEVELS:
        raise DispatchError("CLUE_INVALID", "结论或风险等级非法")
    if (clue["conclusion"] == "suspected") != (clue["risk_level"] != "none"):
        raise DispatchError("CLUE_INVALID", "suspected 必须有非 none 风险等级，其余必须 none")
    if not _text(clue["clue_id"]) or not _text(clue["rule_id"]) or not _text(clue["rule_version"]) or not _text(clue["reason"], 4096):
        raise DispatchError("CLUE_INVALID", "clue_id / rule_id / rule_version / reason 非法")
    if not _ms(clue["start_ms"]) or not _ms(clue["end_ms"]) or clue["end_ms"] < clue["start_ms"]:
        raise DispatchError("CLUE_INVALID", "线索时间区间非法")
    for key in ("observation_ids", "missing_facts", "quality_flags"):
        if not _strings(clue[key]):
            raise DispatchError("CLUE_INVALID", f"{key} 必须是非空、不重复的字符串列表")
    if not isinstance(clue["evidence_refs"], list) or any(not isinstance(ref, dict) for ref in clue["evidence_refs"]):
        raise DispatchError("CLUE_INVALID", "evidence_refs 必须是对象列表")
    parse_utc(clue["occurred_at"])
    if not _text(clue["source_id"]) or not _text(clue["media_run_id"]):
        raise DispatchError("CLUE_INVALID", "source_id / media_run_id 必须是非空字符串")
    for ref in clue["evidence_refs"]:
        for key in SCOPE + ("source_id", "media_run_id", "asset_id"):
            if ref.get(key) != clue.get(key):
                raise DispatchError("CLUE_INVALID", f"证据引用的 {key} 与线索不一致")
    if clue["binding_status"] == "bound":
        if any(not _text(clue[k]) for k in SCOPE) or not _text(clue["stage_snapshot_id"]):
            raise DispatchError("CLUE_INVALID", "bound 线索必须有完整作用域与快照")
        if clue["phase_id"] not in PHASES or isinstance(clue["phase_version"], bool) or not isinstance(clue["phase_version"], int) or clue["phase_version"] < 1:
            raise DispatchError("CLUE_INVALID", "bound 线索阶段字段非法")
    if clue["binding_status"] == "unbound":
        if clue["conclusion"] != "insufficient" or clue["person_id"] is not None:
            raise DispatchError("CLUE_INVALID", "unbound 线索只能是 insufficient 且 person_id 为 null")
        if any(clue[k] is not None for k in PHASE_FIELDS) or "binding_missing" not in clue["quality_flags"]:
            raise DispatchError("CLUE_INVALID", "unbound 线索阶段字段必须为 null 且带 binding_missing")
    elif clue["binding_status"] != "bound":
        raise DispatchError("CLUE_INVALID", "binding_status 非法")
    if clue["conclusion"] == "suspected":
        if clue["binding_status"] != "bound":
            raise DispatchError("CLUE_INVALID", "suspected 必须 bound")
        if not clue["observation_ids"] or not clue["evidence_refs"]:
            raise DispatchError("CLUE_INVALID", "suspected 必须引用观察与证据")
    if clue["conclusion"] == "insufficient" and not clue["missing_facts"]:
        raise DispatchError("CLUE_INVALID", "insufficient 必须列出缺失事实")
    if observation is not None:
        for key in SCOPE + ("source_id", "media_run_id", "asset_id", "occurred_at") + PHASE_FIELDS:
            if clue.get(key) != observation.get(key):
                raise DispatchError("CLUE_INVALID", f"线索的 {key} 与观察不一致")
        if observation["observation_id"] not in clue["observation_ids"] and clue["conclusion"] == "suspected":
            raise DispatchError("CLUE_INVALID", "suspected 线索必须引用触发它的观察")
    if snapshot is not None and clue["binding_status"] == "bound":
        if scene_id not in allowed_scenes(snapshot):
            raise DispatchError("SCENE_DISABLED", f"阶段没有启用 {scene_id}")
        expected = (snapshot.get("rule_versions") or {}).get(scene_id)
        if expected is None or clue["rule_version"] != expected:
            raise DispatchError("RULE_VERSION_MISMATCH", f"{scene_id} 线索规则版本与快照不一致")


# ---------------------------------------------------------------- 构造
