#!/usr/bin/env bash
# Linux统一入口。默认复用现有镜像，启动平台和必要依赖。
set -euo pipefail
task_poc_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$task_poc_root"
build=false
restart=false
workers=false
video=false
for option in "$@"; do
  case "$option" in
    --build) build=true ;;
    --restart) restart=true ;;
    --workers) workers=true ;;
    --video-worker) video=true ;;
    *) echo "未知参数：$option" >&2; exit 2 ;;
  esac
done
wait_seconds="${POC_WAIT_TIMEOUT:-180}"
docker info >/dev/null
wait_ready() {
  local url="$1"
  local deadline=$((SECONDS + wait_seconds))
  until curl --fail --silent --show-error --max-time 12 "$url" >/dev/null 2>&1; do
    if ((SECONDS >= deadline)); then echo "服务等待超时：$url" >&2; return 1; fi
    sleep 2
  done
}
if "$build"; then
  docker compose build web-console
  targets=(api)
  if "$workers"; then targets+=(event-service media-service audio-worker); fi
  if "$video"; then targets+=(vision-worker); fi
  docker compose build "${targets[@]}"
fi
docker compose up -d --no-build --wait --wait-timeout "$wait_seconds" postgres redis minio mediamtx
docker compose up -d --no-build api
if "$restart"; then docker compose restart api; fi
docker compose up -d --no-build --wait --wait-timeout "$wait_seconds" api
docker compose up -d --no-build web-console
if "$restart"; then docker compose restart web-console; fi
docker compose up -d --no-build --wait --wait-timeout "$wait_seconds" web-console
targets=()
if "$workers"; then targets+=(event-service media-service audio-worker); fi
if "$video"; then targets+=(vision-worker); fi
if ((${#targets[@]})); then docker compose up -d --no-build "${targets[@]}"; fi
wait_ready http://localhost:3000/health/ready
wait_ready http://localhost:3000/api/auth/status
docker compose ps api web-console postgres redis minio
echo '平台就绪：http://localhost:3000/stages'
