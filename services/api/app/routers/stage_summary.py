"""评标阶段总览：当前场次、九阶段时间轴与事件统计。

数据来源：
- eval_sessions / phase_snapshots：阶段时间轴（谁在什么时候切到哪一步）
- events.phase_id：事件归属阶段，由 event-service 落库时按"当前生效快照"写入

边界：本接口只做汇总，不改阶段配置，也不改事件状态。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from app.db import get_session

router = APIRouter(tags=["stages"])

PHASES = [
    ("SIGNIN", "入场签到"),
    ("DISCIPLINE", "宣读纪律"),
    ("PRELIM", "初步评审"),
    ("DETAIL", "详细评审"),
    ("CLARIFY", "澄清讨论"),
    ("REPORT", "收尾宣读"),
]
TITLES = dict(PHASES)
ORDER = [pid for pid, _ in PHASES]


def _iso(value):
    """时间统一带时区输出：库里的 start_at 是 naive UTC，直接 isoformat 会被前端当本地时间解析。"""
    if not value:
        return None
    if value.tzinfo is None:
        from datetime import timezone as _tz
        return value.replace(tzinfo=_tz.utc).isoformat().replace("+00:00", "Z")
    return value.isoformat()


@router.get("/stages/summary")
def stage_summary(session_id: Optional[str] = Query(None, max_length=64), session=Depends(get_session)):
    if session_id:
        target = session.execute(text(
            "select session_id, name, status, current_phase, start_at, expected_people from eval_sessions "
            "where session_id = :sid"
        ), {"sid": session_id}).mappings().first()
    else:
        target = session.execute(text(
            "select session_id, name, status, current_phase, start_at, expected_people from eval_sessions "
            "where status in ('active','running') order by updated_at desc limit 1"
        )).mappings().first()
        if not target:
            target = session.execute(text(
                "select session_id, name, status, current_phase, start_at, expected_people from eval_sessions "
                "order by updated_at desc limit 1"
            )).mappings().first()

    if not target:
        return {"session": None, "phases": [], "total": {"events": 0, "high": 0, "medium": 0}}

    sid = target["session_id"]

    snaps = session.execute(text("""
        select phase_id, phase_version, effective_at, enabled_scenes, rule_versions, reason, actor_id
        from phase_snapshots where session_id = :sid order by effective_at
    """), {"sid": sid}).mappings().all()

    # 只统计本场次开始之后的事件，避免把上一场同阶段的历史记录算进来
    started = target["start_at"]
    stats = {
        row["phase_id"]: row for row in session.execute(text("""
            select phase_id,
                   count(*) as total,
                   count(*) filter (where risk_level = 'high') as high,
                   count(*) filter (where risk_level = 'medium') as medium,
                   count(*) filter (where risk_level = 'low') as low,
                   count(*) filter (where status = 'PENDING') as pending,
                   count(*) filter (where status = 'CONFIRMED') as confirmed,
                   count(*) filter (where status = 'CLOSED') as closed,
                   count(*) filter (where status = 'FALSE_POSITIVE') as false_positive
            from events
            where phase_id is not null
              and session_id = :sid
              and event_type <> 'person_detected'
              and (cast(:started as timestamp) is null or occurred_at >= cast(:started as timestamp))
            group by phase_id
        """), {"started": started, "sid": sid}).mappings().all()
    }

    # 每个阶段取最后一次快照（同一阶段可能因版本升级被重复写入）
    latest = {}
    for snap in snaps:
        latest[snap["phase_id"]] = snap

    # 计算每个阶段的起止：进入 = 该阶段首次生效，离开 = 下一个阶段的首次生效
    first_seen = {}
    for snap in snaps:
        first_seen.setdefault(snap["phase_id"], snap["effective_at"])

    current = target["current_phase"]
    current_index = ORDER.index(current) if current in ORDER else -1

    phases = []
    for pid, title in PHASES:
        snap = latest.get(pid)
        row = stats.get(pid) or {}
        entered = first_seen.get(pid)
        later = [first_seen[p] for p in ORDER if p in first_seen and first_seen[p] > entered] if entered else []
        left = min(later) if later else None

        position = ORDER.index(pid)
        if pid == current:
            state = "current"
            left = None  # 进行中的阶段不显示结束时间，避免阶段回退后出现"结束早于开始"
        elif entered and current_index >= 0 and position > current_index:
            # 历史演示里来回切过阶段，会留下"未来阶段"的快照；本轮还没走到，按未开始显示
            state = "pending"
        elif entered:
            state = "done"
        else:
            state = "pending"

        phases.append({
            "phase_id": pid,
            "title": title,
            "state": state,
            "entered_at": _iso(entered),
            "left_at": _iso(left),
            "event_count": row.get("total", 0),
            "high": row.get("high", 0),
            "medium": row.get("medium", 0),
            "low": row.get("low", 0),
            "pending": row.get("pending", 0),
            "confirmed": row.get("confirmed", 0),
            "closed": row.get("closed", 0),
            "false_positive": row.get("false_positive", 0),
            "version": snap["phase_version"] if snap else None,
            "scenes": list(snap["enabled_scenes"] or []) if snap else [],
            "rule_versions": dict(snap["rule_versions"] or {}) if snap else {},
            "reason": snap["reason"] if snap else None,
            "actor": snap["actor_id"] if snap else None,
        })

    # 项目信息条：绑定的项目/评标室，取不到就留空，不编造
    project = session.execute(text(
        "select p.code, p.name, p.owner, r.name as room_name "
        "from phase_bindings b "
        "left join business_projects p on p.project_id = b.project_id "
        "left join business_rooms r on r.room_id = b.room_id "
        "where b.session_id = :sid order by b.created_at desc limit 1"), {"sid": sid}).mappings().first()

    total = {
        "events": sum(p["event_count"] for p in phases),
        "high": sum(p["high"] for p in phases),
        "medium": sum(p["medium"] for p in phases),
        "pending": sum(p["pending"] for p in phases),
        "confirmed": sum(p["confirmed"] for p in phases),
        "closed": sum(p["closed"] for p in phases),
        "false_positive": sum(p["false_positive"] for p in phases),
    }

    return {
        "project": {
            "code": project["code"] if project else None,
            "name": project["name"] if project else None,
            "owner": project["owner"] if project else None,
            "room_name": project["room_name"] if project else None,
        },
        "session": {
            "session_id": sid,
            "name": target["name"],
            "status": target["status"],
            "current_phase": current,
            "current_title": TITLES.get(current, current),
            "start_at": _iso(target["start_at"]),
            "expected_people": target.get("expected_people"),
        },
        "phases": phases,
        "total": total,
    }
