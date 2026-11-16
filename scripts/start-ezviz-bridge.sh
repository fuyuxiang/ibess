#!/usr/bin/env bash
# 启动萤石摄像头 → mediamtx 的桥接容器（常驻，断线自动重连）
#
# 用法：
#   bash POC/scripts/start-ezviz-bridge.sh [quality]
#   quality: 1 / 2 / 3，实测三档拿到的都是 512×288，取值不影响画质
#
# 凭据从 文档/萤石摄像/.env 读取，不在命令行暴露。
# 设备离线时容器会持续重试（5s 起、最长 60s），设备通电联网后自动出流。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="$ROOT/文档/萤石摄像/.env"
SCRIPT="$ROOT/POC/scripts/ezviz_bridge.py"
QUALITY="${1:-2}"

[ -f "$ENV_FILE" ] || { echo "缺少凭据文件：$ENV_FILE"; exit 1; }
[ -f "$SCRIPT" ] || { echo "缺少桥接脚本：$SCRIPT"; exit 1; }

read_env() {
  grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"'\''\r' | sed 's/[[:space:]]*$//'
}

APP_KEY="$(read_env EZVIZ_APP_KEY)"
APP_SECRET="$(read_env EZVIZ_APP_SECRET)"
SERIAL="$(read_env EZVIZ_DEVICE_SERIAL)"
CHANNEL="$(read_env EZVIZ_CHANNEL_NO)"

[ -n "$APP_KEY" ] && [ -n "$APP_SECRET" ] && [ -n "$SERIAL" ] || {
  echo "凭据不完整，请检查 $ENV_FILE"; exit 1
}

export MSYS_NO_PATHCONV=1
docker rm -f poc-ezviz-bridge >/dev/null 2>&1 || true

docker run -d --name poc-ezviz-bridge \
  --network poc_default --restart unless-stopped \
  -v "$SCRIPT:/app/bridge.py:ro" \
  -e EZVIZ_APP_KEY="$APP_KEY" \
  -e EZVIZ_APP_SECRET="$APP_SECRET" \
  -e EZVIZ_DEVICE_SERIAL="$SERIAL" \
  -e EZVIZ_CHANNEL_NO="${CHANNEL:-1}" \
  --entrypoint python \
  poc-inference-base:latest \
  /app/bridge.py --path cam-ezviz --quality "$QUALITY" --ffmpeg-mode direct

echo "已启动 poc-ezviz-bridge（quality=$QUALITY）"
echo "查看状态：docker logs -f poc-ezviz-bridge"
echo "页面地址：http://localhost:3000/camera-live"
