"""event-service：消费 Redis Streams Observation → 规则 → Event 写入 PostgreSQL。

最小垂直切片实现：
- person_detected → PENDING 事件（同类型 5 秒冷却去重）
- video_offline → 视频断流事件（high）
- camera_blacked → 遮挡/黑屏事件（medium）
"""
import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import threading

import redis.asyncio as redis
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from service.models import Base, Decision, Event, Evidence, Operator, OperatorBinding
from service.operators import OperatorConfig, evaluate_candidates, validate_parameters, _scope_matches
from service.supervision_integration import observation_scope, save_context, persist_episode, episode_key, finish_observed_episodes, record_signal
from service.a_scene_events import A_EVENT_TYPE, build_event as build_a_event
from service.clue_models import RiskClueRecord

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("event-service")

STREAM = os.getenv("REDIS_STREAM", "observations")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg://demo@localhost:5432/demo"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
def initialize_database():
    from ibess_db.schema import require_schema
    require_schema(engine, ("events", "evidences", "operators", "operator_bindings", "risk_clues"))


Session = sessionmaker(bind=engine)

# 2.3 事件冷却：按 摄像头×事件类型 分级冷却（原实现仅按类型全局5秒，多路摄像头下事件洪泛）。
DEFAULT_COOLDOWN_SECONDS = float(os.getenv("EVENT_COOLDOWN", "60"))
COOLDOWN_BY_TYPE = {
    "person_detected": float(os.getenv("EVENT_COOLDOWN_PERSON", "90")),
    "crowd_gathering": float(os.getenv("EVENT_COOLDOWN_CROWD", "180")),
    "long_talking": float(os.getenv("EVENT_COOLDOWN_LONG_TALK", "180")),
    "sensitive_word": float(os.getenv("EVENT_COOLDOWN_SENSITIVE", "30")),
    "seat_absence": float(os.getenv("EVENT_COOLDOWN_SEAT_ABSENCE", "180")),
    "zone_intrusion": float(os.getenv("EVENT_COOLDOWN_ZONE", "120")),
    "phone_call": float(os.getenv("EVENT_COOLDOWN_PHONE_CALL", "120")),
    "phone_capture": float(os.getenv("EVENT_COOLDOWN_PHONE_CAPTURE", "180")),
    "phone_unstored": float(os.getenv("EVENT_COOLDOWN_PHONE_UNSTORED", "180")),
    "transcript_text": float(os.getenv("EVENT_COOLDOWN_TRANSCRIPT", "30")),
}


def cooldown_for(obs_type: str) -> float:
    return COOLDOWN_BY_TYPE.get(obs_type, DEFAULT_COOLDOWN_SECONDS)


def compute_absence_seconds(last_seen: dict, source_id: str, count: int, now: float) -> float:
    """5.1 离席判定状态：按摄像头跟踪最近一次有人时刻，计算连续无人秒数。

    count>0 时刷新 last_seen 并返回0；count=0 时返回 now-last_seen（无记录则0）。
    """
    key = ("seen", source_id)
    if count > 0:
        last_seen[key] = now
        return 0.0
    first = last_seen.get(key)
    if first is None:
        last_seen[key] = now
        return 0.0
    return max(0.0, now - first)


def should_emit(last_emit: dict, source_id: str, obs_type: str, now: float) -> bool:
    """同一摄像头同一事件类型在冷却窗内只发一次；返回True时调用方记录时间戳。"""
    key = (source_id, obs_type)
    if key in last_emit and now - last_emit[key] < cooldown_for(obs_type):
        return False
    return True


OPERATOR_REFRESH_SECONDS = float(os.getenv("OPERATOR_REFRESH_SECONDS", "3"))

# ---- 阶段控制 ack 回写（10.1）：收到控制消息并应用缓存后，向API确认applied。----
PHASE_CONTROL_API = os.getenv("POC_PHASE_CONTROL_API", "")
PHASE_CONTROL_TOKEN = os.getenv("POC_PHASE_CONTROL_TOKEN", "")
PHASE_CONTROL_CONSUMER_ID = os.getenv("POC_PHASE_CONTROL_CONSUMER_ID", "event-service")
PHASE_CONTROL_CONTRACT = "2.0.0"


def send_phase_ack(notification: dict, payload: dict, status: str = "applied", error: dict | None = None):
    return _send_phase_ack(_context, notification, payload, status, error)


async def handle_phase_control(r, data: dict):
    return await _handle_phase_control(_context, r, data)

# 恢复观察 → 对应设备异常事件类型（恢复时自动关闭）
RECOVERY_MAP = {
    "video_online": "video_offline",
    "camera_normal": "camera_blacked",
    "audio_online": "audio_offline",
}


async def handle_recovery(r, data: dict):
    return await _handle_recovery(_context, r, data)

from service.event_templates import EVENT_TEMPLATES

operator_cache: dict[str, OperatorConfig] = {}
operator_cache_at = 0.0
phase_scene_cache: dict[str, set[str]] = {}


def observation_allowed(session_id: str, obs_type: str, allowed: set[str]) -> bool:
    if obs_type in {"video_offline", "camera_blacked", "video_online", "camera_normal"}:
        return "S10" in allowed
    if obs_type == "person_detected":
        return bool(allowed.intersection({"S01", "S02", "S03", "S04", "S05", "S08", "S09", "V02", "V03", "V04", "V05", "V06"}))
    if obs_type in {"transcript_text", "sensitive_word", "long_talking"}:
        return bool(allowed.intersection({"S06", "S07", "A01", "A02", "A03"}))
    return True


def update_phase_cache(data: dict):
    return _update_phase_cache(_context, data)


# 手机类算子依赖的具体场景：观察类型是 person_detected，光靠阶段过滤会把"准备阶段的通话"也放进来
# 事件类型 → 归属场景：阶段切换后只让本阶段该管的事件通过
OPERATOR_SCENES = {
    'whispering': ('S05',), 'workstation_visit': ('S05',), 'document_transfer': ('S08',),
    'frequent_absence': ('S03',), 'joint_absence': ('S03',), 'unauthorized_entry': ('S04',),
    "phone_unstored": ("S01",),
    "phone_call": ("S02",),
    "phone_capture": ("S08",),
    "phone_detected": None,  # 按机位决定
    "crowd_gathering": ("S02", "S03", "S05", "S09"),
    "zone_intrusion": ("S05",),
    "seat_absence": ("S03",),
}
PHONE_DETECTED_SCENE_BY_CAM = {"camera-002": "S01", "camera-003": "S02", "camera-006": "S08"}


def scene_allowed(code: str, source: str, allowed) -> bool:
    """allowed 为 None（缓存未就绪）时放行，避免因为配置问题把事件全吞掉。"""
    if allowed is None:
        return True
    scenes = OPERATOR_SCENES.get(code)
    if code == "phone_detected":
        scenes = ('S01', 'S02', 'S08')
    if not scenes:
        return True
    return any(scene in allowed for scene in scenes if scene)


def load_phase_cache_from_db():
    return _load_phase_cache_from_db(_context)


def refresh_operator_cache(force: bool = False):
    return _refresh_operator_cache(_context, force)


def _decode(data: dict) -> dict:
    from ibess_contracts.messages import decode_message
    return decode_message(data)


_SESSION_CACHE = {"at": 0.0, "value": None}


def active_session_id() -> str | None:
    return _active_session_id(_context)


_PHASE_CACHE = {"at": 0.0, "value": None}


def current_phase_id() -> str | None:
    return _current_phase_id(_context)


# 关键帧每 5 秒才存一张，事件恰好落在间隔里就没有图；用同机位最近一张兜底，避免列表出现空占位
LAST_KEYFRAME: dict[str, tuple[str, float]] = {}
KEYFRAME_REUSE_SECONDS = float(os.getenv("KEYFRAME_REUSE_SECONDS", "15"))


async def commit_and_publish(r, evt: Event, frame_key: str | None = None, scope=None):
    return await _commit_and_publish(_context, r, evt, frame_key, scope)


ABSENCE_SEEN: dict[tuple[str, str], float] = {}
SEAT_ABSENCE_SECONDS = float(os.getenv("SEAT_ABSENCE_SECONDS", "60"))

# 时序确认：聚集/越界这类"状态型"事件，条件要持续够久才算数，避免路过、瞬间站一起就报
SUSTAIN = {}
SUSTAIN_SECONDS = {
    "crowd_gathering": float(os.getenv("SUSTAIN_CROWD_SECONDS", "8")),
    "zone_intrusion": float(os.getenv("SUSTAIN_ZONE_SECONDS", "3")),
}
SUSTAIN_GAP = 6.0


def sustained(source: str, event_type: str, now: float, subject=None) -> bool:
    """返回 True 表示已经持续够久、可以出事件；中间断了会在 gap 后重新计时。"""
    need = SUSTAIN_SECONDS.get(event_type)
    if not need:
        return True
    key = (source, event_type, subject)
    state = SUSTAIN.get(key)
    if state is None or now - state[1] > SUSTAIN_GAP:
        SUSTAIN[key] = [now, now]
        return False
    state[1] = now
    if now - state[0] >= need:
        return True
    return False



async def handle_observation(r, data: dict, last_emit: dict):
    return await _handle_observation(_context, r, data, last_emit)


async def consume():
    return await _consume(_context)


AUTO_CLOSE_MINUTES = {
    "person_detected": 1,
    "video_online": 5,
    "camera_normal": 5,
    "crowd_gathering": 10,
    "seat_absence": 10,
    "zone_intrusion": 15,
    "video_offline": 10,
    "camera_blacked": 30,
    "sensitive_word": 60,
    "long_talking": 60,
    "phone_unstored": 120,
    "phone_call": 120,
    "phone_capture": 120,
}


async def auto_close_pending():
    return await _auto_close_pending(_context)


async def cleanup_old_data():
    return await _cleanup_old_data(_context)  # 每天执行一次


async def main():
    return await _main(_context)


from ibess_runtime.context import Namespace
from service.application.persistence import handle_recovery as _handle_recovery
from service.application.persistence import commit_and_publish as _commit_and_publish
from service.application.observations import handle_observation as _handle_observation
from service.application.configuration import refresh_operator_cache as _refresh_operator_cache
from service.application.configuration import load_phase_cache_from_db as _load_phase_cache_from_db
from service.application.configuration import active_session_id as _active_session_id
from service.application.configuration import current_phase_id as _current_phase_id
from service.application.phase_control import send_phase_ack as _send_phase_ack
from service.application.phase_control import handle_phase_control as _handle_phase_control
from service.application.phase_control import update_phase_cache as _update_phase_cache
from service.runtime import consume as _consume
from service.runtime import auto_close_pending as _auto_close_pending
from service.runtime import cleanup_old_data as _cleanup_old_data
from service.runtime import main as _main

_context = Namespace(globals())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
