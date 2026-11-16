"""执行卡 7.4：S09 秩序冲突规则。evaluate(observations, context) -> list[RiskClue]。

输入：5.1 的 person_count（轨迹位置 → 快速位移 / 快速聚拢的视觉运动候选）与音频侧的 audio_order_candidate（威胁语句 / 声强突发）。
三态：威胁语句 × 视觉运动在 ±10 秒内重叠 → suspected medium；只有威胁语句 → suspected low（speaker_unverified，待听审）；
只有声强突发 → 不出（声音大不是冲突）；只有视觉运动 → insufficient（缺音频佐证与视觉复核）。说话人 A/B 不映射人员。
融合只在「同步来源」之间做：context["sync_groups"] = {source_id: 组名}，同场次同组的摄像头与麦克风才互相融合，
时间按 UTC 发生时间比较（各源 media_run_id / start_ms 各自独立，不能跨源比）；没配同步组的源只出单模态线索。
融合线索的 source_id 取触发它的那条观察（2.3 要求线索与触发观察同源），证据合并两路。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from worker.absence_rules import new_clue, parse_utc
from worker.device_rules import _rule_version

RULE_ID = "s09-order-conflict"
INPUT_TYPES = frozenset({"person_count", "audio_order_candidate"})
UNRELIABLE_FLAGS = frozenset({"camera_degraded", "camera_offline", "video_offline", "camera_blacked", "frame_error", "sampling_gap"})


@dataclass
class ConflictConfig:
    speed_threshold: float = 0.25      # 中心位移速度（归一化画面 / 秒）
    converge_drop: float = 0.15        # 两轨迹距离在窗口内下降超过此值视为快速聚拢
    converge_window_ms: int = 1000
    fuse_window_ms: int = 10000        # 音频候选与视觉候选的时间重叠窗口
    candidate_ttl_ms: int = 30000      # 候选保留时长；也是「只有视觉动作」段的静默间隔：静默超过它再动才算新的一段

    def __post_init__(self) -> None:
        if self.speed_threshold <= 0 or self.converge_drop <= 0 or self.converge_window_ms <= 0 or self.fuse_window_ms <= 0:
            raise ValueError("阈值非法")


def _center(bbox) -> tuple[float, float]:
    return (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0


class MotionDetector:
    """按轨迹记上一帧中心；按轨迹对记窗口内的距离。"""

    def __init__(self, config: ConflictConfig) -> None:
        self.config = config
        self._last: dict[str, tuple[int, tuple[float, float]]] = {}
        self._pair: dict[tuple, list[tuple[int, float]]] = {}

    def reset(self) -> None:
        self._last.clear()
        self._pair.clear()

    def feed(self, now_ms: int, tracks: list[dict]) -> list[dict]:
        """返回本帧的视觉运动候选 [{"kind": fast_move|converge, "track_ids", "value"}]。"""
        candidates: list[dict] = []
        centers = {}
        for track in tracks:
            bbox = track.get("bbox")
            if not isinstance(bbox, list) or len(bbox) != 4:
                continue
            centers[track["track_id"]] = _center(bbox)
        for track_id, center in centers.items():
            previous = self._last.get(track_id)
            if previous is not None and now_ms > previous[0]:
                dt = (now_ms - previous[0]) / 1000.0
                speed = math.hypot(center[0] - previous[1][0], center[1] - previous[1][1]) / dt
                if speed >= self.config.speed_threshold:
                    candidates.append({"kind": "fast_move", "track_ids": [track_id], "value": round(speed, 3)})
            self._last[track_id] = (now_ms, center)
        ids = sorted(centers)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                pair = (ids[i], ids[j])
                distance = math.hypot(centers[ids[i]][0] - centers[ids[j]][0], centers[ids[i]][1] - centers[ids[j]][1])
                history = self._pair.setdefault(pair, [])
                history.append((now_ms, distance))
                while history and now_ms - history[0][0] > self.config.converge_window_ms:
                    history.pop(0)
                if len(history) >= 2 and history[0][1] - distance >= self.config.converge_drop:
                    candidates.append({"kind": "converge", "track_ids": [ids[i], ids[j]], "value": round(history[0][1] - distance, 3)})
        for track_id in [t for t in self._last if t not in centers]:
            del self._last[track_id]
        for pair in [p for p in self._pair if p[0] not in centers or p[1] not in centers]:
            del self._pair[pair]          # 轨迹消失后配对历史不再有用；不清会随轨迹编号无限增长（5.01 复核）
        return candidates


class ConflictRules:
    input_types = INPUT_TYPES

    def __init__(self, config: Optional[ConflictConfig] = None) -> None:
        self.config = config or ConflictConfig()
        self._motion: dict[tuple, MotionDetector] = {}            # (camera source, run)
        self._visual: dict[tuple, list[dict]] = {}                 # (session, 同步组) -> 最近视觉候选 [{ms(UTC), observation, kinds}]
        self._audio: dict[tuple, list[dict]] = {}                  # (session, 同步组) -> 最近音频候选
        self._visual_segment: dict[tuple, int] = {}               # (source, run) -> 最近一次视觉动作的 UTC 毫秒
        self._emitted: set[str] = set()
        self._runs: dict[str, str] = {}

    def __call__(self, observations: list[dict], context: dict) -> list[dict]:
        return self.evaluate(observations, context)

    def reset(self, source_id: Optional[str] = None) -> None:
        if source_id is None:
            self._motion.clear(); self._visual.clear(); self._audio.clear(); self._visual_segment.clear(); self._emitted.clear(); self._runs.clear()
            return
        for store in (self._motion, self._visual_segment):
            for key in [k for k in store if k[0] == source_id]:
                del store[key]
        for store in (self._visual, self._audio):                  # 该源上一轮的候选不能再参与融合
            for key in list(store):
                store[key] = [c for c in store[key] if c["observation"].get("source_id") != source_id]
        self._runs.pop(source_id, None)

    def evaluate(self, observations: list[dict], context: dict) -> list[dict]:
        config = context.get("config")
        config = config if isinstance(config, ConflictConfig) else self.config     # 派发器把同一 context 交给各场景
        rule_version = _rule_version(context, "S09")
        groups = context.get("sync_groups") or {}
        clues: list[dict] = []
        for observation in observations:
            if not isinstance(observation, dict) or observation.get("type") not in INPUT_TYPES:
                continue
            source, run = observation.get("source_id"), observation.get("media_run_id")
            if self._runs.get(source) not in (None, run):
                self.reset(source)
            self._runs[source] = run
            if observation.get("binding_status") != "bound":
                key = f"unbound:{source}:{run}:{observation['type']}"
                if key in self._emitted:
                    continue
                self._emitted.add(key)
                clues.append(new_clue(observation, scene_id="S09", rule_id=RULE_ID, rule_version=rule_version, conclusion="insufficient", risk_level="none",
                                      reason="场次 / 阶段未绑定，只保留技术事实", missing_facts=["场次 / 阶段未绑定"]))
                continue
            if set(observation.get("quality_flags") or []) & UNRELIABLE_FLAGS:
                continue
            if observation["type"] == "person_count":
                clues.extend(self._visual_side(observation, config, rule_version, groups))
            else:
                clues.extend(self._audio_side(observation, config, rule_version, groups))
        return clues

    # ---- 内部 ----
    def _bucket(self, observation: dict, groups: dict) -> tuple:
        """同场次同同步组才能融合；没配组的源自成一组（只能单模态）。"""
        source = observation.get("source_id")
        return (observation.get("session_id"), groups.get(source) or f"solo:{source}")

    def _prune(self, store: dict, key: tuple, now_ms: int, config: ConflictConfig) -> list[dict]:
        items = [c for c in store.get(key, []) if abs(now_ms - c["ms"]) <= config.candidate_ttl_ms]
        store[key] = items
        return items

    def _visual_side(self, observation: dict, config: ConflictConfig, rule_version: str, groups: dict) -> list[dict]:
        payload = observation.get("payload") or {}
        tracks = [t for t in (payload.get("tracks") or []) if isinstance(t, dict) and isinstance(t.get("track_id"), str)]
        key = (observation["source_id"], observation["media_run_id"])
        detector = self._motion.setdefault(key, MotionDetector(config))
        candidates = detector.feed(int(observation["start_ms"]), tracks)   # 运动按本源媒体时间算
        if not candidates:
            return []
        now_ms = _utc_ms(observation)                                     # 融合按 UTC 发生时间比
        bucket = self._bucket(observation, groups)
        visual = self._prune(self._visual, bucket, now_ms, config)
        entry = {"ms": now_ms, "observation": observation, "kinds": sorted({c["kind"] for c in candidates}),
                 "track_ids": sorted({t for c in candidates for t in c["track_ids"]})}
        visual.append(entry)
        last_motion = self._visual_segment.get(key)
        self._visual_segment[key] = now_ms
        audio = [a for a in self._prune(self._audio, bucket, now_ms, config) if a["kind"] == "threat_phrase" and abs(a["ms"] - now_ms) <= config.fuse_window_ms]
        if audio:
            return self._fused(observation, audio[-1], entry, rule_version)
        if last_motion is not None and now_ms - last_motion <= config.candidate_ttl_ms:
            return []                                                     # 同一段快速动作只出一条 insufficient
        clue_id = f"clue:S09:{RULE_ID}:{observation['source_id']}:{observation['media_run_id']}:visual:{observation['start_ms']}"
        return [new_clue(observation, scene_id="S09", rule_id=RULE_ID, rule_version=rule_version, conclusion="insufficient", risk_level="none",
                         track_id=entry["track_ids"][0], clue_id=clue_id,
                         missing_facts=["同步音频中的威胁 / 争吵语句", "推搡动作的视觉复核（人工看片）"],
                         reason=f"画面内出现快速动作（{', '.join(entry['kinds'])}），但没有同步音频佐证，不能认定冲突")]

    def _audio_side(self, observation: dict, config: ConflictConfig, rule_version: str, groups: dict) -> list[dict]:
        payload = observation.get("payload") or {}
        kind = payload.get("kind")
        now_ms = _utc_ms(observation)
        bucket = self._bucket(observation, groups)
        audio = self._prune(self._audio, bucket, now_ms, config)
        entry = {"ms": now_ms, "observation": observation, "kind": kind, "speaker": payload.get("speaker"), "quotes": list(payload.get("quotes") or [])}
        audio.append(entry)
        if kind != "threat_phrase":
            return []                                              # 声强突发单独不出线索：声音大不是冲突
        visual = [v for v in self._prune(self._visual, bucket, now_ms, config) if abs(v["ms"] - now_ms) <= config.fuse_window_ms]
        if visual:
            return self._fused(visual[-1]["observation"], entry, visual[-1], rule_version, trigger=observation)
        clue_id = f"clue:S09:{RULE_ID}:{observation['source_id']}:{observation['media_run_id']}:audio:{observation['observation_id']}"
        if clue_id in self._emitted:
            return []
        self._emitted.add(clue_id)
        quotes = "、".join(f"「{q}」" for q in entry["quotes"]) or "（无引用）"
        return [new_clue(observation, scene_id="S09", rule_id=RULE_ID, rule_version=rule_version, conclusion="suspected", risk_level="low",
                         clue_id=clue_id, quality_flags=["speaker_unverified"],
                         reason=f"转写句段出现威胁性表达 {quotes}（说话人标签 {entry['speaker'] or '未知'}，未映射到人员，需人工听审）；无同步视觉候选，不融合")]

    def _fused(self, visual_obs: dict, audio_entry: dict, visual_entry: dict, rule_version: str, *, trigger: Optional[dict] = None) -> list[dict]:
        base = trigger or visual_obs                # 线索与触发观察同源（2.3 校验要求）
        audio_obs = audio_entry["observation"]
        fuse_key = f"fused:{audio_obs['observation_id']}"
        if fuse_key in self._emitted:
            return []                               # 一条威胁语句只融合一次：同一次冲突的多帧快速动作不重复出 medium
        self._emitted.add(fuse_key)
        clue_id = f"clue:S09:{RULE_ID}:{base['source_id']}:{base['media_run_id']}:fused:{audio_obs['observation_id']}"
        quotes = "、".join(f"「{q}」" for q in audio_entry["quotes"]) or "（无引用）"
        clue = new_clue(base, scene_id="S09", rule_id=RULE_ID, rule_version=rule_version, conclusion="suspected", risk_level="medium",
                        clue_id=clue_id, track_id=visual_entry["track_ids"][0],
                        observation_ids=[visual_obs["observation_id"], audio_obs["observation_id"]],
                        quality_flags=["speaker_unverified"], start_ms=base["start_ms"],
                        reason=f"同步时段内：转写出现威胁性表达 {quotes} 且画面出现快速动作（{', '.join(visual_entry['kinds'])}）；说话人标签 {audio_entry['speaker'] or '未知'} 未映射到人员，责任人待人工核验")
        refs = [*(visual_obs.get("evidence_refs") or []), *(audio_obs.get("evidence_refs") or [])]
        clue["evidence_refs"] = list({r.get("evidence_id"): r for r in refs if isinstance(r, dict)}.values())
        return [clue]


def _utc_ms(observation: dict) -> int:
    return int(parse_utc(observation["occurred_at"]).timestamp() * 1000)


_default = ConflictRules()


def evaluate(observations: list[dict], context: dict) -> list[dict]:
    return _default.evaluate(observations, context)


evaluate.input_types = INPUT_TYPES  # type: ignore[attr-defined]
