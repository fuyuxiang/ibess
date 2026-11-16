#!/usr/bin/env bash
# 在 Linux 服务器上导入演示数据（需先按 README 解包代码并启动过数据库）
#
#   bash deploy/linux/import-on-linux.sh /root/poc-transfer
set -euo pipefail

SRC="${1:-/root/poc-transfer}"
cd "$(dirname "$0")/../.."
source scripts/load-env.sh
load_poc_env .env

if [ ! -d "$SRC" ]; then echo "找不到数据目录：$SRC"; exit 1; fi
COMPOSE="docker compose -f docker-compose.yml -f deploy/linux/docker-compose.linux.yml"

echo "== 1. 确保数据库已启动 =="
$COMPOSE up -d postgres
sleep 8
until docker exec poc-postgres pg_isready -U demo >/dev/null 2>&1; do sleep 2; done
echo "   OK"

echo
echo "== 2. 恢复数据库（会覆盖现有数据）=="
if [ -f "$SRC/db.dump" ]; then
  docker exec -i poc-postgres psql -U demo -d postgres -c \
    "select pg_terminate_backend(pid) from pg_stat_activity where datname='demo' and pid<>pg_backend_pid();" >/dev/null
  docker exec -i poc-postgres psql -U demo -d postgres -c "drop database if exists demo;" >/dev/null
  docker exec -i poc-postgres psql -U demo -d postgres -c "create database demo;" >/dev/null
  docker exec -i poc-postgres pg_restore -U demo -d demo --no-owner --no-privileges < "$SRC/db.dump"
  echo "   已恢复 $(du -h "$SRC/db.dump" | cut -f1)"
else
  echo "   跳过：没有 db.dump"
fi

echo
echo "== 3. 导入证据（对象存储）=="
$COMPOSE up -d minio
sleep 6
if [ -d "$SRC/evidence" ] && [ -n "$(ls -A "$SRC/evidence" 2>/dev/null)" ]; then
  docker exec poc-minio sh -c "rm -rf /tmp/imp" >/dev/null 2>&1 || true
  docker cp "$SRC/evidence/." poc-minio:/tmp/imp
  docker exec poc-minio sh -c 'mc alias set ex http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1; mc mb -p ex/evidence >/dev/null 2>&1 || true; mc mirror /tmp/imp ex/evidence'
  docker exec poc-minio rm -rf /tmp/imp
  echo "   已导入"
else
  echo "   跳过：没有 evidence 目录"
fi

echo
echo "== 4. 语音模型缓存 =="
if [ -f "$SRC/models_cache.tar" ]; then
  docker run --rm -v poc_models_cache_placeholder:/tmp -v "$SRC:/in:ro" alpine true 2>/dev/null || true
  mkdir -p ../models_cache && tar -xf "$SRC/models_cache.tar" -C ../models_cache
  echo "   已解包到 ../models_cache"
else
  echo "   跳过：服务器会自动下载（首次转写会慢一些）"
fi

echo
echo "== 5. 启动全部服务 =="
bash deploy/linux/deploy.sh

echo
echo "完成。浏览器打开 http://$(hostname -I | awk '{print $1}'):3000，用原来的管理员账号登录。"
