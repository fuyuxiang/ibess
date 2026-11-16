#!/usr/bin/env bash
# 前端部署（可靠路径）：本地构建 + 灌入运行中的 web-console 容器 + 精确清理历史资源。
# 背景：容器内构建有层缓存问题；多次 docker cp 会累积历史 chunk，浏览器缓存了旧
# index.html 时会一直命中旧代码。懒加载资源每次构建都会更新 hash，
# 因此按本次构建清单清理历史资源。
# 用法：bash deploy-web.sh
set -eu
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
source "$PROJECT_ROOT/scripts/load-env.sh"
load_poc_env "$PROJECT_ROOT/.env"
cd "$PROJECT_ROOT/web-console"
if [ -n "${POC_NODE_BIN:-}" ]; then
  export PATH="$POC_NODE_BIN:$PATH"
fi
command -v npm >/dev/null 2>&1 || { echo "找不到 npm；请配置 PATH 或 .env 中的 POC_NODE_BIN"; exit 1; }
npm run build

# 先灌新产物，再删容器里本次产物之外的文件（顺序反过来会出现短暂 404 窗口）
docker cp dist/. poc-web-console:/usr/share/nginx/html/

CONTAINER_ASSETS="$(mktemp)"
LOCAL_ASSETS="$(mktemp)"
trap 'rm -f "$CONTAINER_ASSETS" "$LOCAL_ASSETS"' EXIT
docker exec poc-web-console sh -c "ls /usr/share/nginx/html/assets" 2>/dev/null | tr -d '\r' | sort > "$CONTAINER_ASSETS" || true
ls dist/assets | sort > "$LOCAL_ASSETS"
STALE="$(comm -23 "$CONTAINER_ASSETS" "$LOCAL_ASSETS" | tr '\n' ' ')"
if [ -n "$STALE" ]; then
  docker exec poc-web-console sh -c "cd /usr/share/nginx/html/assets && rm -f $STALE"
  echo "已清理历史资源 $(echo "$STALE" | wc -w) 个"
fi
echo "已部署。强制刷新浏览器（Ctrl+F5）查看。"
