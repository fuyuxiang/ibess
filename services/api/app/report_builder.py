"""9.4 离线质检报告：纯函数，把任务行 + 同时段实时事件与人工决定拼成 JSON 报告，再渲染成可打印 HTML。

报告只并列事实，不另作结论：离线线索是「模型 / 规则建议」，实时事件的人工决定原样列出，二者按规则 id + 时间容差对照；
冻结时间轴里启用了、但离线流水线没接的场景写「未覆盖」，不写正常；任务没成功时报告标注状态，不出线索统计。
"""
from __future__ import annotations

import html
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

REPORT_VERSION = "9.4-v1"
MATCH_TOLERANCE_MS = 10_000          # 离线线索与实时事件按发生时间对照的容差（工程初值）
CONCLUSIONS = ("suspected", "insufficient", "normal")
STATIC_LIMITATIONS = (
    "测试性质：本机为桩验证（假帧源、假检测器、SQLite）；真实视频、YOLO 权重、PostgreSQL 与 60 分钟吞吐在另一台机器验证。",
    "区域配置只有当前版本没有历史，建任务时冻结的是当时的版本。",
    "证据校验只复核素材文件 SHA256 与线索证据引用的状态；证据文件的私有登记与摘要校验归 9.1，尚未接入。",
    "代码版本（模块源码摘要、检测权重、依赖库版本）取自本任务第一次运行时，不是实时那次运行的版本；与实时的差异看对照表的规则版本列。",
    "授权例外（6.3）与工位身份（6.2）事实尚未交付，未冻结进任务：快照引用了例外时，S03 按「事实缺失」出 insufficient，合法离岗不会判 normal。",
    "离线线索是规则建议，不是违规认定；是否违规以人工复核为准。",
)


def _parse(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if moment.tzinfo is None:                          # 实时 events.occurred_at 是无时区 UTC
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(moment: Optional[datetime]) -> Optional[str]:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z") if moment else None


def timeline(frozen: dict) -> list[dict]:
    start = _parse(frozen.get("media_started_at"))
    out = []
    for segment in frozen.get("segments") or []:
        snapshot = segment.get("snapshot") or {}
        out.append({
            "from_ms": segment.get("from_ms"), "to_ms": segment.get("to_ms"),
            "from_at": _iso(start + timedelta(milliseconds=segment["from_ms"])) if start and segment.get("from_ms") is not None else None,
            "to_at": _iso(start + timedelta(milliseconds=segment["to_ms"])) if start and segment.get("to_ms") is not None else None,
            "binding_status": segment.get("binding_status"), "stage_snapshot_id": snapshot.get("stage_snapshot_id"),
            "phase_id": snapshot.get("phase_id"), "phase_version": snapshot.get("phase_version"),
            "enabled_scenes": sorted(set(snapshot.get("enabled_scenes") or []) | set(snapshot.get("always_on_scenes") or [])),
            "rule_versions": dict(snapshot.get("rule_versions") or {}),
        })
    return out


def match_events(clues: list[dict], events: list[dict], decisions: dict, *, tolerance_ms: int = MATCH_TOLERANCE_MS) -> tuple[list[dict], list[str]]:
    """返回 (实时事件对照行, 只在离线出现的 suspected 线索 id)。decisions = {event_id: {"human": {...}, "system": {...}}}。"""
    suspected = [c for c in clues if c.get("conclusion") == "suspected"]
    used: set[str] = set()
    rows = []
    for event in sorted(events, key=lambda e: (_parse(e.get("occurred_at")) or datetime.min.replace(tzinfo=timezone.utc), str(e.get("event_id")))):
        at = _parse(event.get("occurred_at"))
        matched = [c["clue_id"] for c in suspected if c.get("rule_id") == event.get("skill_code") and at is not None
                   and (_parse(c.get("occurred_at")) is not None)
                   and abs((_parse(c["occurred_at"]) - at).total_seconds() * 1000) <= tolerance_ms]
        used.update(matched)
        decided = decisions.get(event.get("event_id")) or {}
        offline_versions = sorted({c.get("rule_version") for c in suspected if c["clue_id"] in matched})
        rows.append({
            "event_id": event.get("event_id"), "rule_id": event.get("skill_code"), "rule_version": event.get("skill_version"),
            "occurred_at": _iso(at), "risk_level": event.get("risk_level"), "status": event.get("status"), "reason": event.get("reason"),
            "decision": dict(decided["human"]) if decided.get("human") else None,
            "system_close": dict(decided["system"]) if decided.get("system") else None,
            "offline_reproduced": bool(matched), "matched_clue_ids": matched, "offline_rule_versions": offline_versions,
            "rule_version_same": (offline_versions == [event.get("skill_version")]) if matched else None,
        })
    offline_only = [c["clue_id"] for c in suspected if c["clue_id"] not in used]
    return rows, offline_only


def build_report(job: dict, events: Iterable[dict] = (), decisions: Optional[dict] = None, *, generated_at: Optional[datetime] = None) -> dict:
    frozen = job.get("frozen") or {}
    result = job.get("result") or {}
    status = job.get("status")
    conclusive = status == "succeeded"
    segments = timeline(frozen)
    enabled = sorted({scene for seg in segments for scene in seg["enabled_scenes"]})
    covered = sorted(result.get("covered_scenes") or [])
    scene_failures = dict(result.get("scene_failures") or {})
    failed_scenes = sorted(scene for scene in scene_failures if scene in enabled)
    not_covered = [scene for scene in enabled if scene not in covered and scene not in failed_scenes]
    clues = list(result.get("clues") or []) if conclusive else []
    summary = {scene: {c: 0 for c in CONCLUSIONS} for scene in covered}
    for clue in clues:
        counts = summary.setdefault(clue.get("scene_id"), {c: 0 for c in CONCLUSIONS})
        counts[clue.get("conclusion")] = counts.get(clue.get("conclusion"), 0) + 1
    evidence_status = Counter(ref.get("status") for clue in clues for ref in clue.get("evidence_refs") or [] if isinstance(ref, dict))
    comparison, offline_only = match_events(clues, list(events), decisions or {}) if conclusive else ([], [])
    limitations = list(STATIC_LIMITATIONS)
    if conclusive and not_covered:
        limitations.insert(0, f"以下场景在冻结时间轴里启用，但离线流水线未接入，结果是「未覆盖」，不代表正常：{'、'.join(not_covered)}。")
    if conclusive and scene_failures:
        detail = "、".join(f"{scene} {count} 次" for scene, count in sorted(scene_failures.items()))
        limitations.insert(0, f"以下场景有观察处理失败（{detail}），这些场景的线索统计不完整，计数为 0 不代表干净。")
    if not conclusive:
        limitations.insert(0, f"任务状态为 {status}，本报告不给线索统计与对照，只列任务信息与时间轴。")
        not_covered = []
    return {
        "report_version": REPORT_VERSION,
        "generated_at": _iso(generated_at or datetime.now(timezone.utc)),
        "conclusive": conclusive,
        "job": {key: job.get(key) for key in ("job_id", "session_id", "project_id", "room_id", "source_id", "binding_id", "asset_id",
                                               "status", "attempts", "max_attempts", "error", "created_by", "created_at", "finished_at", "rerun_of")},
        "versions": {"contract_version": job.get("contract_version"), "components": frozen.get("components"),
                     "sample_fps": frozen.get("sample_fps"), "zone_config_version": (frozen.get("zone_config") or {}).get("config_version")},
        "timeline": segments,
        "coverage": {"enabled_scenes": enabled, "covered_scenes": covered if conclusive else [], "not_covered_scenes": not_covered,
                     "failed_scenes": failed_scenes if conclusive else [], "scene_failures": scene_failures if conclusive else {}},
        "clue_summary": summary if conclusive else {},
        "clues": [{key: clue.get(key) for key in ("clue_id", "scene_id", "rule_id", "rule_version", "conclusion", "risk_level", "occurred_at",
                                                    "start_ms", "end_ms", "track_id", "seat_id", "person_id", "reason", "missing_facts")}
                  for clue in clues],
        "realtime_comparison": comparison,
        "offline_only_clue_ids": offline_only,
        "evidence_check": {"asset": result.get("asset_check"), "evidence_ref_status": dict(evidence_status)},
        "failures": list(result.get("failures") or []) if conclusive else [],
        "stats": dict(result.get("stats") or {}),
        "limitations": limitations,
    }


# ---------------------------------------------------------------- HTML
_CSS = """
:root{--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--bg:#fff;--warn:#9a6700;--bad:#cf222e;--ok:#1a7f37}
body{font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:var(--fg);background:var(--bg);margin:24px auto;max-width:1100px;padding:0 16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
.meta{color:var(--muted)}table{border-collapse:collapse;width:100%;margin:8px 0}th,td{border:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}
th{background:#f6f8fa}.banner{border:1px solid var(--warn);color:var(--warn);padding:8px 12px;margin:12px 0}
.suspected{color:var(--bad)}.normal{color:var(--ok)}.insufficient{color:var(--warn)}code{font-size:12px}
@media print{body{margin:0;max-width:none}h2{break-after:avoid}tr{break-inside:avoid}}
"""


def _cell(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, (list, tuple)):
        return html.escape("、".join(str(v) for v in value)) or "—"
    if isinstance(value, dict):
        return html.escape("；".join(f"{k}={v}" for k, v in value.items())) or "—"
    return html.escape(str(value))


def _table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return '<p class="meta">无</p>'
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{_cell(v)}</td>" for v in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_html(report: dict) -> str:
    job = report["job"]
    parts = [
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        f"<title>离线质检报告 {_cell(job.get('job_id'))}</title><style>{_CSS}</style></head><body>",
        "<h1>离线质检报告</h1>",
        f"<p class=\"meta\">任务 <code>{_cell(job.get('job_id'))}</code> · 场次 <code>{_cell(job.get('session_id'))}</code> · "
        f"源 <code>{_cell(job.get('source_id'))}</code> · 素材 <code>{_cell(job.get('asset_id'))}</code> · 状态 {_cell(job.get('status'))} · "
        f"生成于 {_cell(report.get('generated_at'))} · 报告版本 {_cell(report.get('report_version'))}</p>",
    ]
    if not report["conclusive"]:
        parts.append(f"<div class=\"banner\">任务未成功（{_cell(job.get('status'))}，{_cell(job.get('error'))}），本报告不给结论。</div>")
    parts.append("<h2>限制</h2><ul>" + "".join(f"<li>{_cell(item)}</li>" for item in report["limitations"]) + "</ul>")
    versions = report["versions"]
    parts.append("<h2>冻结版本</h2>" + _table(["项", "值"], [["契约", versions.get("contract_version")], ["采样帧率", versions.get("sample_fps")],
                                                          ["区域配置版本", versions.get("zone_config_version")]]
                                         + [[name, info] for name, info in sorted((versions.get("components") or {}).items())]))
    parts.append("<h2>阶段时间轴</h2>" + _table(["起（ms）", "止（ms）", "起（UTC）", "止（UTC）", "绑定", "阶段", "阶段版本", "启用场景", "规则版本"],
                                            [[s["from_ms"], s["to_ms"], s["from_at"], s["to_at"], s["binding_status"], s["phase_id"], s["phase_version"],
                                              s["enabled_scenes"], s["rule_versions"]] for s in report["timeline"]]))
    coverage = report["coverage"]
    parts.append("<h2>场景覆盖</h2>" + _table(["启用", "离线已覆盖", "处理失败（结果不完整）", "未覆盖（不代表正常）"],
                                          [[coverage["enabled_scenes"], coverage["covered_scenes"], coverage["scene_failures"] or None,
                                            coverage["not_covered_scenes"]]]))
    if report["conclusive"]:
        parts.append("<h2>线索统计（模型 / 规则建议）</h2>" + _table(["场景", "suspected", "insufficient", "normal"],
                                                            [[scene, c["suspected"], c["insufficient"], c["normal"]] for scene, c in sorted(report["clue_summary"].items())]))
        parts.append("<h2>线索明细</h2>" + _table(["发生时间", "场景", "规则", "结论", "等级", "原因", "缺失事实", "线索 id"],
                                              [[c["occurred_at"], c["scene_id"], f"{c['rule_id']} {c['rule_version']}", c["conclusion"], c["risk_level"],
                                                c["reason"], c["missing_facts"], c["clue_id"]] for c in report["clues"]]))
        parts.append("<h2>实时事件与人工决定对照</h2>" + _table(
            ["发生时间", "规则（实时版本）", "实时状态", "人工决定", "复核人", "备注", "系统关闭", "离线是否复现", "离线规则版本"],
            [[r["occurred_at"], f"{r['rule_id']} {r['rule_version']}", r["status"], (r["decision"] or {}).get("action"),
              (r["decision"] or {}).get("reviewer_id"), (r["decision"] or {}).get("note"), (r["system_close"] or {}).get("action"),
              "是" if r["offline_reproduced"] else "否",
              None if r["rule_version_same"] is None else (f"{'、'.join(r['offline_rule_versions'])}（{'一致' if r['rule_version_same'] else '不一致'}）")]
             for r in report["realtime_comparison"]]))
        parts.append("<h2>只在离线出现的 suspected 线索</h2>" + _table(["线索 id"], [[cid] for cid in report["offline_only_clue_ids"]]))
        parts.append("<h2>处理失败（不计入线索）</h2>" + _table(["观察", "场景", "代码", "说明"],
                                                     [[f.get("observation_id"), f.get("scene_id"), f.get("code"), f.get("message")] for f in report["failures"]]))
    evidence = report["evidence_check"]
    asset = evidence.get("asset") or {}
    parts.append("<h2>证据校验</h2>" + _table(["项", "结果"], [["素材 SHA256 复核", asset.get("status")], ["登记摘要", asset.get("expected_sha256")],
                                                         ["实际摘要", asset.get("actual_sha256")], ["线索证据引用状态", evidence.get("evidence_ref_status")]]))
    parts.append("<h2>运行统计</h2>" + _table(["项", "值"], [[k, v] for k, v in sorted(report["stats"].items())]))
    parts.append("</body></html>")
    return "".join(parts)
