#!/bin/bash
# 一键停止：智能评标现场视频监测 POC
# 用法: bash scripts/stop_all.sh
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

echo "停止应用服务与基础设施..."
docker compose down 2>/dev/null || true

echo "停止推流容器..."
docker rm -f poc-publisher 2>/dev/null || true

echo "停止前端 dev server..."
pkill -f "npm run dev" 2>/dev/null || true

echo "已全部停止"
