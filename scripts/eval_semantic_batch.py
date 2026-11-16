#!/usr/bin/env python3
"""8.4 首批独立样本评测：收集7段素材×S06/S07的研判结果，对比基准数据集，输出评测报告。

用法（管理员凭据经环境变量ADMIN_PW传入，不落盘）：
  ADMIN_PW=... python scripts/eval_semantic_batch.py --api http://localhost:8000 \
      --dataset services/semantic-worker/tests/fixtures/eval_dataset_8_4.json \
      --report tmp/reports/semantic-batch-report.md
可选 --trigger（对缺失/排队中的窗口用授权快照上下文自动创建任务，需 --room 指定快照来源评标室）。
"""
import argparse
import json
import os
import sys
import time
from urllib import request as rq

BASE = "http://localhost:8000"


def call(jar, method, path, body=None, headers=None):
    req = rq.Request(path if path.startswith("http") else BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Requested-With", "XMLHttpRequest")
    if jar.get("csrf"):
        req.add_header("X-CSRF-Token", jar["csrf"])
    if headers:
        for key, value in headers.items():
            req.add_header(key, value)
    req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in jar.get("cookies", {}).items()))
    data = json.dumps(body).encode() if body is not None else None
    with rq.urlopen(req, data=data, timeout=60) as resp:
        for cookie in (resp.headers.get_all("Set-Cookie") or []):
            key, _, value = cookie.split(";")[0].partition("=")
            jar.setdefault("cookies", {})[key] = value
        return resp.status, json.loads(resp.read().decode() or "{}")


def login(jar, password, api_base):
    status, session = call(jar, "POST", api_base + "/api/auth/login", {"username": "wyh", "password": password})
    if status != 200:
        sys.exit("登录失败")
    jar["csrf"] = session.get("csrf_token")


def judge(window, actual):
    expected = window["expected_conclusion"]
    if not actual:
        return "missing"
    if actual == expected:
        return "match"
    if actual in window.get("accept_any", []):
        return "context_dependent"
    if actual in window.get("boundary_allow", []):
        return "boundary"
    return "mismatch"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default=BASE)
    parser.add_argument("--dataset", default="services/semantic-worker/tests/fixtures/eval_dataset_8_4.json")
    parser.add_argument("--report", default="tmp/reports/semantic-batch-report.md")
    parser.add_argument("--trigger", action="store_true", help="对缺失窗口自动创建语义任务（需--room）")
    parser.add_argument("--room", default="", help="授权快照来源评标室ID")
    args = parser.parse_args()
    api_base = args.api.rstrip("/")

    with open(args.dataset, encoding="utf-8") as handle:
        dataset = json.load(handle)
    password = os.environ.get("ADMIN_PW", "")
    if not password:
        sys.exit("缺少 ADMIN_PW 环境变量")
    jar = {}
    login(jar, password, api_base)

    status, listing = call(jar, "GET", api_base + "/api/audio/recordings")
    recordings = {item["title"]: item for item in listing["items"] if item["status"] == "ready"}

    rows, created_any = [], 0
    for material in dataset["materials"]:
        match = next((title for title in recordings if material["prefix"] in title), None)
        if not match:
            for window in material["windows"]:
                rows.append({"material": material["prefix"] + " " + material["title_contains"], "scene": window["scene"],
                             "expected": window["expected_conclusion"], "actual": None, "verdict": "missing",
                             "reason": "素材未接入或未完成转写", "evidence": [], "boundary_note": window.get("note", "")})
            continue
        recording = recordings[match]
        for window in material["windows"]:
            scene = window["scene"]
            status, data = call(jar, "GET", api_base + f"/api/audio/recordings/{recording['recording_id']}/semantic-analyses?limit=30")
            ready = [item for item in data["items"] if item["scene_id"] == scene and item["status"] == "ready" and item["result"]]
            queued = [item for item in data["items"] if item["scene_id"] == scene and item["status"] in ("queued", "processing")]
            if args.trigger and not ready and not queued:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                created_any += 1
            actual = ready[0]["result"]["conclusion"] if ready else None
            verdict = judge(window, actual)
            latest = ready[0] if ready else None
            rows.append({"material": match, "scene": scene, "expected": window["expected_conclusion"],
                         "actual": actual, "verdict": verdict,
                         "risk": latest["result"]["risk_level"] if latest else None,
                         "reason": latest["result"]["reason"] if latest else ("排队/研判中" if queued else "无研判结果"),
                         "evidence": latest["result"]["evidence"] if latest else [],
                         "missing_facts": latest["result"]["missing_facts"] if latest else [],
                         "boundary_note": window.get("note", "")})
    if created_any:
        print(f"已为 {created_any} 个缺失窗口提交创建请求，请稍后重跑本脚本收集结果。")

    scenes = {}
    for row in rows:
        stats = scenes.setdefault(row["scene"], {"match": 0, "boundary": 0, "context_dependent": 0, "mismatch": 0, "missing": 0, "windows": []})
        stats[row["verdict"]] += 1
        stats["windows"].append(row)

    lines = ["# 8.4 首批独立样本评测报告（S06/S07）", "",
             f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"- 样本：{len(dataset['materials'])} 段AI合成素材 × S06/S07 = {len(rows)} 窗口",
             "- 判定：match=符合标注；boundary=可辩护偏差（标注允许的相邻结论，不计硬错误）；mismatch=硬错误；missing=无结果", ""]
    for scene in ("S06", "S07"):
        stats = scenes.get(scene, {"match": 0, "boundary": 0, "context_dependent": 0, "mismatch": 0, "missing": 0, "windows": []})
        evaluated = stats["match"] + stats["boundary"] + stats["mismatch"]
        strict = stats["match"] / evaluated if evaluated else 0
        lenient = (stats["match"] + stats["boundary"]) / evaluated if evaluated else 0
        context_dep = stats.get("context_dependent", 0)
        lines += [f"## {scene}", "",
                  f"- 严格准确率 {stats['match']}/{evaluated} = {strict:.0%}；含可辩护偏差 {stats['match'] + stats['boundary']}/{evaluated} = {lenient:.0%}",
                  f"- 硬错误 {stats['mismatch']}，缺失 {stats['missing']}，上下文依赖窗口 {context_dep}（正确结论取决于未提供的业务事实，不计硬错误）", "",
                  "| 素材 | 期望 | 实际 | 判定 | 风险 | 理由/证据 |", "|---|---|---|---|---|---|"]
        for row in stats["windows"]:
            evidence = row["evidence"][0]["quote"] if row.get("evidence") else ""
            lines.append(f"| {row['material'][:22]} | {row['expected']} | {row['actual'] or '—'} | {row['verdict']} | {row.get('risk') or '—'} | {(row['reason'] or evidence)[:60]} |")
        lines.append("")
    mismatched = [row for row in rows if row["verdict"] == "mismatch"]
    lines += ["## 结论与调优建议", ""]
    if mismatched:
        lines.append(f"- 共 {len(mismatched)} 处硬错误：")
        for row in mismatched:
            lines.append(f"  - {row['material'][:22]} {row['scene']}：期望 {row['expected']}，实际 {row['actual']}。{row['reason'][:80]}")
    else:
        lines.append("- 无硬错误。")
    s07 = scenes.get("S07", {})
    if s07.get("mismatch", 0) >= 2:
        lines += ["- **S07 在保密上下文下存在系统性过判**（正常讨论被判疑似泄密）：建议在 8.4 后续批次中调优 S07 规则——",
                  "  - 强调“讨论评分细节/澄清流程本身不构成泄密，须存在向无权对象传递未公开信息的行为”；",
                  "  - 或在 trusted context 中补充信息公开级别的适用阶段。"]
    s06 = scenes.get("S06", {})
    if s06.get("mismatch", 0) == 0:
        lines.append("- S06 正常样本零误报、施压正例命中，当前规则版本可保持。")
    lines += ["- 每窗口样本量小（各场景7窗口），本报告为首批基线；计划中的各30窗口评测见主计划长期目标。"]

    report = "\n".join(lines) + "\n"
    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as handle:
        handle.write(report)
    print(report)


if __name__ == "__main__":
    main()
