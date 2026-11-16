#!/usr/bin/env bash
# 在本机把全部镜像打包成一个 tar，拷到服务器后 docker load
#   bash deploy/linux/save-images.sh
# 产物：deploy/linux/out/poc-images.tar（约 32 GB）
set -euo pipefail

cd "$(dirname "$0")/../.."
OUT="deploy/linux/out"
mkdir -p "$OUT"

IMAGES=(
  postgres:16-alpine
  redis:7-alpine
  minio/minio:latest
  bluenviron/mediamtx:latest
  poc-api:latest
  poc-event-service:latest
  poc-media-service:latest
  poc-vision-worker:latest
  poc-audio-worker:latest
  poc-semantic-worker:latest
  poc-web-console:latest
)

echo "将打包以下镜像："
for i in "${IMAGES[@]}"; do
  printf "  %-32s %s\n" "$i" "$(docker image inspect "$i" --format '{{.Size}}' 2>/dev/null | awk '{printf "%.1f GB", $1/1e9}')" || echo "  缺失：$i"
done

echo
echo "开始导出（不压缩，压缩对这些层已无收益）..."
docker save "${IMAGES[@]}" -o "$OUT/poc-images.tar"
echo "完成：$(du -h "$OUT/poc-images.tar" | cut -f1)"
echo
echo "拷到服务器后执行：docker load -i /root/poc-transfer/poc-images.tar"
