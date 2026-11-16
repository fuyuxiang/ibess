"""执行卡 5.1：轻量多目标跟踪（IoU 贪心匹配），零依赖、确定性。

track_id 只在本源本媒体轮次内有效（id 里带 media_run_id），不是人员身份，永远不能当 person_id。
丢失按媒体时间容忍（max_missed_ms），超过即删除，重现给新 id；reset(media_run_id) 全部清零。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
import math

from worker.zone_geometry import BBox, bbox_center, bbox_iou, validate_bbox


def tracking_detections(detections: list[dict], width: int, height: int) -> list[dict]:
    """只为轨迹计算裁剪检测框；不覆盖原始检测输出，坏框不停止采集。"""
    if width <= 0 or height <= 0:
        return []
    result=[]
    for item in detections:
        try:
            raw=item['bbox']
            if len(raw)!=4 or not all(math.isfinite(float(v)) for v in raw):continue
            box=[max(0.,min(1.,float(v)/dimension)) for v,dimension in zip(raw,(width,height,width,height))]
            box=validate_bbox(box)
            confidence=float(item.get('conf',0))
            result.append({'bbox':box,'confidence':confidence if math.isfinite(confidence) else 0.})
        except (ValueError,TypeError,KeyError):
            continue
    return result


@dataclass
class Track:
    track_id: str
    bbox: BBox
    first_ms: int
    last_ms: int
    confidence: float
    hits: int = 1
    misses: int = 0
    history: list = field(default_factory=list)   # [(start_ms, center)]，最多 max_history

    @property
    def center(self):
        return bbox_center(self.bbox)

    def previous_center(self):
        return self.history[-2][1] if len(self.history) >= 2 else None

    def to_dict(self) -> dict:
        return {"track_id": self.track_id, "bbox": [round(v, 6) for v in self.bbox], "confidence": round(self.confidence, 4),
                "first_ms": self.first_ms, "last_ms": self.last_ms, "hits": self.hits}


class IouTracker:
    """每帧：现有轨迹 × 检测 两两算 IoU，贪心取最大配对；没配上的检测开新轨迹，没配上的轨迹记丢失。"""

    def __init__(self, media_run_id: str, *, iou_threshold: float = 0.3, max_missed_ms: int = 2000, max_history: int = 16) -> None:
        if not isinstance(media_run_id, str) or not media_run_id.strip():
            raise ValueError("media_run_id 必填")
        if not 0.0 < iou_threshold < 1.0:
            raise ValueError("iou_threshold 必须在 (0,1)")
        if max_missed_ms < 0:
            raise ValueError("max_missed_ms 不能为负")
        self.media_run_id = media_run_id
        self.iou_threshold = iou_threshold
        self.max_missed_ms = max_missed_ms
        self.max_history = max_history
        self._tracks: dict[str, Track] = {}
        self._next = 0
        self._last_ms: Optional[int] = None

    def reset(self, media_run_id: str) -> None:
        if not isinstance(media_run_id, str) or not media_run_id.strip():
            raise ValueError("media_run_id 必填")
        self.media_run_id = media_run_id
        self._tracks.clear()
        self._next = 0
        self._last_ms = None

    def pause(self, start_ms: int) -> None:
        """故障帧：时间照走但不算轨迹丢失（把所有轨迹的 last_ms 推到现在）。"""
        if isinstance(start_ms, bool) or not isinstance(start_ms, int) or start_ms < 0:
            raise ValueError("start_ms 必须是非负整数毫秒")
        if self._last_ms is not None and start_ms < self._last_ms:
            raise ValueError("媒体时间不能倒退；新轮次请先 reset")
        self._last_ms = start_ms
        for track in self._tracks.values():
            track.last_ms = max(track.last_ms, start_ms)

    def active(self) -> list[Track]:
        """本帧有检测支撑的轨迹（misses == 0）。"""
        return sorted((t for t in self._tracks.values() if t.misses == 0), key=lambda t: t.track_id)

    def all_tracks(self) -> list[Track]:
        return sorted(self._tracks.values(), key=lambda t: t.track_id)

    def update(self, detections: list[dict], start_ms: int) -> list[Track]:
        """detections 已归一化并校验（bbox、confidence）。返回本帧匹配或新建的轨迹。"""
        if isinstance(start_ms, bool) or not isinstance(start_ms, int) or start_ms < 0:
            raise ValueError("start_ms 必须是非负整数毫秒")
        if self._last_ms is not None and start_ms < self._last_ms:
            raise ValueError("媒体时间不能倒退；新轮次请先 reset")
        self._last_ms = start_ms
        boxes = [validate_bbox(d["bbox"]) for d in detections]
        pairs = []
        for track_id, track in self._tracks.items():
            for index, box in enumerate(boxes):
                iou = bbox_iou(track.bbox, box)
                if iou >= self.iou_threshold:
                    pairs.append((iou, track_id, index))
        pairs.sort(key=lambda item: (-item[0], item[1], item[2]))
        used_tracks: set[str] = set()
        used_boxes: set[int] = set()
        matched: list[Track] = []
        for iou, track_id, index in pairs:
            if track_id in used_tracks or index in used_boxes:
                continue
            track = self._tracks[track_id]
            track.bbox = boxes[index]
            track.confidence = float(detections[index].get("confidence", 0.0))
            track.last_ms = start_ms
            track.hits += 1
            track.misses = 0
            self._push_history(track, start_ms)
            used_tracks.add(track_id)
            used_boxes.add(index)
            matched.append(track)
        for index, box in enumerate(boxes):
            if index in used_boxes:
                continue
            self._next += 1
            track = Track(track_id=f"track:{self.media_run_id}:{self._next}", bbox=box, first_ms=start_ms, last_ms=start_ms,
                          confidence=float(detections[index].get("confidence", 0.0)))
            self._push_history(track, start_ms)
            self._tracks[track.track_id] = track
            matched.append(track)
        for track_id, track in list(self._tracks.items()):
            if track_id in used_tracks or track.first_ms == start_ms:
                continue
            track.misses += 1
            if start_ms - track.last_ms > self.max_missed_ms:
                del self._tracks[track_id]
        return sorted(matched, key=lambda t: t.track_id)

    def _push_history(self, track: Track, start_ms: int) -> None:
        track.history.append((start_ms, bbox_center(track.bbox)))
        if len(track.history) > self.max_history:
            del track.history[0]
