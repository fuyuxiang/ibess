#!/usr/bin/env bash
# 在本机（Windows Git Bash 也可）导出演示数据，打包成可拷贝的目录
#
#   bash deploy/linux/export-demo-data.sh            # 默认近 3 天证据
#   DAYS=7 bash deploy/linux/export-demo-data.sh     # 近 7 天
#
# 产物：deploy/linux/out/
#   db.dump          数据库全量（约 135 MB）
#   evidence/        对象存储里的截图与片段（近 N 天）
#   models_cache.tar 语音模型缓存（2.1 GB，服务器可跳过改为在线下载）
#   code.tar.gz      可分发源码（不含凭据、原始媒体、运行数据和模型）
set -euo pipefail

cd "$(dirname "$0")/../.."
source scripts/load-env.sh
load_poc_env .env
: "${MINIO_ROOT_PASSWORD:?请在本机 .env 配置 MINIO_ROOT_PASSWORD}"
DAYS="${DAYS:-3}"

# Git Bash 下 docker -v 认不出 /d/... 形式，转成 D:/... 才能挂载
hostpath() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }
OUT="deploy/linux/out"
mkdir -p "$OUT"

echo "== 1. 导出数据库 =="
docker exec poc-postgres pg_dump -U demo -d demo -Fc > "$OUT/db.dump"
echo "   $(du -h "$OUT/db.dump" | cut -f1)"

echo
echo "== 2. 导出对象存储（近 ${DAYS} 天）=="
mkdir -p "$OUT/evidence"
# 复用 minio 容器里自带的 mc，避免再拉一个 mc 镜像（离线环境拉不到）
docker exec poc-minio sh -c 'mc alias set ex http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1; rm -rf /tmp/exp; mkdir -p /tmp/exp; mc mirror --newer-than "$1"d --quiet ex/evidence /tmp/exp' sh "$DAYS"
rm -rf "$OUT/evidence"; mkdir -p "$OUT/evidence"
docker cp poc-minio:/tmp/exp/. "$OUT/evidence/"
docker exec poc-minio rm -rf /tmp/exp
count=$(find "$OUT/evidence" -type f | wc -l | tr -d ' ')
if [ "$count" = "0" ]; then
  echo "   警告：没有导出任何对象（检查 minio 是否在跑、.env 里的凭据是否一致）"
else
echo "   ${count} 个文件 / $(du -sh "$OUT/evidence" | cut -f1)"
fi

echo
echo "== 3. 语音模型缓存 =="
docker run --rm -v demo_models_cache:/cache:ro -v "$(pwd)/$OUT:/out" alpine \
  tar -cf /out/models_cache.tar -C /cache . 2>/dev/null || \
  tar -cf "$OUT/models_cache.tar" -C ../models_cache . 2>/dev/null || \
  echo "   跳过（找不到 models_cache，服务器可在线下载）"
[ -f "$OUT/models_cache.tar" ] && echo "   $(du -h "$OUT/models_cache.tar" | cut -f1)"

echo
echo "== 4. 打包源码 =="
"${POC_PYTHON:-python3}" scripts/package_source.py --output "$OUT/code.tar.gz"
echo "   $(du -h "$OUT/code.tar.gz" | cut -f1)"

echo
echo "== 完成 =="
du -sh "$OUT"
echo
echo "拷贝到服务器（在 out 目录下执行）："
if [ -n "${DEPLOY_SSH_HOST:-}" ] && [ -n "${DEPLOY_SSH_USER:-}" ] && [ -n "${DEPLOY_REMOTE_DIR:-}" ]; then
  echo "  scp -r . ${DEPLOY_SSH_USER}@${DEPLOY_SSH_HOST}:${DEPLOY_REMOTE_DIR}/"
else
  echo "  请先在 .env 配置 DEPLOY_SSH_HOST、DEPLOY_SSH_USER 和 DEPLOY_REMOTE_DIR"
fi
echo
echo "然后在服务器上："
echo "  在服务器代码目录执行：bash deploy/linux/import-on-linux.sh <数据包目录>"
