"""执行卡 7.3：S05 跨区域接触。evaluate(observations, context) -> list[RiskClue]。

单路视频只能证明同一画面里的事：限制区停留（5.1 zone_dwell_progress）、两轨迹接近停留（5.1 person_count 的 tracks）。
「跨评标室」是画面外的事实：单源一律 insufficient 并列补采条件；cross_room_protocol 只给多源协议（带 protocol_fixture 标记），
没有在真实多机位验证过，不冒充跨室追踪。权限按 5.2 的许可判定：绑得上当事人才套用。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from worker.absence_rules import new_clue, parse_utc, resolve_permission
from worker.device_rules import _rule_version, resolve_identity

RULE_ZONE = "s05-zone-access"
RULE_PROXIMITY = "s05-proximity"
RULE_CROSS_ROOM = "s05-cross-room"
INPUT_TYPES = frozenset({"zone_dwell_progress", "zone_left", "person_count"})
UNRELIABLE_FLAGS = frozenset({"camera_degraded", "camera_offline", "video_offline", "camera_blacked", "frame_error", "sampling_gap"})
CROSS_ROOM_MISSING = ("第二评标室机位的同一人员轨迹（单路视频无法证明跨室）", "门禁 / 通道记录")


@dataclass
class CrossZoneConfig:
    dwell_ms: int = 2000                 # 限制区停留阈值（执行卡:81 区域越界 2 秒）
    proximity_distance: float = 0.08     # 两人框中心的归一化距离阈值（工程初值）
    proximity_ms: int = 5000             # 接近持续阈值

    def __post_init__(self) -> None:
        if self.dwell_ms <= 0 or self.proximity_ms <= 0 or not 0.0 < self.proximity_distance < 1.0:
            raise ValueError("阈值非法")


def _center(bbox) -> tuple[float, float]:
    return (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0


def _identity(context: dict, track_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    identity = resolve_identity(context, track_id)
    return (identity["person_id"], identity["seat_id"]) if identity else (None, None)


class CrossZoneRules:
    input_types = INPUT_TYPES

    def __init__(self, config: Optional[CrossZoneConfig] = None) -> None:
        self.config = config or CrossZoneConfig()
        self._open: dict[tuple, dict] = {}        # (source, run, subject) -> {clue_id, since_ms, conclusion, permission}
        self._pairs: dict[tuple, dict] = {}       # (source, run, a, b) -> {since_ms, last_ms}
        self._runs: dict[str, str] = {}

    def __call__(self, observations: list[dict], context: dict) -> list[dict]:
        return self.evaluate(observations, context)

    def reset(self, source_id: Optional[str] = None) -> None:
        for store in (self._open, self._pairs):
            for key in [k for k in store if source_id is None or k[0] == source_id]:
                del store[key]
        if source_id is None:
            self._runs.clear()
        else:
            self._runs.pop(source_id, None)

    def evaluate(self, observations: list[dict], context: dict) -> list[dict]:
        config = context.get("config")
        config = config if isinstance(config, CrossZoneConfig) else self.config     # 派发器把同一 context 交给各场景
        rule_version = _rule_version(context, "S05")
        clues: list[dict] = []
        for observation in observations:
            if not isinstance(observation, dict) or observation.get("type") not in INPUT_TYPES:
                continue
            self._track_run(observation)
            if observation.get("binding_status") != "bound":
                clues.extend(self._unbound(observation, rule_version))
                continue
            if set(observation.get("quality_flags") or []) & UNRELIABLE_FLAGS:
                if observation["type"] == "person_count":   # 画面故障不能累计接近时长：清掉本源本轮的配对计时
                    for pair in [p for p in self._pairs if p[0] == observation["source_id"] and p[1] == observation["media_run_id"]]:
                        del self._pairs[pair]
                continue
            kind = observation["type"]
            if kind in ("zone_dwell_progress", "zone_left"):
                clues.extend(self._zone(observation, context, config, rule_version))
            elif kind == "person_count":
                clues.extend(self._proximity(observation, context, config, rule_version))
        return clues

    # ---- 内部 ----
    def _track_run(self, observation: dict) -> None:
        source, run = observation.get("source_id"), observation.get("media_run_id")
        if self._runs.get(source) not in (None, run):
            self.reset(source)
        self._runs[source] = run

    def _unbound(self, observation: dict, rule_version: str) -> list[dict]:
        key = (observation["source_id"], observation["media_run_id"], f"unbound:{observation['type']}")
        if key in self._open:
            return []
        self._open[key] = {"clue_id": None, "since_ms": observation["start_ms"], "conclusion": "insufficient", "permission": None}
        return [new_clue(observation, scene_id="S05", rule_id=RULE_ZONE, rule_version=rule_version, conclusion="insufficient", risk_level="none",
                         reason="场次 / 阶段未绑定，只保留技术事实", missing_facts=["场次 / 阶段未绑定"])]

    def _zone(self, observation: dict, context: dict, config: CrossZoneConfig, rule_version: str) -> list[dict]:
        payload = observation.get("payload") or {}
        zone_id, track_id = payload.get("zone_id"), payload.get("track_id")
        if not isinstance(zone_id, str) or not isinstance(track_id, str):
            return []
        key = (observation["source_id"], observation["media_run_id"], f"zone:{zone_id}:{track_id}")
        person_id, seat_id = _identity(context, track_id)
        dwell_ms = int(payload.get("dwell_ms") or 0)
        if observation["type"] == "zone_left":
            opened = self._open.pop(key, None)
            if opened is None:
                return []
            return [new_clue(observation, scene_id="S05", rule_id=RULE_ZONE, rule_version=rule_version, conclusion="normal", risk_level="none",
                             track_id=track_id, seat_id=seat_id, person_id=person_id, start_ms=opened["since_ms"], end_ms=observation["end_ms"],
                             clue_id=f"{opened['clue_id']}:left:{observation['end_ms']}",
                             reason=f"离开限制区 {zone_id}，停留结束：共 {dwell_ms} ms（此前已出 {opened['conclusion']} 线索 {opened['clue_id']}）")]
        if dwell_ms < config.dwell_ms:
            return []
        occurred = parse_utc(observation["occurred_at"])
        permission = resolve_permission(context, kinds=("zone_access",), seat_id=seat_id, zone_id=zone_id, occurred_at=occurred, person_id=person_id)
        opened = self._open.get(key)
        if opened is not None and opened.get("permission") == permission.status:
            return []
        since_ms = observation["start_ms"]
        clue_id = f"clue:S05:{RULE_ZONE}:{observation['source_id']}:{observation['media_run_id']}:zone:{zone_id}:{track_id}:{since_ms}:{permission.status}"
        common = dict(scene_id="S05", rule_id=RULE_ZONE, rule_version=rule_version, track_id=track_id, seat_id=seat_id, clue_id=clue_id,
                      start_ms=since_ms, end_ms=observation["end_ms"])
        if permission.status == "allowed":
            fact = permission.fact
            clue = new_clue(observation, conclusion="normal", risk_level="none", person_id=permission.person_id,
                            reason=f"在限制区 {zone_id} 停留 {dwell_ms} ms，但有有效区域权限 zone_access {fact.fact_id} v{fact.fact_version}", **common)
        elif permission.status in ("missing_fact", "missing_identity"):
            clue = new_clue(observation, conclusion="insufficient", risk_level="none", missing_facts=permission.missing,
                            reason="在限制区停留但无法核对区域权限（事实内容缺失或当事人身份未核验）", **common)
        else:
            clue = new_clue(observation, conclusion="suspected", risk_level="medium", person_id=permission.person_id,
                            reason=f"同源画面内轨迹在限制区 {zone_id} 停留 {dwell_ms} ms，超过阈值 {config.dwell_ms} ms，无适用的区域权限；本线索只证明单源内可见越界，不证明跨室", **common)
        self._open[key] = {"clue_id": clue_id, "since_ms": since_ms, "conclusion": clue["conclusion"], "permission": permission.status}
        return [clue]

    def _proximity(self, observation: dict, context: dict, config: CrossZoneConfig, rule_version: str) -> list[dict]:
        payload = observation.get("payload") or {}
        tracks = [t for t in (payload.get("tracks") or []) if isinstance(t, dict) and isinstance(t.get("track_id"), str)
                  and isinstance(t.get("bbox"), list) and len(t["bbox"]) == 4]
        now_ms = int(observation["start_ms"])
        source, run = observation["source_id"], observation["media_run_id"]
        close_pairs: set[tuple] = set()
        clues: list[dict] = []
        for i in range(len(tracks)):
            for j in range(i + 1, len(tracks)):
                a, b = sorted((tracks[i]["track_id"], tracks[j]["track_id"]))
                (ax, ay), (bx, by) = _center(tracks[i]["bbox"]), _center(tracks[j]["bbox"])
                if math.hypot(ax - bx, ay - by) > config.proximity_distance:
                    continue
                pair = (source, run, a, b)
                close_pairs.add(pair)
                state = self._pairs.setdefault(pair, {"since_ms": now_ms, "last_ms": now_ms})
                state["last_ms"] = now_ms
                if now_ms - state["since_ms"] < config.proximity_ms:
                    continue
                key = (source, run, f"pair:{a}:{b}")
                person_a, seat_a = _identity(context, a)
                person_b, seat_b = _identity(context, b)
                occurred = parse_utc(observation["occurred_at"])
                permission = resolve_permission(context, kinds=("contact_allowed",), seat_id=seat_a, zone_id=None, occurred_at=occurred, person_id=person_a)
                if permission.status != "allowed" and person_b:
                    permission_b = resolve_permission(context, kinds=("contact_allowed",), seat_id=seat_b, zone_id=None, occurred_at=occurred, person_id=person_b)
                    permission = permission_b if permission_b.status == "allowed" else permission
                status = permission.status if permission.status == "allowed" else ("missing_identity" if not (person_a and person_b) else permission.status)
                opened = self._open.get(key)
                if opened is not None and opened.get("permission") == status:
                    continue
                since = state["since_ms"]
                clue_id = f"clue:S05:{RULE_PROXIMITY}:{source}:{run}:pair:{a}:{b}:{since}:{status}"
                common = dict(scene_id="S05", rule_id=RULE_PROXIMITY, rule_version=rule_version, track_id=a, seat_id=seat_a, clue_id=clue_id,
                              start_ms=since, end_ms=now_ms)
                duration = now_ms - since
                if status == "allowed":
                    fact = permission.fact
                    clue = new_clue(observation, conclusion="normal", risk_level="none", person_id=person_a,
                                    reason=f"两轨迹接近停留 {duration} ms，但有有效接触许可 contact_allowed {fact.fact_id} v{fact.fact_version}", **common)
                elif status == "missing_fact":
                    clue = new_clue(observation, conclusion="insufficient", risk_level="none", missing_facts=permission.missing,
                                    reason="两轨迹接近停留，但快照引用的许可事实内容缺失", **common)
                elif status == "missing_identity":
                    clue = new_clue(observation, conclusion="insufficient", risk_level="none",
                                    missing_facts=["接触双方身份未经核验绑定（至少一方）", "接触许可无法核对"],
                                    reason=f"两轨迹接近停留 {duration} ms，但身份未核验，不能判断是否为非授权接触", **common)
                else:
                    clue = new_clue(observation, conclusion="suspected", risk_level="low", person_id=person_a,
                                    reason=f"同源画面内两名已核验人员接近停留 {duration} ms（{a} 与 {b}），无接触许可；需人工复核，不证明跨室", **common)
                self._open[key] = {"clue_id": clue_id, "since_ms": since, "conclusion": clue["conclusion"], "permission": status}
                clues.append(clue)
        for pair in [p for p in self._pairs if p[0] == source and p[1] == run and p not in close_pairs]:
            state = self._pairs.pop(pair)
            key = (source, run, f"pair:{pair[2]}:{pair[3]}")
            opened = self._open.pop(key, None)
            if opened is not None:
                clues.append(new_clue(observation, scene_id="S05", rule_id=RULE_PROXIMITY, rule_version=rule_version, conclusion="normal", risk_level="none",
                                      track_id=pair[2], start_ms=opened["since_ms"], end_ms=now_ms, clue_id=f"{opened['clue_id']}:apart:{now_ms}",
                                      reason=f"两轨迹分开，接近停留结束：共 {state['last_ms'] - opened['since_ms']} ms（此前已出 {opened['conclusion']} 线索 {opened['clue_id']}）"))
        return clues


def cross_room_protocol(observations_by_source: dict[str, list[dict]], context: dict) -> list[dict]:
    """跨室判定协议（未在真实多机位验证）。room_sources = {source_id: room_id}。
    不同 room 的源 < 2：每个（源, 轨迹）的限制区停留事实只出一条 insufficient 并列补采条件。
    ≥ 2：同一核验 person_id 在两个不同室的限制区都有停留事实、且第二室无 zone_access → suspected（protocol_fixture）。"""
    rule_version = _rule_version(context, "S05")
    room_sources = context.get("room_sources") or {}
    rooms = {room_sources.get(src) for src in observations_by_source if room_sources.get(src)}
    clues: list[dict] = []
    if len(rooms) < 2:
        seen: set[tuple] = set()
        for source, observations in observations_by_source.items():
            for obs in observations:
                if obs.get("type") not in ("zone_dwell_progress", "zone_entered") or obs.get("binding_status") != "bound":
                    continue
                track_id = (obs.get("payload") or {}).get("track_id")
                key = (source, track_id)
                if key in seen:
                    continue
                seen.add(key)
                person_id, seat_id = _identity(context, track_id)
                clues.append(new_clue(obs, scene_id="S05", rule_id=RULE_CROSS_ROOM, rule_version=rule_version, conclusion="insufficient", risk_level="none",
                                      track_id=track_id, seat_id=seat_id, missing_facts=list(CROSS_ROOM_MISSING),
                                      reason="只有一个评标室的视频，无法判断跨室；补采第二室机位或门禁记录后再判", quality_flags=["single_source"]))
        return clues
    by_person: dict[str, dict[str, dict]] = {}
    for source, observations in observations_by_source.items():
        room = room_sources.get(source)
        for obs in observations:
            if obs.get("type") not in ("zone_dwell_progress", "zone_entered") or obs.get("binding_status") != "bound" or not room:
                continue
            payload = obs.get("payload") or {}
            person_id, _seat = _identity(context, payload.get("track_id"))
            if not person_id:
                continue
            by_person.setdefault(person_id, {}).setdefault(room, obs)
    for person_id, rooms_seen in by_person.items():
        if len(rooms_seen) < 2:
            continue
        ordered = sorted(rooms_seen.items(), key=lambda item: parse_utc(item[1]["occurred_at"]))
        second_room, second_obs = ordered[-1]
        zone_id = (second_obs.get("payload") or {}).get("zone_id")
        occurred = parse_utc(second_obs["occurred_at"])
        ctx = dict(context, snapshot=dict(context.get("snapshot") or {}, room_id=second_room))
        permission = resolve_permission(ctx, kinds=("zone_access",), seat_id=None, zone_id=zone_id, occurred_at=occurred, person_id=person_id)
        if permission.status in ("missing_fact", "missing_identity"):
            clues.append(new_clue(second_obs, scene_id="S05", rule_id=RULE_CROSS_ROOM, rule_version=rule_version, conclusion="insufficient", risk_level="none",
                                  person_id=person_id, track_id=(second_obs.get("payload") or {}).get("track_id"), quality_flags=["protocol_fixture"],
                                  missing_facts=list(permission.missing) or ["第二室区域权限无法核对"],
                                  reason="核验人员先后出现在两个评标室的限制区，但第二室区域权限无法核对（事实内容缺失或身份未核验）"))
        elif permission.status == "allowed":
            clues.append(new_clue(second_obs, scene_id="S05", rule_id=RULE_CROSS_ROOM, rule_version=rule_version, conclusion="normal", risk_level="none",
                                  person_id=person_id, track_id=(second_obs.get("payload") or {}).get("track_id"), quality_flags=["protocol_fixture"],
                                  reason=f"核验人员先后出现在两个评标室的限制区，但第二室有有效区域权限 {permission.fact.fact_id} v{permission.fact.fact_version}"))
        else:
            clues.append(new_clue(second_obs, scene_id="S05", rule_id=RULE_CROSS_ROOM, rule_version=rule_version, conclusion="suspected", risk_level="medium",
                                  person_id=person_id, track_id=(second_obs.get("payload") or {}).get("track_id"), quality_flags=["protocol_fixture"],
                                  observation_ids=[o["observation_id"] for _r, o in ordered],
                                  reason="多源协议样例：同一核验人员先后出现在两个评标室的限制区，第二室无区域权限；本路径未在真实多机位验证"))
    return clues


_default = CrossZoneRules()


def evaluate(observations: list[dict], context: dict) -> list[dict]:
    return _default.evaluate(observations, context)


evaluate.input_types = INPUT_TYPES  # type: ignore[attr-defined]
