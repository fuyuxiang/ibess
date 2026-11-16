"""video-audit worker：历史录像逐帧回放 → 检测 → 审计发现。

与实时链路（vision-worker）的区别：
- 输入是文件而不是 RTSP 流，可以快进、可以跑完即止
- 结果写 video_audits / audit_findings，不进实时事件流
  （历史视频里的"违规"若混进今天的告警，冷却、去重、取证全乱）
- 判定复用同一套 YOLO 检测器与阈值，只是把"时刻"换算成视频内的毫秒偏移

任务模型：DB 轮询（queued → running → done/failed），无需 MQ。
"""

from __future__ import annotations

import os

# 审计单进程给 4 线程：开满会和实时检测互抢，实测 8 线程时推理慢 88 倍
os.environ.setdefault("OMP_NUM_THREADS", "4")
import torch

torch.set_num_threads(int(os.getenv("TORCH_THREADS", "4")))

import io
import logging
import os
import time
import uuid

import cv2
from minio import Minio
from sqlalchemy import create_engine, text

from worker.detector import PersonDetector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("audit-worker")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://demo@postgres:5432/demo")
VIDEO_DIR = os.getenv("VIDEO_DIR", "/videos")
SAMPLE_FPS = float(os.getenv("AUDIT_SAMPLE_FPS", "3"))     # 审计抽帧率，低于实时
CROWD_THRESHOLD = int(os.getenv("AUDIT_CROWD_THRESHOLD", "4"))
BLACK_VAR_THRESHOLD = float(os.getenv("BLACK_VAR_THRESHOLD", "10"))
BLACK_HOLD_MS = 1000
ABSENCE_SECONDS = float(os.getenv("AUDIT_ABSENCE_SECONDS", "10"))
CLOSE_DIST = float(os.getenv("AUDIT_CLOSE_DIST", "0.12"))      # 两人中心距离阈值（画面宽度比例）
CLOSE_SECONDS = float(os.getenv("AUDIT_CLOSE_SECONDS", "3"))    # 持续多久才算接触   # 连续无人达到该值记为离岗
DEDUP_MS = 3000                                            # 同类发现默认最小间隔
# 持续性状态（离岗、遮挡）间隔放长，否则一个持续动作会被拆成十几条
DEDUP_BY_TYPE = {"seat_absence": 60000, "camera_blacked": 30000, "zone_intrusion": 30000}
POLL_SECONDS = float(os.getenv("AUDIT_POLL_SECONDS", "3"))
MODEL_PATH = os.getenv("YOLO_MODEL", "/assets/yolov8n.pt")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
minio = Minio(
    os.getenv("MINIO_ENDPOINT", "minio:9000"),
    access_key=os.getenv("MINIO_ROOT_USER", "demo"),
    secret_key=os.getenv("MINIO_ROOT_PASSWORD", ""),
    secure=False,
)
BUCKET = os.getenv("MINIO_BUCKET", "evidence")

def point_in_polygon(x: float, y: float, poly: list) -> bool:
    """射线法判断点是否在多边形内；坐标均为 0~1 归一化值。"""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i][0], poly[i][1]
        xj, yj = poly[j][0], poly[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-9) + xi):
            inside = not inside
        j = i
    return inside


def zone_occupied(boxes: list, poly: list, width: int, height: int) -> bool:
    """任一目标框中心点落在区域内即视为有人。boxes 为像素坐标 xyxy。"""
    for x1, y1, x2, y2 in boxes:
        cx = (x1 + x2) / 2 / width
        cy = (y1 + y2) / 2 / height
        if point_in_polygon(cx, cy, poly):
            return True
    return False


def is_standing(box: list) -> bool:
    """坐着时人体框偏矮扁（下半身被桌面遮挡），站立时明显竖长。"""
    w = max(box[2] - box[0], 1e-6)
    h = box[3] - box[1]
    return h >= w * 1.5


def min_pair_distance(boxes: list, width: int, height: int) -> float:
    """任意两人中心点的最小距离（按画面宽归一化）。无或仅 1 人返回 1.0。"""
    centers = [((b[0] + b[2]) / 2 / width, (b[1] + b[3]) / 2 / height) for b in boxes]
    best = 1.0
    for i in range(len(centers)):
        for j in range(i + 1, len(centers)):
            dx = centers[i][0] - centers[j][0]
            dy = centers[i][1] - centers[j][1]
            best = min(best, (dx * dx + dy * dy) ** 0.5)
    return best


def load_zones(source_id: str) -> list[dict]:
    """读取该摄像机配置的工位区（归一化多边形）。"""
    try:
        with engine.begin() as conn:
            rows = conn.execute(text("""
                select name, type, polygon from zones
                where camera_id = :cid and polygon is not null
            """), {"cid": source_id}).mappings().all()
        return [{"name": r["name"], "type": r["type"] or "seat", "polygon": r["polygon"]} for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.warning("区域配置读取失败：%s", type(exc).__name__)
        return []


det = PersonDetector(MODEL_PATH, conf=0.25, imgsz=480, device=os.getenv("DEVICE", "cpu"))
log.info("审计 worker 就绪，模型 %s，抽帧 %.1f fps", MODEL_PATH, SAMPLE_FPS)


def _snapshot(frame, audit_id: str, at_ms: int) -> str | None:
    """发现时刻留一张截图，存 MinIO 供审计报告引用。"""
    try:
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok:
            return None
        data = buf.tobytes()
        key = f"audit/{audit_id}/{at_ms}.jpg"
        minio.put_object(BUCKET, key, io.BytesIO(data), len(data), content_type="image/jpeg")
        return key
    except Exception as exc:  # noqa: BLE001
        log.warning("截图上传失败：%s", type(exc).__name__)
        return None


def _mark(audit_id: str, status: str | None = None, progress: int | None = None,
          error: str | None = None, findings: int | None = None, finish: bool = False):
    sets, params = [], {"aid": audit_id}
    if status:
        sets.append("status = :st")
        params["st"] = status
        if status == "running":
            sets.append("started_at = now()")
        if finish:
            sets.append("finished_at = now()")
    if progress is not None:
        sets.append("progress = :pg")
        params["pg"] = progress
    if findings is not None:
        sets.append("findings_count = :fc")
        params["fc"] = findings
    if error is not None:
        sets.append("error = :err")
        params["err"] = error[:250]
    if not sets:
        return
    with engine.begin() as conn:
        conn.execute(text(f"update video_audits set {', '.join(sets)} where audit_id = :aid"), params)


def _insert_findings(audit_id: str, rows: list[tuple]):
    if not rows:
        return
    with engine.begin() as conn:
        conn.execute(text("""
            insert into audit_findings
              (audit_id, at_ms, event_type, risk_level, confidence, person_count, phone_count, snapshot_key, reason)
            values (:audit_id, :at_ms, :et, :risk, :conf, :pc, :phc, :key, :reason)
        """), [
            {"audit_id": audit_id, "at_ms": r[0], "et": r[1], "risk": r[2], "conf": r[3],
             "pc": r[4], "phc": r[5], "key": r[6], "reason": r[7]} for r in rows
        ])


DEMO_SOURCE_MAP = {"S03": "camera-009", "S05": "camera-010"}


def infer_source(file_name: str) -> str:
    """素材名 → 摄像头 ID（演示映射：S03 对应工位机位，S05 对应通道机位）。"""
    for key, cam in DEMO_SOURCE_MAP.items():
        if key in file_name:
            return cam
    return os.getenv("AUDIT_SOURCE_ID", "audit-default")


def analyze(audit_id: str, path: str, source_id: str = 'audit-default') -> list[tuple]:
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("无法打开视频文件")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    step = max(1, int(round(fps / SAMPLE_FPS)))
    zones = load_zones(source_id)
    log.info("开始审计 %s：fps=%.1f 总帧=%d 抽帧步长=%d 区域=%d", audit_id[:8], fps, total, step, len(zones))

    findings: list[tuple] = []
    last_hit: dict[str, int] = {}
    black_since: int | None = None
    absent_since: int | None = None
    absent_by_zone: dict[str, int] = {}
    close_since: int | None = None
    idx = 0
    last_progress = -1

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        idx += 1
        if idx % step:
            continue
        at_ms = int(idx / fps * 1000)

        # 画面异常：方差过低持续 1 秒
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        variance = float(gray.var())
        if variance < BLACK_VAR_THRESHOLD:
            black_since = at_ms if black_since is None else black_since
            if at_ms - black_since >= BLACK_HOLD_MS and "camera_blacked" not in last_hit:
                key = _snapshot(frame, audit_id, at_ms)
                findings.append((at_ms, "camera_blacked", "high", None, 0, 0, key, "画面被遮挡或黑屏"))
                last_hit["camera_blacked"] = at_ms
        else:
            black_since = None
            last_hit.pop("camera_blacked", None)

        dets, phones = det.detect(frame)
        count = len(dets)
        height, width = frame.shape[:2]
        frames_done = locals().get("_frames_done", 0) + 1
        _frames_done = frames_done
        if frames_done % 15 == 0:
            log.info("抽样 %d 帧：本帧 %d 人 / %d 手机（%.0f%%）", frames_done, count, len(phones), (idx / total * 100) if total else 0)

        def _due(kind: str) -> bool:
            prev = last_hit.get(kind)
            gap = DEDUP_BY_TYPE.get(kind.split(":", 1)[0], DEDUP_MS)
            return prev is None or at_ms - prev >= gap

        # 离岗：逐座位判定。配了工位就定位到"哪个座位、哪个人"；否则退回全局无人。
        if zones:
            boxes = [d["bbox"] for d in dets]
            h, w = frame.shape[:2]
            for z in zones:
                seat = z["name"]
                if (z.get("type") or "seat") == "restricted":
                    # 限制区：区内 ≥2 人视为非授权接触
                    inside = [b for b in boxes
                              if point_in_polygon((b[0] + b[2]) / 2 / w, (b[1] + b[3]) / 2 / h, z["polygon"])]
                    zkey = f"zone_intrusion:{seat}"
                    if len(inside) >= 2 and _due(zkey):
                        snap = _snapshot(frame, audit_id, at_ms)
                        findings.append((at_ms, "zone_intrusion", "high", None, len(inside), 0, snap,
                                         f"{seat} 出现 {len(inside)} 人，疑似非授权接触"))
                        last_hit[zkey] = at_ms
                    continue
                key_seat = f"seat_absence:{seat}"
                if zone_occupied(boxes, z["polygon"], w, h):
                    absent_by_zone.pop(seat, None)
                    last_hit.pop(key_seat, None)
                    continue
                start = absent_by_zone.setdefault(seat, at_ms)
                seat_sec = (at_ms - start) / 1000.0
                if seat_sec >= ABSENCE_SECONDS and _due(key_seat):
                    snap = _snapshot(frame, audit_id, at_ms)
                    findings.append((at_ms, "seat_absence", "medium", None, 0, 0, snap,
                                     f"{seat} 连续无人 {seat_sec:.0f} 秒（阈值 {ABSENCE_SECONDS:.0f} 秒）"))
                    last_hit[key_seat] = at_ms
        elif count == 0:
            absent_since = at_ms if absent_since is None else absent_since
            absent_sec = (at_ms - absent_since) / 1000.0
            if absent_sec >= ABSENCE_SECONDS and _due("seat_absence"):
                snap = _snapshot(frame, audit_id, at_ms)
                findings.append((at_ms, "seat_absence", "medium", None, 0, 0, snap,
                                 f"画面连续无人 {absent_sec:.0f} 秒（阈值 {ABSENCE_SECONDS:.0f} 秒）"))
                last_hit["seat_absence"] = at_ms
        else:
            absent_since = None
            last_hit.pop("seat_absence", None)

        # 非授权接触：两人距离过近并持续，比"区域内 ≥2 人"更贴合"凑近"这个行为
        if count >= 2:
            boxes_now = [d["bbox"] for d in dets]
            gap = min_pair_distance(boxes_now, width, height)
            # 相邻而坐：两人都坐着，距离再近也只是同桌；必须有人起身靠近才算接触
            someone_standing = any(is_standing(b) for b in boxes_now)
            if gap < CLOSE_DIST and someone_standing:
                close_since = at_ms if close_since is None else close_since
                if (at_ms - close_since) / 1000.0 >= CLOSE_SECONDS and _due("zone_intrusion:close"):
                    snap = _snapshot(frame, audit_id, at_ms)
                    findings.append((at_ms, "zone_intrusion", "high", None, count, 0, snap,
                                     f"两人距离 {gap:.2f}（阈值 {CLOSE_DIST:.2f}），疑似非授权接触"))
                    last_hit["zone_intrusion:close"] = at_ms
            else:
                # 距离恢复时只重置计时，保留冷却记录，避免来回走动导致反复报
                close_since = None
        else:
            close_since = None

        if count >= CROWD_THRESHOLD and _due("crowd_gathering"):
            conf = max((d.get("conf", 0) for d in dets), default=None)
            key = _snapshot(frame, audit_id, at_ms)
            findings.append((at_ms, "crowd_gathering", "medium", conf, count, len(phones), key,
                             f"检测到 {count} 人聚集（阈值 {CROWD_THRESHOLD} 人）"))
            last_hit["crowd_gathering"] = at_ms

        if phones and _due("phone_detected"):
            conf = max((p.get("conf", 0) for p in phones), default=None)
            key = _snapshot(frame, audit_id, at_ms)
            findings.append((at_ms, "phone_detected", "medium", conf, count, len(phones), key,
                             f"检测到 {len(phones)} 部手机"))
            last_hit["phone_detected"] = at_ms

        if total:
            progress = int(idx / total * 100)
            if progress >= last_progress + 10:
                last_progress = progress
                _mark(audit_id, progress=progress)

    cap.release()
    return findings


def run_once() -> bool:
    with engine.begin() as conn:
        row = conn.execute(text("""
            select audit_id, file_name, file_path from video_audits
            where status = 'queued' order by created_at limit 1
        """)).mappings().first()
    if row is None:
        return False

    audit_id = row["audit_id"]
    path = row["file_path"] or os.path.join(VIDEO_DIR, row["file_name"])
    _mark(audit_id, status="running", progress=0)
    try:
        results = analyze(audit_id, path, source_id=infer_source(row['file_name']))
        _insert_findings(audit_id, results)
        _mark(audit_id, status="completed", progress=100, findings=len(results), finish=True)
        log.info("审计完成 %s：%d 处发现", audit_id[:8], len(results))
    except Exception as exc:  # noqa: BLE001 — 单任务失败不能拖垮守护进程
        log.warning("审计失败 %s：%s %s", audit_id[:8], type(exc).__name__, exc)
        _mark(audit_id, status="failed", error=f"{type(exc).__name__}: {exc}", finish=True)
    return True


def main():
    try:
        if not minio.bucket_exists(BUCKET):
            minio.make_bucket(BUCKET)
    except Exception as exc:  # noqa: BLE001
        log.warning("MinIO 初始化异常：%s", type(exc).__name__)

    log.info("审计 worker 启动，轮询间隔 %.0fs", POLL_SECONDS)
    while True:
        try:
            if not run_once():
                time.sleep(POLL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            log.warning("轮询异常：%s %s", type(exc).__name__, exc)
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
