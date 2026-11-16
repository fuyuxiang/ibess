"""5.01：A 侧补充能力接入 B 的 vision-worker 主循环的唯一入口。

B 的链路是唯一主链路，这里只做两件事（D-30、D-32）：
1. 给 B 的 person_detected payload 追加 B 没有的键（inside / zone_name 供 B 的 zone_intrusion 算子直接使用，a_* 键只给人看）；
2. 把 A 的规则线索变成「A 场景事件」，由主循环以观察类型 a_scene_event 发给 event-service。
与 B 判同一件事的 A 规则（7.2 phone_rules / device_rules、7.3 拍摄分支、5.2 越界分支与 S05 限制区停留（都由 B 的 zone_intrusion 判）、
5.3 断流与黑屏、7.4 order_rules）
不在这里调用，以 B 为准（D-33）。三态：suspected 出事件、insufficient 出 low 事件并以「证据不足：」开头、normal 不出（D-34）。
已接入的能力默认打开（D-34），各开关设 0 关闭；A_EXT_ENABLED=0 时 from_env 返回 None，主循环与改动前完全一样。
"""
from __future__ import annotations

import logging
import os

import cv2
import queue
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Optional

from worker.a_context import CachedContext, DbContextSource, RuleContext, utc_iso
from worker.absence_rules import RULE_SEAT, AbsenceRules
from worker.conflict_rules import RULE_ID as RULE_S09, ConflictRules
from worker.cross_zone_rules import RULE_PROXIMITY, CrossZoneRules
from worker.document_rules import RULE_CARRY, DocumentRules
from worker.object_candidates import CandidateDeduper, extract_candidates
from worker.stage_health import FrameContext, StageHealthConfig, StageHealthMonitor
from worker.absence_rules import new_clue
from worker.zone_geometry import bbox_center, point_in_polygon
from worker.zone_observer import ZoneObserver, normalize_detections

log = logging.getLogger("vision-worker.a_ext")

A_EVENT_TYPE = "a_scene_event"
SWITCHES = ("A_ZONES_ENABLED", "A_S03_SEAT_ENABLED", "A_S05_ENABLED", "A_S09_MOTION_ENABLED",
            "A_HEALTH_ENABLED", "A_VISUAL_REVIEW_ENABLED", "A_S08_CARRY_ENABLED")
# (场景, 规则) → B 的事件类型；前端中文标签在 web-console/src/utils/ui.ts
EVENT_TYPES = {
    ("S03", RULE_SEAT): "seat_vacancy",
    ("S05", RULE_PROXIMITY): "close_contact",
    ("S08", RULE_CARRY): "document_carry_out",
    ("S09", RULE_S09): "order_motion",
}
HEALTH_EVENTS = {"shifted": "camera_shifted", "dark": "video_quality_degraded", "bright": "video_quality_degraded",
                 "blurred": "video_quality_degraded"}
HEALTH_REASONS = {"shifted": "镜头偏移", "dark": "画面过暗", "bright": "画面过曝", "blurred": "画面失焦"}
HEALTH_RULE = "s10-a-quality"
ZONE_TYPES = frozenset({"seat_vacancy_progress", "seat_occupied", "zone_dwell_progress", "zone_left", "person_count", "line_crossed"})
SEAT_TYPES = frozenset({"seat_vacancy_progress", "seat_occupied"})
UNBOUND_ONLY = "场次 / 阶段未绑定"


def _on(env: dict, name: str) -> bool:
    return str(env.get(name, "1")).strip().lower() not in ("0", "false", "no", "off")


def clue_to_event(clue: dict, event_type: str) -> Optional[dict]:
    """三态映射（D-34）。返回 a_scene_event 的 payload；normal 返回 None。"""
    conclusion = clue.get("conclusion")
    if conclusion == "suspected":
        risk, reason = clue["risk_level"], clue["reason"]
    elif conclusion == "insufficient":
        missing = "；".join(clue.get("missing_facts") or [])
        risk, reason = "low", f"证据不足：{clue['reason']}" + (f"（缺：{missing}）" if missing else "")
    else:
        return None
    return {"scene_id": clue["scene_id"], "event_type": event_type, "risk_level": risk, "reason": reason[:1000],
            "conclusion": conclusion, "clue": clue}


class _ReviewWorker:
    """7.1 的后台复核：主循环只投递，结果下一帧取走；队列满就丢，不阻塞检测。"""

    def __init__(self, reviewer, maxsize: int = 4) -> None:
        self.reviewer = reviewer
        self.inbox: queue.Queue = queue.Queue(maxsize=maxsize)
        self.outbox: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="a-visual-review", daemon=True)
        self._thread.start()

    def submit(self, request, ctx) -> bool:
        try:
            self.inbox.put_nowait((request, ctx))
            return True
        except queue.Full:
            return False

    def drain(self) -> list[dict]:
        out = []
        while True:
            try:
                out.append(self.outbox.get_nowait())
            except queue.Empty:
                return out

    def _run(self) -> None:
        while True:
            request, ctx = self.inbox.get()
            try:
                self.outbox.put(self.reviewer.review(request, ctx))
            except Exception as exc:     # noqa: BLE001 — 复核失败只记失败
                log.warning("视觉复核异常：%s", str(exc).splitlines()[0][:200])


class AExtensions:
    """一个 vision-worker 进程（一个源）一个实例。on_frame(frame, persons, phones) -> (追加的 payload 键, A 场景事件列表)。"""

    def __init__(self, source_id: str, *, context: Optional[CachedContext], switches: Optional[dict] = None,
                 reviewer=None, prompt=None, model: str = "", paper_detector=None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 health_config: Optional[StageHealthConfig] = None, min_interval_s: float = 0.0,
                 health_interval_s: float = 0.0, shift_reference=None, shift_roi: tuple = (0.0, 0.0, 1.0, 1.0)) -> None:
        self.source_id = source_id
        self.context = context
        self.switches = {name: True for name in SWITCHES} if switches is None else {name: bool(switches.get(name, True)) for name in SWITCHES}
        self.clock, self.wall = clock, wall
        self.min_interval_s = min_interval_s
        self._t0 = clock()
        self.media_run_id = f"{source_id}:live:{datetime.fromtimestamp(wall(), timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        self._last_ms = -1
        self._last_run = None
        self._observer: Optional[ZoneObserver] = None
        self._observer_version = None
        self.absence, self.cross_zone, self.conflict, self.documents = AbsenceRules(), CrossZoneRules(), ConflictRules(), DocumentRules()
        self.health = StageHealthMonitor(health_config or StageHealthConfig()) if self.switches["A_HEALTH_ENABLED"] else None
        # 5.3 偏移：只有配置了参考图才检测（5.01 复核：拿启动首帧当参考，人走动、演示视频循环都会误报）；健康检查按间隔、缩小后算，不占 B 的帧预算
        self.health_interval_s = health_interval_s
        self._last_health = None
        self._reference = self._small(shift_reference) if shift_reference is not None else None
        self.shift_roi = shift_roi if shift_reference is not None else None
        self._bad_zone_version = None
        self._logged: set = set()
        self._sent: deque = deque(maxlen=2048)
        self._sent_set: set = set()
        self.reviewer, self.prompt, self.model = reviewer, prompt, model
        self.paper_detector = paper_detector
        self._review = _ReviewWorker(reviewer) if (reviewer is not None and getattr(reviewer, "client", None) is not None
                                                  and self.switches["A_VISUAL_REVIEW_ENABLED"]) else None
        self._deduper = CandidateDeduper()
        self._frames: deque = deque(maxlen=3)
        self._facts: deque = deque(maxlen=8)
        self._carry_crossings: deque = deque(maxlen=64)
        self._unbound_logged = False

    # ---------------------------------------------------------------- 组装
    @classmethod
    def from_env(cls, source_id: str, env: Optional[dict] = None) -> Optional["AExtensions"]:
        env = dict(os.environ if env is None else env)
        if not _on(env, "A_EXT_ENABLED"):
            log.info("A 扩展已关闭（A_EXT_ENABLED=0）")
            return None
        switches = {name: _on(env, name) for name in SWITCHES}
        database_url = env.get("DATABASE_URL", "").strip()
        context = None
        if database_url:
            source = DbContextSource(database_url, permit_minutes=float(env.get("A_LEAVE_PERMIT_MINUTES", "30") or 30))
            context = CachedContext(source, source_id, refresh_seconds=float(env.get("A_CONTEXT_REFRESH_SECONDS", "30") or 30), background=True)
        reviewer = prompt = paper = None
        model = ""
        if switches["A_VISUAL_REVIEW_ENABLED"] and env.get("VISUAL_MODEL_BASE_URL", "").strip():
            try:
                from worker.visual_review import reviewer_from_env
                reviewer, prompt = reviewer_from_env(env)
                model = env.get("VISUAL_MODEL_NAME", "")
                if switches["A_S08_CARRY_ENABLED"] and getattr(reviewer, 'client', None) is not None:
                    from worker.object_candidates import YoloCandidateDetector
                    paper = YoloCandidateDetector(env.get("YOLO_MODEL", "/assets/yolov8n.pt"), device=env.get("DEVICE", "cpu"),
                                                  label_map={"book": "paper"})
            except Exception as exc:     # noqa: BLE001 — 视觉模型配置有误只关掉 7.1，不影响其他
                log.warning("视觉复核未启用：%s", str(exc).splitlines()[0][:200])
                reviewer = prompt = paper = None
        else:
            log.info("视觉复核跳过：未配置 VISUAL_MODEL_BASE_URL 或开关关闭")
        reference, roi = None, (0.0, 0.0, 1.0, 1.0)
        if env.get("A_SHIFT_REFERENCE", "").strip():
            reference = cv2.imread(env["A_SHIFT_REFERENCE"].strip())
            if reference is None:
                log.warning("镜头偏移参考图读不到，偏移检测不启用：%s", env["A_SHIFT_REFERENCE"])
            if env.get("A_SHIFT_ROI", "").strip():
                try:
                    roi = tuple(float(v) for v in env["A_SHIFT_ROI"].split(","))
                except ValueError:
                    log.warning("A_SHIFT_ROI 格式应为 x1,y1,x2,y2，按整幅画面")
        ext = cls(source_id, context=context, switches=switches, reviewer=reviewer, prompt=prompt, model=model, paper_detector=paper,
                  health_config=StageHealthConfig.from_env(env), min_interval_s=float(env.get("A_EXT_MIN_INTERVAL_SECONDS", "0") or 0),
                  health_interval_s=float(env.get("A_HEALTH_INTERVAL_SECONDS", "1") or 1), shift_reference=reference, shift_roi=roi)
        log.info("A 扩展已启用：%s", ", ".join(k for k, v in ext.switches.items() if v))
        return ext

    # ---------------------------------------------------------------- 主循环调用
    def on_frame(self, frame, persons: list, phones: list) -> tuple[dict, list[dict]]:
        now = self.clock()
        if self._last_run is not None and now - self._last_run < self.min_interval_s:
            return {}, []
        self._last_run = now
        start_ms = max(int((now - self._t0) * 1000), self._last_ms + 1)
        self._last_ms = start_ms
        rc = self.context.get() if self.context is not None else RuleContext()
        occurred_at = utc_iso(datetime.fromtimestamp(self.wall(), timezone.utc))
        ctx = FrameContext(source_id=self.source_id, occurred_at=occurred_at, media_run_id=self.media_run_id, start_ms=start_ms,
                           frame=frame, **rc.scope)
        extra: dict = {}
        clues: list[tuple[dict, str]] = []
        height, width = frame.shape[:2]
        tracks: list = []
        # 各部分各自兜底（5.01 复核）：区域配置坏了不能连带停掉画面质量与视觉复核；同一类错误只记一次日志
        try:
            dets = normalize_detections([{"bbox": p["bbox"], "label": "person", "confidence": float(p.get("conf", 0.0))} for p in persons],
                                        width, height)
            zone_config = rc.zone_config
            if self.switches["A_ZONES_ENABLED"] and zone_config and (zone_config.get("zones") or zone_config.get("entry_lines")):
                observations = self._zone_observations(rc, ctx, dets)
                if observations is not None:
                    extra.update(self._inside(zone_config, dets))
                    clues += self._zone_clues(rc, observations, people=len(persons))
                    tracks = (self._observer.status().get("tracks") if self._observer else None) or []
        except Exception as exc:     # noqa: BLE001
            self._warn_once("zones", exc)
        if self.health is not None and (self._last_health is None or now - self._last_health >= self.health_interval_s):
            self._last_health = now
            try:
                hctx = FrameContext(source_id=self.source_id, occurred_at=occurred_at, media_run_id=self.media_run_id, start_ms=start_ms,
                                    frame=self._small(frame), reference_frame=self._reference, roi=self.shift_roi, **rc.scope)
                clues += self._health_clues(hctx, rc)
            except Exception as exc:     # noqa: BLE001
                self._warn_once("health", exc)
        try:
            facts = self._visual(ctx, frame, phones, tracks, width, height)
            if facts:
                extra["a_visual_facts"] = facts
        except Exception as exc:     # noqa: BLE001
            self._warn_once("visual", exc)
        events = []
        for clue, event_type in clues:
            if clue["clue_id"] in self._sent_set:
                continue
            if str(clue.get("reason", "")).startswith(UNBOUND_ONLY):
                # 规则对未绑定场次只给这一条、不带画面事实，不进 B 的事件；提示一次：要出 A 的规则事件须先在阶段控制台绑定本源
                self._remember(clue["clue_id"])
                if not self._unbound_logged:
                    log.info("%s 没有阶段快照（未绑定），A 的 S03/S05/S08/S09 规则不判；画面质量与区域字段照常", self.source_id)
                    self._unbound_logged = True
                continue
            event = clue_to_event(clue, event_type)
            if event is None:
                continue
            self._remember(clue["clue_id"])
            events.append(event)
        return extra, events

    # ---------------------------------------------------------------- 5.1 / 5.2 / 5.5 / 7.4
    def _zone_observations(self, rc: RuleContext, ctx: FrameContext, dets: list) -> list[dict]:
        version = (rc.zone_config.get("session_id"), rc.zone_config.get("config_version"))
        if version == self._bad_zone_version:
            return None                     # 这一版区域配置已知不可用：等配置改版再试，不逐帧重建刷日志
        if self._observer is None or version != self._observer_version:
            try:
                self._observer = ZoneObserver({"config_version": rc.zone_config.get("config_version") or 0,
                                               "zones": rc.zone_config.get("zones") or [], "entry_lines": rc.zone_config.get("entry_lines") or []})
            except (ValueError, TypeError, KeyError) as exc:
                self._observer, self._observer_version, self._bad_zone_version = None, None, version
                log.warning("%s 的区域配置 v%s 不可用，区域类能力暂停到配置改版：%s", self.source_id, version[1], str(exc)[:200])
                return None
            self._observer_version = version
        return self._observer.update(ctx, dets)

    @staticmethod
    def _inside(zone_config: dict, dets: list) -> dict:
        """B 的 zone_intrusion 只看 inside / zone_name：任一人框中心落在任一限制区就是 inside。"""
        for zone in zone_config.get("zones") or []:
            if zone.get("kind") != "restricted":
                continue
            polygon = [tuple(p) for p in zone.get("polygon") or []]
            for det in dets:
                if len(polygon) >= 3 and point_in_polygon(bbox_center(tuple(det["bbox"])), polygon):
                    return {"inside": True, "zone_name": zone.get("name"), "a_zone_id": zone.get("zone_id")}
        return {"inside": False}

    def _zone_clues(self, rc: RuleContext, observations: list[dict], people: int) -> list[tuple[dict, str]]:
        out: list[tuple[dict, str]] = []
        if self.switches["A_S03_SEAT_ENABLED"] and people > 0:
            # 画面全空归 B 的 seat_absence；这里只判「画面还有人、某工位空了」，且只取工位规则（越界归 B 的 zone_intrusion）
            seat_obs = [o for o in observations if o["type"] in SEAT_TYPES]
            out += [(c, EVENT_TYPES[("S03", RULE_SEAT)]) for c in self.absence.evaluate(seat_obs, rc.rule_context("S03", "s03-v1"))
                    if c["rule_id"] == RULE_SEAT]
        if self.switches["A_S05_ENABLED"]:
            # 只取两人接近停留；限制区停留与 B 的 zone_intrusion 判同一件事，以 B 为准
            for clue in self.cross_zone.evaluate([o for o in observations if o["type"] == "person_count"],
                                                 rc.rule_context("S05", "s05-v1")):
                event_type = EVENT_TYPES.get(("S05", clue["rule_id"]))
                if event_type:
                    out.append((clue, event_type))
        if self.switches["A_S09_MOTION_ENABLED"]:
            out += [(c, EVENT_TYPES[("S09", RULE_S09)]) for c in
                    self.conflict.evaluate([o for o in observations if o["type"] == "person_count"], rc.rule_context("S09", "s09-v1"))]
        if self.switches["A_S08_CARRY_ENABLED"]:
            crossings = [o for o in observations if o["type"] == "line_crossed"]
            facts = list(self._facts)
            self._carry_crossings.extend(c for c in crossings if (c.get('payload') or {}).get('direction')=='out')
            now_ms = max((o['end_ms'] for o in observations), default=0)
            self._carry_crossings = deque((c for c in self._carry_crossings
                if now_ms-c['end_ms'] <= 15000 and c.get('stage_snapshot_id')==rc.scope.get('stage_snapshot_id')), maxlen=64)
            if self._carry_crossings or facts:
                # The visual model can return after the person has already crossed the door.
                history = sorted(facts + list(self._carry_crossings), key=lambda o:(o['start_ms'],o['type']=='line_crossed'))
                carry = [c for c in self.documents.evaluate(history, rc.rule_context("S08", "s08-v1")) if c["rule_id"]==RULE_CARRY]
                out += [(c, EVENT_TYPES[("S08", RULE_CARRY)]) for c in carry]
                consumed = {oid for c in carry for oid in c.get('observation_ids', [])}
                self._carry_crossings = deque((c for c in self._carry_crossings if c['observation_id'] not in consumed), maxlen=64)
                self._facts.clear()
        return out

    # ---------------------------------------------------------------- 5.3
    def _health_clues(self, ctx: FrameContext, rc: RuleContext) -> list[tuple[dict, str]]:
        out = []
        for observation in self.health.update(ctx):
            if observation.get("type") != "camera_blacked":
                continue      # 断流 / 恢复归 B
            reason = (observation.get("payload") or {}).get("technical_reason")
            if reason not in HEALTH_EVENTS:
                continue      # blacked 归 B 的 camera_blacked
            snap = rc.snapshot or {}
            version = (snap.get("rule_versions") or {}).get("S10") or "s10-v1"
            clue = new_clue(observation, scene_id="S10", rule_id=HEALTH_RULE, rule_version=version, conclusion="suspected", risk_level="medium",
                            reason=f"{HEALTH_REASONS[reason]}（技术事实，不代表人为遮挡）；缺口 {(observation.get('payload') or {}).get('gap_id', '')}",
                            missing_facts=["阶段快照未绑定"] if observation.get("binding_status") != "bound" else ())
            out.append((clue, HEALTH_EVENTS[reason]))
        return out

    # ---------------------------------------------------------------- 7.1
    def _visual(self, ctx: FrameContext, frame, phones: list, tracks: list, width: int, height: int) -> list[dict]:
        if self._review is None:
            return []
        self._frames.append((ctx.start_ms, frame))
        raw = [{"class": "cell phone", "conf": float(p.get("conf", 0.0)), "bbox": p["bbox"]} for p in phones]
        if self.paper_detector is not None:
            try:
                raw += [d for d in self.paper_detector.detect(frame) if d.get("label", d.get("class")) == "book"]
            except Exception as exc:     # noqa: BLE001
                log.warning("纸张候选检测失败：%s", str(exc).splitlines()[0][:200])
        persons = [{"track_id": t.get("track_id"), "bbox": t.get("bbox")} for t in tracks if isinstance(t, dict)]
        from worker.visual_review import build_review_request
        for candidate in extract_candidates(raw, width, height, start_ms=ctx.start_ms, persons=persons):
            if not self._deduper.admit(candidate):
                continue
            self._review.submit(build_review_request(list(self._frames), candidate, prompt=self.prompt, model=self.model), ctx)
        done = self._review.drain()
        self._facts.extend(o for o in done if (o.get("payload") or {}).get("status") == "ok")
        return [{"kind": (o.get("payload") or {}).get("candidate", {}).get("kind"), "status": (o.get("payload") or {}).get("status"),
                 "facts": (o.get("payload") or {}).get("facts")} for o in done]

    @staticmethod
    def _small(frame, max_width: int = 320):
        """健康检查只看亮度、清晰度与整体位移：缩到 320 宽足够，省 B 的 CPU。"""
        if frame is None:
            return None
        height, width = frame.shape[:2]
        if width <= max_width:
            return frame
        return cv2.resize(frame, (max_width, max(1, int(height * max_width / width))), interpolation=cv2.INTER_NEAREST)   # 隔点取样：不抹平方差，黑屏 / 过暗的判别与全分辨率一致

    def _warn_once(self, section: str, exc: Exception) -> None:
        key = (section, type(exc).__name__, str(exc)[:80])
        if key not in self._logged:
            self._logged.add(key)
            log.warning("A 扩展 %s 本帧跳过：%s", section, (str(exc).splitlines() or [type(exc).__name__])[0][:200])

    def _remember(self, clue_id: str) -> None:
        if len(self._sent) == self._sent.maxlen:
            self._sent_set.discard(self._sent[0])
        self._sent.append(clue_id)
        self._sent_set.add(clue_id)


__all__ = ["AExtensions", "A_EVENT_TYPE", "EVENT_TYPES", "HEALTH_EVENTS", "SWITCHES", "clue_to_event"]
