"""执行卡 7.1 / T1.4：视觉复核模型可运行性检查。不下载模型、不改配置；缺任一项即 restricted，不冒充可运行。

用法：python deploy/visual-model/check_runnability.py [--output <json>] [--probe]
检查：GPU（nvidia-smi）、配置（VISUAL_MODEL_*）、模型服务（GET {base_url}/models 含模型 ID）、可选最小多模态请求延迟（--probe）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REQUIRED_ENV = ("VISUAL_MODEL_BASE_URL", "VISUAL_MODEL_NAME")


def check_gpu(runner=subprocess.run) -> dict:
    binary = shutil.which("nvidia-smi")
    if not binary:
        return {"ok": False, "detail": "nvidia-smi 不在 PATH（本机无 NVIDIA 驱动或无 GPU）"}
    try:
        result = runner([binary, "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": f"nvidia-smi 执行失败: {type(exc).__name__}"}
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    if result.returncode != 0 or not lines:
        return {"ok": False, "detail": "nvidia-smi 没有列出 GPU"}
    return {"ok": True, "detail": lines}


def check_config(env: dict) -> dict:
    missing = [key for key in REQUIRED_ENV if not env.get(key, "").strip()]
    key_present = bool(env.get("VISUAL_MODEL_API_KEY", "").strip()) or (
        env.get("VISUAL_MODEL_API_KEY_FILE", "").strip() and Path(env["VISUAL_MODEL_API_KEY_FILE"]).is_file())
    if not key_present:
        missing.append("VISUAL_MODEL_API_KEY 或 VISUAL_MODEL_API_KEY_FILE")
    return {"ok": not missing, "detail": {"missing": missing}}


def check_service(env: dict, opener=urllib.request.urlopen, timeout: float = 5.0) -> dict:
    base = env.get("VISUAL_MODEL_BASE_URL", "").strip().rstrip("/")
    model = env.get("VISUAL_MODEL_NAME", "").strip()
    if not base or not model:
        return {"ok": False, "detail": "配置缺失，跳过"}
    headers = {"Accept": "application/json"}
    key = env.get("VISUAL_MODEL_API_KEY", "").strip()
    key_file = env.get("VISUAL_MODEL_API_KEY_FILE", "").strip()
    if not key and key_file and Path(key_file).is_file():
        key = Path(key_file).read_text(encoding="utf-8").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"          # 要求鉴权的服务不带头会 401，被误判为不可用
    request = urllib.request.Request(base + "/models", headers=headers)
    try:
        with opener(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "detail": f"HTTP_{exc.code}"}
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        return {"ok": False, "detail": f"服务不可达: {type(exc).__name__}"}
    ids = [item.get("id") for item in (data.get("data") or []) if isinstance(item, dict)] if isinstance(data, dict) else []
    if model not in ids:
        return {"ok": False, "detail": f"服务未列出模型 {model}（列出 {len(ids)} 个）"}
    return {"ok": True, "detail": f"模型 {model} 已在服务列表"}


def probe_latency(env: dict, opener=urllib.request.urlopen, timeout: float = 60.0) -> dict:
    """最小多模态请求（1×1 像素图片），只测通路与延迟，不评估效果。"""
    base = env.get("VISUAL_MODEL_BASE_URL", "").strip().rstrip("/")
    model = env.get("VISUAL_MODEL_NAME", "").strip()
    key = env.get("VISUAL_MODEL_API_KEY", "").strip()
    if not key and env.get("VISUAL_MODEL_API_KEY_FILE", "").strip():
        key = Path(env["VISUAL_MODEL_API_KEY_FILE"]).read_text(encoding="utf-8").strip()
    if not base or not model or not key:
        return {"ok": False, "detail": "配置缺失，跳过"}
    tiny = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    body = json.dumps({"model": model, "max_tokens": 5, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "回答 ok"}, {"type": "image_url", "image_url": {"url": tiny}}]}]}).encode("utf-8")
    request = urllib.request.Request(base + "/chat/completions", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    started = time.monotonic()
    try:
        with opener(request, timeout=timeout) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        return {"ok": False, "detail": f"HTTP_{exc.code}"}
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return {"ok": False, "detail": f"请求失败: {type(exc).__name__}"}
    return {"ok": True, "detail": {"latency_ms": int((time.monotonic() - started) * 1000)}}


def run(env: Optional[dict] = None, *, probe: bool = False, runner=subprocess.run, opener=urllib.request.urlopen) -> dict:
    env = dict(os.environ if env is None else env)
    checks = {"gpu": check_gpu(runner), "config": check_config(env), "service": check_service(env, opener)}
    if probe:
        checks["probe"] = probe_latency(env, opener)
    missing = [name for name, result in checks.items() if not result["ok"]]
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "status": "ok" if not missing else "restricted",
        "missing": missing,
        "checks": checks,
        "conditions_to_lift": {
            "gpu": "在有 NVIDIA GPU 的机器上运行；与 ASR 共用时先测显存（K-10）",
            "config": "设置 VISUAL_MODEL_BASE_URL / VISUAL_MODEL_NAME / VISUAL_MODEL_API_KEY(_FILE)",
            "service": "启动 OpenAI 兼容的多模态服务（见 vllm-compose.example.yml）并确认 /models 列出模型",
            "probe": "服务可达后用 --probe 测一次最小请求延迟",
        },
        "nature": "runnability-only; 不评估识别效果",
    }
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="视觉复核模型可运行性检查")
    parser.add_argument("--output", help="把报告写到此 JSON 文件（不删除历史，建议带日期）")
    parser.add_argument("--probe", action="store_true", help="服务可达时发一次最小多模态请求测延迟")
    args = parser.parse_args(argv)
    report = run(probe=args.probe)
    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(text)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    return 0 if report["status"] == "ok" else 3


if __name__ == "__main__":
    sys.exit(main())
