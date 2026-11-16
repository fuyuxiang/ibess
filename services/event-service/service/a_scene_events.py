"""5.01：event-service 消费 vision-worker 发来的 A 场景事件（观察类型 a_scene_event）→ B 的 Event。

payload 由 vision-worker 的 A 扩展组好：scene_id / event_type / risk_level / reason / conclusion 与原始线索 clue。
这里只做三件事：线索先过 2.3 的契约校验（validate_clue）；按阶段快照启用的场景过滤（与 B 相同口径）；按 (源, 事件类型) 冷却。
校验不过只记日志、不出事件。B 原有的观察类型不经过这里。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from service.stage_dispatcher import DispatchError, validate_clue

log = logging.getLogger("event-service.a_scene")

A_EVENT_TYPE = "a_scene_event"
ALWAYS_ON = frozenset()     # 与 B 的 observation_allowed 一致：S10 也要在阶段启用场景里（5.01 复核）
RISKS = frozenset({"low", "medium", "high"})


def scene_allowed(scene_id: Optional[str], allowed: Optional[set]) -> bool:
    """allowed 为 None 表示还没收到阶段控制消息，与 B 的 observation_allowed 一样放行。"""
    if allowed is None:
        return True
    return scene_id in allowed or scene_id in ALWAYS_ON


def build_event(data: dict, *, event_cls, last_emit: dict, now: float, allowed: Optional[set],
                cooldown_for: Callable[[str], float]):
    """返回待提交的 Event；不出事件时返回 None（阶段未启用、校验不过、冷却中）。"""
    try:
        payload = json.loads(data.get("payload", "{}"))
    except (TypeError, ValueError):
        log.warning("A 场景事件 payload 不是 JSON，丢弃")
        return None
    scene_id, event_type = payload.get("scene_id"), payload.get("event_type")
    risk, reason, clue = payload.get("risk_level"), payload.get("reason"), payload.get("clue")
    if not isinstance(event_type, str) or not event_type or risk not in RISKS or not isinstance(reason, str):
        log.warning("A 场景事件字段非法，丢弃：%s", event_type)
        return None
    if not scene_allowed(scene_id, allowed):
        log.info("当前阶段忽略 A 场景事件：scene=%s type=%s", scene_id, event_type)
        return None
    try:
        validate_clue(clue, None, scene_id)
    except DispatchError as exc:
        log.warning("A 场景事件线索校验不过，丢弃：%s %s", exc.code, exc)
        return None
    source = data.get("source_id", "camera-001")
    subject=clue.get('seat_id') or clue.get('track_id') or clue.get('person_id')
    key = (data.get('session_id'),source,event_type,subject,clue.get('media_run_id'))
    if key in last_emit and now - last_emit[key] < cooldown_for(event_type):
        return None
    last_emit[key] = now
    occurred = float(data.get("occurred_at", now))
    return event_cls(
        event_id=str(uuid.uuid4()), session_id=data.get("session_id", "session-001"), camera_id=source,
        person_id=clue.get("person_id"), seat_id=clue.get("seat_id"), zone_id=None,
        skill_code=str(clue.get("rule_id"))[:64], skill_version=str(clue.get("rule_version"))[:32],
        model_name="a-ext", model_version="5.01", event_type=event_type, risk_level=risk, confidence=None,
        reason=reason, occurred_at=datetime.fromisoformat(clue['occurred_at'].replace('Z','+00:00')).astimezone(timezone.utc).replace(tzinfo=None),
        phase_id=clue.get('phase_id'), status="PENDING",
    )


__all__ = ["A_EVENT_TYPE", "build_event", "scene_allowed"]
