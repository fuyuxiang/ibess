#!/usr/bin/env python3
"""萤石摄像头 → mediamtx 的桥接守护。

萤石开放平台给的是有时效的 RTMP 地址（实测 FLV 协议受并发/频率限制，
RTMP 更稳），这里循环：取地址 → ffmpeg 转推到 mediamtx 的指定 path →
断开或地址过期就重取重连。

用法（宿主机，需能访问 open.ys7.com 与 Docker）：
  python scripts/ezviz_bridge.py [--path cam-ezviz] [--quality 2]

凭据从 文档/萤石摄像/.env 读取，保持和播放器项目同一份配置。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

def env_file() -> Path | None:
    """宿主机上凭据放在项目里的固定位置；容器内层级不足，返回 None 走环境变量。"""
    try:
        return Path(__file__).resolve().parents[2] / "文档" / "萤石摄像" / ".env"
    except IndexError:
        return None
API = "https://open.ys7.com"
RTSP_BASE = "rtsp://mediamtx:8554"
CONTAINER = "poc-media-service"          # 复用它的 ffmpeg，避免再建镜像
PUSH_TIMEOUT = 540                       # 地址有效期 600s，留余量主动重连


def load_env() -> dict:
    env = {}
    # 容器部署时没有 .env 文件，先看环境变量
    for key in ("EZVIZ_APP_KEY", "EZVIZ_APP_SECRET", "EZVIZ_DEVICE_SERIAL", "EZVIZ_CHANNEL_NO"):
        if os.getenv(key):
            env[key] = os.getenv(key, "")
    if all(env.get(k) for k in ("EZVIZ_APP_KEY", "EZVIZ_APP_SECRET", "EZVIZ_DEVICE_SERIAL")):
        return env
    target = env_file()
    if target is None or not target.exists():
        sys.exit("找不到凭据文件（容器部署请通过 -e 传 EZVIZ_APP_KEY / SECRET / DEVICE_SERIAL）")
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for key in ("EZVIZ_APP_KEY", "EZVIZ_APP_SECRET", "EZVIZ_DEVICE_SERIAL"):
        if not env.get(key):
            sys.exit(f"凭据缺失：{key}")
    return env


def post(path: str, data: dict) -> dict:
    req = urllib.request.Request(
        API + path,
        data=urllib.parse.urlencode(data).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def stream_url(env: dict, quality: int, expire: int = 600) -> str:
    token = post("/api/lapp/token/get", {
        "appKey": env["EZVIZ_APP_KEY"], "appSecret": env["EZVIZ_APP_SECRET"],
    }).get("data", {}).get("accessToken", "")
    if not token:
        raise RuntimeError("未取到 accessToken")
    result = post("/api/lapp/v2/live/address/get", {
        "accessToken": token,
        "deviceSerial": env["EZVIZ_DEVICE_SERIAL"],
        "channelNo": env.get("EZVIZ_CHANNEL_NO", "1"),
        "protocol": 3,             # 3 = RTMP，实测比 FLV 稳
        "quality": quality,
        "expireTime": expire,
        "type": 1,
    })
    url = (result.get("data") or {}).get("url", "")
    if not url:
        raise RuntimeError(f"未取到播放地址：{result.get('code')} {result.get('msg')}")
    return url


def push(url: str, path: str, mode: str = "docker") -> int:
    ffmpeg = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-rw_timeout", "15000000",
        "-i", url,
        "-c", "copy",
        "-f", "rtsp", "-rtsp_transport", "tcp",
        f"{RTSP_BASE}/{path}",
    ]
    # 容器内没有 docker CLI，直接调本机 ffmpeg
    cmd = ["docker", "exec", "-i", CONTAINER] + ffmpeg if mode == "docker" else ffmpeg
    return subprocess.run(cmd, timeout=PUSH_TIMEOUT).returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default="cam-ezviz", help="mediamtx 中的路径名")
    parser.add_argument("--quality", type=int, default=2, choices=[1, 2, 3])
    parser.add_argument("--ffmpeg-mode", default=os.getenv("BRIDGE_FFMPEG_MODE", "docker"),
                        choices=["docker", "direct"],
                        help="docker=宿主机执行 docker exec 调容器内 ffmpeg；direct=容器内部直跑")
    args = parser.parse_args()

    env = load_env()
    print(f"桥接启动：设备 {env['EZVIZ_DEVICE_SERIAL']} → {args.path}（quality={args.quality}）", flush=True)

    failures = 0
    while True:
        try:
            url = stream_url(env, args.quality)
            print(f"[{time.strftime('%H:%M:%S')}] 取得地址，开始推流", flush=True)
            code = push(url, args.path, args.ffmpeg_mode)
            print(f"[{time.strftime('%H:%M:%S')}] 推流结束（code={code}），重连", flush=True)
            failures = 0
        except subprocess.TimeoutExpired:
            print(f"[{time.strftime('%H:%M:%S')}] 到达有效期上限，主动重连", flush=True)
            failures = 0
        except Exception as exc:                      # noqa: BLE001 — 守护进程不能退出
            failures += 1
            wait = min(60, 5 * failures)
            print(f"[{time.strftime('%H:%M:%S')}] 异常：{type(exc).__name__} {exc}，{wait}s 后重试", flush=True)
            time.sleep(wait)
            continue
        time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
