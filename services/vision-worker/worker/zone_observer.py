"""执行卡 5.1：把每帧人框变成契约 2.0.0 的区域事实观察。

事实只有：人数、工位占用 / 空置（含空置进度）、限制区进出 / 停留进度、出入口跨线。不说违规。
占用看人框与工位多边形的重叠比例（坐姿脚点常在 ROI 外），状态翻转要滞回确认；时长全部按媒体时间（start_ms）；
入口传入 5.3 的故障标记或采样中断时暂停所有计时（工位与限制区同构）并打标记，故障帧不喂跟踪器、不翻转状态、
清掉待定；新媒体轮次全部清零。状态起点取「第一次观察到变化」的时刻（待定开始），不是确认时刻。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from worker.person_tracking import IouTracker, Track
from worker.stage_health import SCOPE_FIELDS, SNAPSHOT_FIELDS, FrameContext
from worker.zone_geometry import (
    bbox_center, bbox_polygon_overlap_ratio, point_in_polygon, segment_side, segments_intersect, validate_bbox, validate_point,
    validate_polygon,
)

CONTRACT_VERSION = "2.0.0"
MODEL_NAME = "zone_observer"
MODEL_VERSION = "5.1-v1"
DEGRADED_FLAGS = frozenset({"camera_degraded", "camera_offline", "video_offline", "camera_blacked", "frame_error"})


@dataclass
class ZoneObserverConfig:
    """全部是工程初值（执行卡:81）；现场调参走尝试台账。"""
    occupancy_overlap: float = 0.3      # 人框与工位多边形的重叠面积 / min(人框面积, 工位面积) ≥ 此值视为占用
    occupy_confirm_ms: int = 500        # 占用持续多久才确认
    vacate_confirm_ms: int = 1000       # 空置持续多久才确认
    progress_interval_ms: int = 1000    # 空置 / 停留进度心跳
    count_interval_ms: int = 1000       # 人数心跳
    max_sampling_gap_ms: int = 2000     # 相邻帧媒体时间差超过此值视为采样中断
    zone_confirm_ms: int = 500          # 限制区进入 / 离开确认
    min_confidence: float = 0.25
    person_label: str = "person"
    iou_threshold: float = 0.3
    max_missed_ms: int = 2000
    degraded_flags: frozenset = DEGRADED_FLAGS

    def __post_init__(self) -> None:
        if not 0.0 < self.occupancy_overlap <= 1.0:
            raise ValueError("occupancy_overlap 必须在 (0,1]")
        for name in ("occupy_confirm_ms", "vacate_confirm_ms", "progress_interval_ms", "count_interval_ms", "max_sampling_gap_ms", "zone_confirm_ms"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} 不能为负")


# ---------------------------------------------------------------- 区域配置（3.3 协议）
@dataclass(frozen=True)
class SeatZone:
    zone_id: str
    name: str
    seat_id: str
    polygon: tuple


@dataclass(frozen=True)
class RestrictedZone:
    zone_id: str
    name: str
    polygon: tuple


@dataclass(frozen=True)
class EntryLine:
    line_id: str
    name: str
    start: tuple
    end: tuple
    direction: str


@dataclass(frozen=True)
class ZoneConfig:
    config_version: int
    seats: tuple
    restricted: tuple
    lines: tuple

    @classmethod
    def from_dict(cls, data: dict) -> "ZoneConfig":
        if not isinstance(data, dict):
            raise ValueError("区域配置必须是对象")
        version = data.get("config_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise ValueError("config_version 必须是非负整数")
        seats, restricted, ids, seat_ids = [], [], set(), set()
        for zone in data.get("zones") or []:
            if not isinstance(zone, dict) or not _text(zone.get("zone_id")) or not _text(zone.get("name"), 128):
                raise ValueError("区域缺 zone_id / name")
            if zone["zone_id"] in ids:
                raise ValueError(f"区域 id 重复 {zone['zone_id']}")
            ids.add(zone["zone_id"])
            polygon = tuple(validate_polygon(zone.get("polygon") or []))
            kind = zone.get("kind")
            if kind == "seat":
                if not _text(zone.get("seat_id")):
                    raise ValueError(f"工位区 {zone['zone_id']} 必须关联 seat_id")
                if zone["seat_id"] in seat_ids:
                    raise ValueError(f"工位 {zone['seat_id']} 被多个区域关联")
                seat_ids.add(zone["seat_id"])
                seats.append(SeatZone(zone["zone_id"], zone["name"], zone["seat_id"], polygon))
            elif kind == "restricted":
                if zone.get("seat_id") is not None:
                    raise ValueError("限制区不关联工位")
                restricted.append(RestrictedZone(zone["zone_id"], zone["name"], polygon))
            else:
                raise ValueError(f"未知区域类型 {kind}")
        lines = []
        for line in data.get("entry_lines") or []:
            if not isinstance(line, dict) or not _text(line.get("line_id")) or not _text(line.get("name"), 128):
                raise ValueError("出入口线缺 line_id / name")
            if line["line_id"] in ids:
                raise ValueError(f"出入口线 id 与区域重复 {line['line_id']}")
            ids.add(line["line_id"])
            start, end = validate_point(line.get("start") or ()), validate_point(line.get("end") or ())
            if start == end:
                raise ValueError("出入口线起点终点不能重合")
            direction = line.get("direction", "both")
            if direction not in ("in", "out", "both"):
                raise ValueError("direction 只能是 in / out / both")
            lines.append(EntryLine(line["line_id"], line["name"], start, end, direction))
        return cls(version, tuple(seats), tuple(restricted), tuple(lines))


def _text(value, limit: int = 64) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def normalize_detections(detections: list[dict], frame_width: int, frame_height: int) -> list[dict]:
    """入口用：像素 bbox → 归一化，超出画面的裁到边界。"""
    if frame_width <= 0 or frame_height <= 0:
        raise ValueError("帧宽高必须为正")
    out = []
    for det in detections:
        x1, y1, x2, y2 = det["bbox"]
        out.append({**det, "bbox": [min(max(x1 / frame_width, 0.0), 1.0), min(max(y1 / frame_height, 0.0), 1.0),
                                     min(max(x2 / frame_width, 0.0), 1.0), min(max(y2 / frame_height, 0.0), 1.0)],
                    "label": det.get("label", det.get("class", "")), "confidence": float(det.get("confidence", det.get("conf", 0.0)))})
    return out


# ---------------------------------------------------------------- 状态
@dataclass
class _Timer:
    """持续状态计时：active 期间按媒体时间累积，paused 期间不累积。"""
    since_ms: int
    accumulated_ms: int = 0
    last_ms: Optional[int] = None
    paused_ms: int = 0
    last_report_ms: Optional[int] = None

    def advance(self, now_ms: int, paused: bool) -> int:
        if self.last_ms is not None and now_ms > self.last_ms:
            delta = now_ms - self.last_ms
            if paused:
                self.paused_ms += delta
            else:
                self.accumulated_ms += delta
        self.last_ms = now_ms
        return self.accumulated_ms


@dataclass
class _SeatState:
    occupied: bool = False
    pending_state: Optional[bool] = None
    pending_since_ms: Optional[int] = None
    timer: Optional[_Timer] = None
    track_ids: list = field(default_factory=list)


@dataclass
class _ZoneState:
    inside: bool = False
    pending_state: Optional[bool] = None
    pending_since_ms: Optional[int] = None
    timer: Optional[_Timer] = None
    last_seen_ms: Optional[int] = None


class ZoneObserver:
    """一个源一个实例。update(ctx, detections, quality_flags) -> list[Observation]。"""

    def __init__(self, zone_config: dict, config: Optional[ZoneObserverConfig] = None) -> None:
        self.zones = ZoneConfig.from_dict(zone_config)
        self.config = config or ZoneObserverConfig()
        self._source_id: Optional[str] = None
        self._media_run_id: Optional[str] = None
        self._tracker: Optional[IouTracker] = None
        self._seats: dict[str, _SeatState] = {}
        self._zone_tracks: dict[tuple[str, str], _ZoneState] = {}
        self._line_sides: dict[tuple[str, str], tuple[int, tuple]] = {}     # (line, track) -> (最近非零侧别, 当时中心)
        self._last_ms: Optional[int] = None
        self._same_ms = 0
        self._last_count: Optional[int] = None
        self._last_count_report_ms: Optional[int] = None
        self._frames = 0

    # ---- 对外 ----
    def reset(self, media_run_id: str) -> None:
        self._media_run_id = media_run_id
        self._tracker = IouTracker(media_run_id, iou_threshold=self.config.iou_threshold, max_missed_ms=self.config.max_missed_ms)
        self._seats = {seat.zone_id: _SeatState() for seat in self.zones.seats}
        self._zone_tracks = {}
        self._line_sides = {}
        self._last_ms = None
        self._same_ms = 0
        self._last_count = None
        self._last_count_report_ms = None

    def update(self, ctx: FrameContext, detections: Optional[list], quality_flags: Iterable[str] = ()) -> list[dict]:
        if self._source_id is not None and ctx.source_id != self._source_id:
            raise ValueError(f"一个实例只跟一个源：已是 {self._source_id}，收到 {ctx.source_id}")
        self._source_id = ctx.source_id
        if self._tracker is None or ctx.media_run_id != self._media_run_id:
            self.reset(ctx.media_run_id)
        now = ctx.start_ms
        if self._last_ms is not None and now < self._last_ms:
            raise ValueError("媒体时间倒退；新轮次请换 media_run_id")
        self._same_ms = self._same_ms + 1 if now == self._last_ms else 0
        flags = list(dict.fromkeys(f for f in quality_flags if f))
        degraded = any(f in self.config.degraded_flags for f in flags)
        if self._last_ms is not None and now - self._last_ms > self.config.max_sampling_gap_ms:
            flags.append("sampling_gap")
        paused = degraded or "sampling_gap" in flags
        persons, invalid = self._filter(detections or [])
        if invalid:
            flags.append("detection_invalid")
        if degraded:
            self._tracker.pause(now)                 # 故障帧的检测不可信：不喂跟踪器，轨迹不计丢失
            tracks: list[Track] = []
        else:
            tracks = self._tracker.update(persons, now)
        self._frames += 1
        observations: list[dict] = []
        observations.extend(self._count(ctx, tracks, now, flags))
        observations.extend(self._seat_facts(ctx, tracks, now, flags, paused, degraded))
        observations.extend(self._restricted_facts(ctx, tracks, now, flags, paused, degraded))
        if not degraded:
            observations.extend(self._line_facts(ctx, tracks, now, flags))
        self._last_ms = now
        return observations

    def status(self) -> dict:
        return {
            "source_id": self._source_id, "media_run_id": self._media_run_id, "config_version": self.zones.config_version,
            "frames": self._frames, "tracks": [t.to_dict() for t in (self._tracker.all_tracks() if self._tracker else [])],
            "seats": {zone_id: {"occupied": s.occupied, "track_ids": list(s.track_ids),
                                "vacant_ms": (s.timer.accumulated_ms if (s.timer and not s.occupied) else 0)}
                      for zone_id, s in self._seats.items()},
        }

    # ---- 内部：输入 ----
    def _filter(self, detections: list) -> tuple[list[dict], int]:
        persons, invalid = [], 0
        for det in detections:
            try:
                if not isinstance(det, dict):
                    raise ValueError("检测必须是对象")
                label = det.get("label", det.get("class"))
                confidence = det.get("confidence", det.get("conf"))
                if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
                    raise ValueError("confidence 非法")
                bbox = validate_bbox(det["bbox"])
            except (ValueError, KeyError, TypeError):
                invalid += 1
                continue
            if label != self.config.person_label or confidence < self.config.min_confidence:
                continue
            persons.append({"bbox": list(bbox), "label": label, "confidence": float(confidence)})
        return persons, invalid

    # ---- 内部：事实 ----
    def _count(self, ctx: FrameContext, tracks: list[Track], now: int, flags: list[str]) -> list[dict]:
        count = len(tracks)
        due = self._last_count_report_ms is None or now - self._last_count_report_ms >= self.config.count_interval_ms
        if count == self._last_count and not due:
            return []
        self._last_count = count
        self._last_count_report_ms = now
        payload = {"count": count, "tracks": [t.to_dict() for t in tracks], "track_ids": [t.track_id for t in tracks],
                   "config_version": self.zones.config_version}
        return [self._observation(ctx, "person_count", "all", payload, now, now, flags)]

    def _seat_facts(self, ctx: FrameContext, tracks: list[Track], now: int, flags: list[str], paused: bool, degraded: bool) -> list[dict]:
        out: list[dict] = []
        for seat in self.zones.seats:
            state = self._seats[seat.zone_id]
            occupants = [] if degraded else [t.track_id for t in tracks
                                              if bbox_polygon_overlap_ratio(t.bbox, seat.polygon) >= self.config.occupancy_overlap]
            if state.timer is None:
                state.timer = _Timer(since_ms=now, last_ms=now)
                if occupants:
                    state.occupied, state.track_ids = True, occupants
            elapsed = state.timer.advance(now, paused)
            if paused:
                state.pending_state, state.pending_since_ms = None, None     # 不可信时段不计入滞回，故障后重新确认
            else:
                observed = bool(occupants)
                if observed != state.occupied:
                    if state.pending_state != observed:
                        state.pending_state, state.pending_since_ms = observed, now
                    confirm = self.config.occupy_confirm_ms if observed else self.config.vacate_confirm_ms
                    if now - state.pending_since_ms >= confirm:
                        out.append(self._flip_seat(ctx, seat, state, observed, occupants, now, flags))
                        continue
                else:
                    state.pending_state, state.pending_since_ms = None, None
                    if observed:
                        state.track_ids = occupants
            if not state.occupied and state.timer is not None:
                last = state.timer.last_report_ms
                if last is None or now - last >= self.config.progress_interval_ms:
                    state.timer.last_report_ms = now
                    payload = {"seat_id": seat.seat_id, "zone_id": seat.zone_id, "vacant_since_ms": state.timer.since_ms,
                               "vacant_ms": elapsed, "paused_ms": state.timer.paused_ms, "paused": paused,
                               "track_ids": [], "config_version": self.zones.config_version}
                    out.append(self._observation(ctx, "seat_vacancy_progress", seat.zone_id, payload, state.timer.since_ms, now, flags))
        return out

    def _flip_seat(self, ctx: FrameContext, seat: SeatZone, state: _SeatState, occupied: bool, occupants: list[str], now: int, flags: list[str]) -> dict:
        """状态起点 = 待定开始时刻（第一次观察到变化），滞回期间的时长算进新状态。"""
        since = state.pending_since_ms if state.pending_since_ms is not None else now
        previous = state.timer
        if previous is not None and since > previous.since_ms:
            # 旧状态在 since 结束：把滞回期间已累进旧计时的部分退回去
            previous.accumulated_ms = max(0, previous.accumulated_ms - (now - since))
        state.occupied = occupied
        state.pending_state, state.pending_since_ms = None, None
        state.track_ids = occupants if occupied else []
        state.timer = _Timer(since_ms=since, accumulated_ms=now - since, last_ms=now)
        if occupied:
            payload = {"seat_id": seat.seat_id, "zone_id": seat.zone_id, "occupied_since_ms": since,
                       "vacant_ms": previous.accumulated_ms if previous else 0, "track_ids": occupants,
                       "config_version": self.zones.config_version}
            return self._observation(ctx, "seat_occupied", seat.zone_id, payload, previous.since_ms if previous else since, now, flags)
        payload = {"seat_id": seat.seat_id, "zone_id": seat.zone_id, "vacant_since_ms": since, "vacant_ms": now - since, "paused_ms": 0,
                   "paused": False, "track_ids": [], "config_version": self.zones.config_version}
        state.timer.last_report_ms = now
        return self._observation(ctx, "seat_vacant", seat.zone_id, payload, since, now, flags)

    def _restricted_facts(self, ctx: FrameContext, tracks: list[Track], now: int, flags: list[str], paused: bool, degraded: bool) -> list[dict]:
        out: list[dict] = []
        live = {t.track_id: t for t in tracks}
        if degraded:
            for state in self._zone_tracks.values():          # 故障帧：区内计时一律暂停、待定清零，与工位同构
                if state.inside and state.timer is not None:
                    state.timer.advance(now, True)
                state.pending_state, state.pending_since_ms = None, None
            return out
        for zone in self.zones.restricted:
            for track in tracks:
                key = (zone.zone_id, track.track_id)
                inside = point_in_polygon(bbox_center(track.bbox), zone.polygon)
                state = self._zone_tracks.setdefault(key, _ZoneState())
                state.last_seen_ms = now
                if paused:
                    state.pending_state, state.pending_since_ms = None, None
                elif inside != state.inside:
                    if state.pending_state != inside:
                        state.pending_state, state.pending_since_ms = inside, now
                    if now - state.pending_since_ms >= self.config.zone_confirm_ms:
                        since = state.pending_since_ms
                        state.inside, state.pending_state, state.pending_since_ms = inside, None, None
                        if inside:
                            state.timer = _Timer(since_ms=since, accumulated_ms=now - since, last_ms=now, last_report_ms=now)
                            out.append(self._observation(ctx, "zone_entered", f"{zone.zone_id}:{track.track_id}",
                                                         {"zone_id": zone.zone_id, "track_id": track.track_id, "entered_since_ms": since,
                                                          "track_ids": [track.track_id], "config_version": self.zones.config_version}, since, now, flags))
                            continue
                        out.append(self._zone_left(ctx, zone.zone_id, track.track_id, state, now, flags, since_end=since))
                        continue
                else:
                    state.pending_state, state.pending_since_ms = None, None
                if state.inside and state.timer is not None:
                    dwell = state.timer.advance(now, paused)
                    if now - (state.timer.last_report_ms or now) >= self.config.progress_interval_ms:
                        state.timer.last_report_ms = now
                        out.append(self._observation(ctx, "zone_dwell_progress", f"{zone.zone_id}:{track.track_id}",
                                                     {"zone_id": zone.zone_id, "track_id": track.track_id, "dwell_ms": dwell,
                                                      "paused_ms": state.timer.paused_ms, "track_ids": [track.track_id],
                                                      "config_version": self.zones.config_version}, state.timer.since_ms, now, flags))
        # 轨迹丢失：区内的补一条带 track_lost 的离开事实后清掉；区外 / 待定的直接清掉
        for key in [k for k in self._zone_tracks if k[1] not in live]:
            state = self._zone_tracks[key]
            lost_for = now - (state.last_seen_ms if state.last_seen_ms is not None else now)
            if lost_for <= self.config.max_missed_ms:
                continue
            if state.inside and state.timer is not None:
                out.append(self._zone_left(ctx, key[0], key[1], state, now, [*flags, "track_lost"], since_end=state.last_seen_ms))
            del self._zone_tracks[key]
        return out

    def _zone_left(self, ctx: FrameContext, zone_id: str, track_id: str, state: _ZoneState, now: int, flags: list[str], *, since_end: int) -> dict:
        timer = state.timer
        dwell = max(0, timer.accumulated_ms - max(0, (timer.last_ms or since_end) - since_end)) if timer else 0   # 截到离开 / 最后可见时刻
        started = timer.since_ms if timer else since_end
        state.inside, state.timer, state.pending_state, state.pending_since_ms = False, None, None, None
        return self._observation(ctx, "zone_left", f"{zone_id}:{track_id}",
                                 {"zone_id": zone_id, "track_id": track_id, "dwell_ms": dwell, "left_at_ms": since_end,
                                  "track_ids": [track_id], "config_version": self.zones.config_version}, started, now, flags)

    def _line_facts(self, ctx: FrameContext, tracks: list[Track], now: int, flags: list[str]) -> list[dict]:
        """按 (门线, 轨迹) 记最近一次非零侧别；中心恰落线上沿用上一侧；只在严格变号且与线段相交时报一次。"""
        out: list[dict] = []
        for track in tracks:
            center = track.center
            for line in self.zones.lines:
                key = (line.line_id, track.track_id)
                side = segment_side(center, line.start, line.end)
                if side == 0:
                    continue
                previous = self._line_sides.get(key)
                self._line_sides[key] = (side, center)
                if previous is None or previous[0] == side:
                    continue
                if not segments_intersect(previous[1], center, line.start, line.end):
                    continue
                direction = "in" if side > 0 else "out"
                if line.direction != "both" and direction != line.direction:
                    continue
                out.append(self._observation(ctx, "line_crossed", f"{line.line_id}:{track.track_id}",
                                             {"line_id": line.line_id, "track_id": track.track_id, "direction": direction,
                                              "track_ids": [track.track_id], "config_version": self.zones.config_version},
                                             track.history[-2][0] if len(track.history) >= 2 else now, now, flags))
        live = {t.track_id for t in tracks}
        for key in [k for k in self._line_sides if k[1] not in live and k[1] not in {t.track_id for t in self._tracker.all_tracks()}]:
            del self._line_sides[key]
        return out

    # ---- 内部：契约观察 ----
    def _observation(self, ctx: FrameContext, obs_type: str, subject: str, payload: dict, start_ms: int, end_ms: int, flags: list[str]) -> dict:
        bound = ctx.bound
        quality = list(dict.fromkeys(flags))
        if not bound and "binding_missing" not in quality:
            quality.append("binding_missing")
        start_ms = min(int(start_ms), int(end_ms))
        suffix = f":{self._same_ms}" if self._same_ms else ""
        observation_id = f"{ctx.source_id}:{ctx.media_run_id}:{obs_type}:{subject}:{end_ms}{suffix}"
        scope = {k: getattr(ctx, k) for k in SCOPE_FIELDS}
        evidence = {
            "contract_version": CONTRACT_VERSION, **scope, "source_id": ctx.source_id,
            "evidence_id": f"{observation_id}:video", "media_run_id": ctx.media_run_id, "asset_id": ctx.asset_id,
            "evidence_type": "video", "start_ms": start_ms, "end_ms": end_ms, "sha256": None, "status": "pending",
        }
        return {
            "contract_version": CONTRACT_VERSION, **scope, "source_id": ctx.source_id,
            "stage_snapshot_id": ctx.stage_snapshot_id if bound else None,
            "phase_id": ctx.phase_id if bound else None,
            "phase_version": ctx.phase_version if bound else None,
            "observation_id": observation_id, "binding_status": "bound" if bound else "unbound",
            "occurred_at": ctx.occurred_at, "media_run_id": ctx.media_run_id, "asset_id": ctx.asset_id,
            "start_ms": start_ms, "end_ms": int(end_ms), "type": obs_type, "payload": payload,
            "model_name": MODEL_NAME, "model_version": MODEL_VERSION, "quality_flags": quality, "evidence_refs": [evidence],
        }


__all__ = ["ZoneObserver", "ZoneObserverConfig", "ZoneConfig", "normalize_detections", "SNAPSHOT_FIELDS"]
