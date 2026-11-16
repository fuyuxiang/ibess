#!/usr/bin/env bash
# 一键启动演示环境：核心栈 → 9路推流 → 分批视觉worker → 健康验证。
# 用法：bash start-demo.sh [视觉worker数量，默认9]；引擎未就绪时自动等待。
set -u
cd "$(dirname "$0")"
BATCH=3
WANT_VW="${1:-9}"

log() { echo "[$(date +%H:%M:%S)] $*"; }

# 0) 引擎等待
log "等待 Docker 引擎..."
engine=""
for i in $(seq 1 30); do
  engine=$(docker version --format '{{.Server.Version}}' 2>/dev/null)
  [ -n "$engine" ] && break
  sleep 5
done
[ -z "$engine" ] && { log "错误：Docker 引擎未就绪（请先启动 Docker Desktop）"; exit 1; }
log "引擎就绪: $engine"

# 1) 核心栈
log "启动核心栈（api/postgres/redis/minio/mediamtx/event-service/semantic-worker/web-console）..."
docker compose up -d >/dev/null 2>&1 || { log "错误：核心栈启动失败"; exit 1; }
for i in $(seq 1 24); do
  unhealthy=$(docker ps --filter "health=unhealthy" --format '{{.Names}}' | grep -c poc || true)
  running=$(docker ps --format '{{.Names}}' | grep -cE '^poc-(api|postgres|redis|minio|mediamtx)$' || true)
  [ "$running" -ge 5 ] && [ "$unhealthy" -eq 0 ] && break
  sleep 5
done
log "核心栈状态: $(docker ps --format '{{.Names}}: {{.Status}}' | grep -E '^poc-(api|postgres|redis|minio|mediamtx)' | tr '\n' ' ')"

# 2) 内存水位检查（配额使用率超过80%则不再加压）
quota=$(docker info --format '{{.MemTotal}}' 2>/dev/null)
used_bytes() {
  docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' 2>/dev/null | awk '{
    gsub(/[a-zA-Z]/, "", $2); split($2, parts, "/");
    v = parts[1] + 0
    # docker stats 形如 "1.15GiB / 15.47GiB"，第一段已剥离字母
    print $1, v * 1073741824
  }' | awk -v q="$quota" '{ s += $2 } END { printf "%.0f", s }'
}
mem_guard() {
  [ -z "$quota" ] && return 0
  used=$(used_bytes)
  pct=$(( used * 100 / quota ))
  log "引擎内存水位: ${pct}%"
  [ "$pct" -lt 80 ]
}

# 3) 推流容器（ffmpeg，轻量）：12 路全部为真实素材
if ! docker ps -a --format '{{.Names}}' | grep -q '^poc-pub-cam1$'; then
  log "首次启动，按真实素材清单创建 12 路推流..."
  bash scripts/publish-cams.sh >/dev/null 2>&1
else
  log "拉起推流容器..."
  docker start $(docker ps -a --format '{{.Names}}' | grep -E '^poc-pub-cam' ) >/dev/null 2>&1
fi
log "推流运行: $(docker ps --format '{{.Names}}' | grep -cE '^poc-pub-cam')路"

# 4) 视觉 worker 分批（每批 $BATCH 个，批间内存守护）
log "分批拉起视觉 worker（每批 $BATCH 个，目标 $WANT_VW 路）..."
started=0
# 五路检测：cam2 入口携带、cam3 评标室通话、cam6 桌面桌拍（带手机扫描）、cam9 离岗、cam10 越界。
# cam5/cam8 已下线：前者只有人数统计，后者遮挡 24 小时没报过。
for cam in cam2 cam3 cam6 cam9 cam10; do
  [ "$started" -ge "$WANT_VW" ] && break
  name="poc-vw-$cam"
  state=$(docker ps -a --format '{{.Names}} {{.Status}}' | grep "^$name " | head -1)
  if [ -z "$state" ]; then
    # 容器被删过（重装/清理）时不能静默跳过，否则会出现"有画面没事件"，按 compose 重建
    log "重建缺失的检测容器 $name"
    if docker compose up -d "vw-cam$cam" >/dev/null 2>&1; then
      started=$((started+1)); sleep 5
    else
      log "重建 $name 失败，跳过"
    fi
    continue
  fi
  echo "$state" | grep -q "Up" && { started=$((started+1)); continue; }
  mem_guard || { log "内存水位过高，停止追加视觉 worker（已启动 $started 路）"; break; }
  docker start "$name" >/dev/null 2>&1 && started=$((started+1))
  if [ $(( started % BATCH )) -eq 0 ]; then
    sleep 15
    log "已启动 $started 路，当前水位检查..."; mem_guard || { log "水位过高，暂停追加"; break; }
  fi
done
log "视觉 worker 运行: $(docker ps --format '{{.Names}}' | grep -cE '^poc-vw-cam')路"

# 5) 健康验证
log "等待流与关键帧就绪（约30秒）..."
sleep 30
ok=1
for cam in cam2 cam3 cam6 cam9 cam10; do
  code=$(curl -sL --max-time 8 -o /dev/null -w '%{http_code}' "http://localhost:3000/hls/$cam/index.m3u8" 2>/dev/null)
  log "HLS $cam: $code"
  [ "$code" != "200" ] && ok=0
done
code=$(curl -s --max-time 15 -o /dev/null -w '%{http_code}' "http://localhost:8000/api/auth/status" 2>/dev/null)
log "API: $code"; [ "$code" != "200" ] && ok=0
events=$(docker exec poc-postgres psql -U demo -d demo -t -A -c "select count(*) from events where created_at > now() - interval '5 minutes'" 2>/dev/null)
log "最近5分钟事件: ${events:-?} 条（冷却生效下的正常量级）"

[ "$ok" -eq 1 ] && log "✅ 演示环境就绪：http://localhost:3000" || { log "⚠️ 部分检查未通过，请查看上方日志"; exit 2; }
