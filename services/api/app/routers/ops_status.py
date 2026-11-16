"""算法能力状态（可视化）：算子矩阵 + 各算法近1小时命中 + 运行状态。

供前端"算法能力矩阵"卡展示：让评审方一眼看到系统在跑哪些算法、阈值多少、近期命中。
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_session

router = APIRouter()


@router.get("/ops/algorithm-status")
def algorithm_status(session: Session = Depends(get_session)):
    operators = session.execute(text(
        "select code, name, enabled, input_types, output_event_type, risk_level, parameters "
        "from operators order by code")).mappings().all()
    hit_rows = session.execute(text(
        "select event_type, count(*) as hits from events "
        "where created_at > now() - interval '1 hour' group by event_type")).mappings().all()
    hits = {row["event_type"]: row["hits"] for row in hit_rows}
    semantic = session.execute(text(
        "select count(*) filter (where status='ready') as ready, count(*) as total, "
        "count(*) filter (where status='ready' and result->>'conclusion'='suspected') as suspected "
        "from semantic_analyses where created_at > now() - interval '7 days'")).mappings().first()
    recordings = session.execute(text(
        "select count(*) filter (where status='ready') as ready, count(*) as total from audio_recordings"
    )).mappings().first()
    qa_runs = session.execute(text("select count(*) from offline_qa_runs")).scalar()

    items = []
    for row in operators:
        params = row["parameters"] or {}
        threshold = None
        if row["code"] == "crowd_gathering":
            threshold = f"人数 ≥ {params.get('count_threshold', '?')}"
        elif row["code"] == "seat_absence":
            threshold = f"无人 ≥ {params.get('absence_seconds', '?')} 秒"
        elif row["code"] == "zone_intrusion":
            threshold = "进入限制区域"
        items.append({
            "code": row["code"], "name": row["name"], "enabled": bool(row["enabled"]),
            "input": ",".join(row["input_types"] or []), "output": row["output_event_type"],
            "risk_level": row["risk_level"], "threshold": threshold,
            "recent_hits": hits.get(row["output_event_type"], 0),
        })
    capabilities = [
        {"key": "vision", "name": "YOLO 人员检测", "status": "running",
         "detail": "多路 RTSP 抽帧检测（2fps）", "metric": f"{hits.get('person_detected', 0)} 条观察/小时"},
        {"key": "semantic", "name": "文本语义研判（S06/S07）", "status": "ready",
         "detail": f"近7天任务 {semantic['total']}，就绪 {semantic['ready']}",
         "metric": f"疑似 {semantic['suspected']}"},
        {"key": "acoustic", "name": "声学能量分析（S09）", "status": "ready",
         "detail": "RMS 包络突发检测（离线）", "metric": "按需分析"},
        {"key": "face", "name": "人脸核验（S04）", "status": "ready",
         "detail": "人员端核验+事实关联", "metric": "按需核验"},
        {"key": "audio", "name": "语音转写（Paraformer/VAD）", "status": "ready",
         "detail": f"录音 {recordings['ready']}/{recordings['total']} 就绪", "metric": "A/B 说话人分段"},
        {"key": "qa", "name": "离线质检", "status": "ready",
         "detail": f"质检运行 {qa_runs} 次", "metric": "单文件单任务"},
    ]
    return {"generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "operators": items, "capabilities": capabilities,
            "recent_hour": {key: value for key, value in sorted(hits.items(), key=lambda pair: -pair[1])}}
