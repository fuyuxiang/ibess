"""执行卡 5.1：归一化坐标（0～1）上的纯几何，没有任何业务含义。

点在多边形用射线法，落在边上算「在内」（避免边界抖动）；人框与工位多边形的重叠比例用
Sutherland–Hodgman 把多边形裁进矩形再算面积，所以凹多边形也对；跨线方向用叉积符号的变化 + 线段相交。
"""
from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence

EPS = 1e-9
Point = tuple[float, float]
BBox = tuple[float, float, float, float]


def validate_point(point: Sequence[float]) -> Point:
    if not isinstance(point, (list, tuple)) or len(point) != 2:
        raise ValueError("点必须是 [x, y]")
    out = []
    for value in point:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("坐标必须是有限数值")
        if not 0.0 <= value <= 1.0:
            raise ValueError("坐标必须在 0～1 之间")
        out.append(float(value))
    return out[0], out[1]


def validate_bbox(bbox: Sequence[float]) -> BBox:
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise ValueError("bbox 必须是 [x1, y1, x2, y2]")
    x1, y1 = validate_point(bbox[:2])
    x2, y2 = validate_point(bbox[2:])
    if x2 <= x1 or y2 <= y1:
        raise ValueError("bbox 必须满足 x1<x2 且 y1<y2")
    return x1, y1, x2, y2


def validate_polygon(polygon: Iterable[Sequence[float]]) -> list[Point]:
    points = [validate_point(p) for p in polygon]
    if len(points) < 3:
        raise ValueError("多边形至少 3 个点")
    if len(points) > 64:
        raise ValueError("多边形最多 64 个点")
    if len(set(points)) != len(points):
        raise ValueError("多边形顶点不能重复")
    if abs(polygon_area(points)) <= EPS:
        raise ValueError("多边形面积必须大于零")
    n = len(points)
    for i in range(n):
        a, b = points[i], points[(i + 1) % n]
        for j in range(i + 1, n):
            if j == i or j == (i + 1) % n or i == (j + 1) % n:
                continue
            c, d = points[j], points[(j + 1) % n]
            if segments_intersect(a, b, c, d):
                raise ValueError("多边形边不能自相交")
    return points


def polygon_area(points: Sequence[Point]) -> float:
    total = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def bbox_area(bbox: BBox) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def bbox_center(bbox: BBox) -> Point:
    return (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0


def bbox_foot(bbox: BBox) -> Point:
    """脚点：底边中点。只作参考，占用判断不以它为唯一依据（执行卡）。"""
    return (bbox[0] + bbox[2]) / 2.0, bbox[3]


def _on_segment(p: Point, a: Point, b: Point) -> bool:
    cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    if abs(cross) > EPS:
        return False
    return min(a[0], b[0]) - EPS <= p[0] <= max(a[0], b[0]) + EPS and min(a[1], b[1]) - EPS <= p[1] <= max(a[1], b[1]) + EPS


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    """射线法；点在边上算在内。"""
    x, y = point
    inside = False
    n = len(polygon)
    for i in range(n):
        a, b = polygon[i], polygon[(i + 1) % n]
        if _on_segment(point, a, b):
            return True
        if (a[1] > y) != (b[1] > y):
            x_cross = a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if x < x_cross:
                inside = not inside
    return inside


def clip_polygon_to_bbox(polygon: Sequence[Point], bbox: BBox) -> list[Point]:
    """Sutherland–Hodgman：用矩形的四条边依次裁多边形（矩形是凸的，所以对凹多边形也成立）。"""
    x1, y1, x2, y2 = bbox
    edges = (
        (lambda p: p[0] >= x1 - EPS, lambda a, b: _intersect_vertical(a, b, x1)),
        (lambda p: p[0] <= x2 + EPS, lambda a, b: _intersect_vertical(a, b, x2)),
        (lambda p: p[1] >= y1 - EPS, lambda a, b: _intersect_horizontal(a, b, y1)),
        (lambda p: p[1] <= y2 + EPS, lambda a, b: _intersect_horizontal(a, b, y2)),
    )
    output = list(polygon)
    for inside, intersect in edges:
        if not output:
            break
        input_list, output = output, []
        prev = input_list[-1]
        for current in input_list:
            if inside(current):
                if not inside(prev):
                    output.append(intersect(prev, current))
                output.append(current)
            elif inside(prev):
                output.append(intersect(prev, current))
            prev = current
    return output


def _intersect_vertical(a: Point, b: Point, x: float) -> Point:
    if abs(b[0] - a[0]) <= EPS:
        return x, a[1]
    t = (x - a[0]) / (b[0] - a[0])
    return x, a[1] + t * (b[1] - a[1])


def _intersect_horizontal(a: Point, b: Point, y: float) -> Point:
    if abs(b[1] - a[1]) <= EPS:
        return a[0], y
    t = (y - a[1]) / (b[1] - a[1])
    return a[0] + t * (b[0] - a[0]), y


def bbox_polygon_overlap_ratio(bbox: BBox, polygon: Sequence[Point]) -> float:
    """重叠面积 / min(人框面积, 多边形面积)（0～1）：小工位被大人框盖住、大人框只有一角在工位里都能正确反映。"""
    area = min(bbox_area(bbox), abs(polygon_area(polygon)))
    if area <= EPS:
        return 0.0
    clipped = clip_polygon_to_bbox(polygon, bbox)
    if len(clipped) < 3:
        return 0.0
    overlap = abs(polygon_area(clipped))
    return max(0.0, min(1.0, overlap / area))


def bbox_iou(a: BBox, b: BBox) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = bbox_area(a) + bbox_area(b) - inter
    return inter / union if union > EPS else 0.0


def segment_side(point: Point, start: Point, end: Point) -> int:
    """点在有向线段 start→end 的哪一侧：+1 左（叉积正）、-1 右、0 共线。"""
    cross = (end[0] - start[0]) * (point[1] - start[1]) - (end[1] - start[1]) * (point[0] - start[0])
    return 0 if abs(cross) <= EPS else (1 if cross > 0 else -1)


def segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    s1, s2 = segment_side(c, a, b), segment_side(d, a, b)
    s3, s4 = segment_side(a, c, d), segment_side(b, c, d)
    if s1 * s2 < 0 and s3 * s4 < 0:
        return True
    return (s1 == 0 and _on_segment(c, a, b)) or (s2 == 0 and _on_segment(d, a, b)) or \
           (s3 == 0 and _on_segment(a, c, d)) or (s4 == 0 and _on_segment(b, c, d))


def crossing_direction(prev: Point, now: Point, start: Point, end: Point) -> Optional[str]:
    """轨迹 prev→now 是否跨过门线 start→end。约定：从右侧（-1）到左侧（+1）为 in，反之为 out。
    任一端正落在线上不判定（调用方应沿用上一次非零侧别，避免同一次跨越报两次）；只变侧但不与线段相交（绕过端点）不算跨线。"""
    before, after = segment_side(prev, start, end), segment_side(now, start, end)
    if before == 0 or after == 0 or before == after:
        return None
    if not segments_intersect(prev, now, start, end):
        return None
    return "in" if after > 0 else "out"
