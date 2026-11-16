"""执行卡 7.1：person 之外的物品候选（手机 / 纸张 / 屏幕），用自己的 YOLO 实例，不改共享 detector.py。

候选只是「检测器说这里可能有个东西」：映射种类、归一化框、过小标看不清、关联持有人轨迹、同持有人同种类 5 秒去重。
手表没有模型，列为待配置；纸张用 COCO 的 book 近似，payload 保留原类名，不自动证明是受控资料。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from worker.zone_geometry import bbox_area, validate_bbox

DEFAULT_LABEL_MAP = {"cell phone": "phone", "book": "paper", "laptop": "screen"}
UNSUPPORTED_KINDS = {"watch": "no_model"}       # 执行卡：手表未有模型则待配置
CANDIDATE_KINDS = ("phone", "paper", "screen")


@dataclass
class CandidateConfig:
    label_map: dict = field(default_factory=lambda: dict(DEFAULT_LABEL_MAP))
    min_confidence: float = 0.2
    min_side: float = 0.02          # 归一化最小边；更小的候选标 too_small（看不清）
    dedupe_ms: int = 5000           # 执行卡:81 候选去重 5 秒
    holder_overlap: float = 0.5     # 候选框落在人框内的面积比例 ≥ 此值视为该人持有

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_confidence <= 1.0 or not 0.0 < self.min_side < 1.0 or self.dedupe_ms < 0:
            raise ValueError("候选配置非法")


@dataclass(frozen=True)
class Candidate:
    kind: str
    label: str
    bbox: tuple
    confidence: float
    start_ms: int
    holder_track_id: Optional[str] = None
    too_small: bool = False

    def to_dict(self) -> dict:
        return {"kind": self.kind, "label": self.label, "bbox": [round(v, 6) for v in self.bbox], "confidence": round(self.confidence, 4),
                "holder_track_id": self.holder_track_id, "too_small": self.too_small, "start_ms": self.start_ms}

    @property
    def subject(self) -> str:
        if self.holder_track_id:
            return f"{self.kind}:{self.holder_track_id}"
        cx, cy = (self.bbox[0] + self.bbox[2]) / 2.0, (self.bbox[1] + self.bbox[3]) / 2.0
        return f"{self.kind}:cell:{round(cx * 10)}:{round(cy * 10)}"     # 无人持有：按框中心落在 0.1 网格的格子区分，两处物品不互相压掉


def unsupported_kinds() -> dict:
    """没有模型的候选种类（给 status / 页面显示「待配置」）。"""
    return dict(UNSUPPORTED_KINDS)


def _contained_ratio(inner: tuple, outer: tuple) -> float:
    ix1, iy1 = max(inner[0], outer[0]), max(inner[1], outer[1])
    ix2, iy2 = min(inner[2], outer[2]), min(inner[3], outer[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area = bbox_area(inner)
    return inter / area if area > 0 else 0.0


def extract_candidates(raw_detections: list[dict], frame_width: int, frame_height: int, config: Optional[CandidateConfig] = None, *,
                       start_ms: int, persons: Optional[list[dict]] = None) -> list[Candidate]:
    """raw_detections：检测器原始输出 [{class|label, conf|confidence, bbox(像素)}]；persons：5.1 的轨迹 [{track_id, bbox(归一化)}]。"""
    config = config or CandidateConfig()
    if frame_width <= 0 or frame_height <= 0:
        raise ValueError("帧宽高必须为正")
    out: list[Candidate] = []
    for det in raw_detections:
        if not isinstance(det, dict):
            continue
        label = det.get("label", det.get("class"))
        kind = config.label_map.get(label)
        if kind is None:
            continue
        confidence = det.get("confidence", det.get("conf", 0.0))
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or confidence < config.min_confidence:
            continue
        try:
            x1, y1, x2, y2 = det["bbox"]
            bbox = validate_bbox([min(max(x1 / frame_width, 0.0), 1.0), min(max(y1 / frame_height, 0.0), 1.0),
                                  min(max(x2 / frame_width, 0.0), 1.0), min(max(y2 / frame_height, 0.0), 1.0)])
        except (KeyError, TypeError, ValueError):
            continue
        too_small = min(bbox[2] - bbox[0], bbox[3] - bbox[1]) < config.min_side
        holder = None
        best = 0.0
        for person in persons or []:
            try:
                pbox = validate_bbox(person["bbox"])
            except (KeyError, TypeError, ValueError):
                continue
            ratio = _contained_ratio(bbox, pbox)
            if ratio >= config.holder_overlap and ratio > best:
                best, holder = ratio, person.get("track_id")
        out.append(Candidate(kind=kind, label=str(label), bbox=bbox, confidence=float(confidence), start_ms=start_ms,
                             holder_track_id=holder, too_small=too_small))
    return out


class CandidateDeduper:
    """同持有人同种类候选在 dedupe_ms 内只送审一次；按媒体时间计，新轮次 reset。"""

    def __init__(self, dedupe_ms: int = 5000) -> None:
        self.dedupe_ms = dedupe_ms
        self._last: dict[str, int] = {}

    def reset(self) -> None:
        self._last.clear()

    def admit(self, candidate: Candidate) -> bool:
        last = self._last.get(candidate.subject)
        if last is not None and candidate.start_ms - last < self.dedupe_ms:
            return False
        if not candidate.too_small:                  # 过小的候选照送（会被标看不清），但不占去重窗口：随后清晰的那个仍可送审
            self._last[candidate.subject] = candidate.start_ms
        if len(self._last) > 256:                    # 持有人轨迹编号只增不减：过期的去重记录清掉（5.01 复核）
            for subject in [s for s, ms in self._last.items() if candidate.start_ms - ms >= self.dedupe_ms]:
                del self._last[subject]
        return True


class YoloCandidateDetector:
    """自己的 YOLO 实例（延迟导入 ultralytics，测试不需要）；只取 label_map 里的类。"""

    def __init__(self, model_path: str, *, conf: float = 0.2, imgsz: int = 640, device: str = "cuda", label_map: Optional[dict] = None) -> None:
        from ultralytics import YOLO  # noqa: WPS433  运行时才需要
        self.model = YOLO(model_path)
        self.conf = conf
        self.imgsz = imgsz
        self.device = device
        self.label_map = dict(label_map or DEFAULT_LABEL_MAP)

    def detect(self, frame) -> list[dict]:
        results = self.model.predict(frame, conf=self.conf, imgsz=self.imgsz, device=self.device, verbose=False)
        dets = []
        if results and len(results[0].boxes):
            names = results[0].names
            for box in results[0].boxes:
                name = names[int(box.cls)]
                if name in self.label_map:
                    x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
                    dets.append({"class": name, "conf": float(box.conf), "bbox": [x1, y1, x2, y2]})
        return dets
