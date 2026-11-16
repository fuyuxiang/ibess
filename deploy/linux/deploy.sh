#!/usr/bin/env bash
# 启动整套系统（Linux 服务器）
#   bash deploy/linux/deploy.sh            # 只用已有镜像启动
#   bash deploy/linux/deploy.sh --build    # 先在服务器上构建镜像
#   bash deploy/linux/deploy.sh --with-demo-cams   # 连演示摄像头一起起（本机演示用）
set -euo pipefail

cd "$(dirname "$0")/../.."

BUILD=0; DEMO_CAMS=0
for arg in "$@"; do
  case "$arg" in
    --build) BUILD=1 ;;
    --with-demo-cams) DEMO_CAMS=1 ;;
    *) echo "未知参数：$arg"; exit 1 ;;
  esac
done

COMPOSE="docker compose -f docker-compose.yml -f deploy/linux/docker-compose.linux.yml"

if [ ! -f .env ]; then
  echo "缺少 .env，先执行：cp deploy/linux/.env.example .env"
  exit 1
fi

echo "== 校验配置 =="
$COMPOSE config --quiet
echo "   OK"

if [ "$BUILD" = "1" ]; then
  echo "== 构建镜像（首次约 20-40 分钟，取决于网络）=="
  $COMPOSE build
fi

echo "== 启动 =="
if [ "$DEMO_CAMS" = "1" ]; then
  $COMPOSE --profile demo-cams up -d
else
  $COMPOSE up -d
fi

echo "== 等待服务就绪 =="
for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://localhost:8000/health/ready || true)
  if [ "$code" = "200" ]; then
    echo "   api 就绪（${i}0 秒内）"
    break
  fi
  sleep 10
  if [ "$i" = "60" ]; then
    echo "   api 仍未就绪，查看日志：docker logs poc-api --tail 50"
    exit 1
  fi
done

echo
echo "== 状态 =="
$COMPOSE ps

echo
IP=$(hostname -I | awk '{print $1}')
echo "前端地址： http://${IP}:3000"
echo "接口文档： http://${IP}:8000/docs"
echo "默认管理员：见 .env 里的 ADMIN_USER / ADMIN_PASSWORD"
