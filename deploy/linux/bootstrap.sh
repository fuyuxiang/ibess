#!/usr/bin/env bash
# 智能评标监测系统 —— Linux 服务器部署前置检查
# 在项目根目录执行：bash deploy/linux/bootstrap.sh
set -euo pipefail

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'; NC='\033[0m'
ok()   { echo -e "${GRN}✔${NC} $1"; }
warn() { echo -e "${YLW}!${NC} $1"; }
die()  { echo -e "${RED}x${NC} $1"; exit 1; }

echo "== 1. 运行环境 =="
if ! command -v docker >/dev/null 2>&1; then die "未安装 docker"; fi
ok "docker $(docker --version | awk '{print $3}' | tr -d ,)"
if docker compose version >/dev/null 2>&1; then
  ok "compose $(docker compose version --short)"
else
  die "未安装 docker compose 插件（v2）"
fi
if ! docker info >/dev/null 2>&1; then die "docker 守护进程未运行，或无权限（试试 sudo / 加入 docker 组）"; fi
ok "docker 守护进程正常"

echo
echo "== 2. 端口占用 =="
for p in 3000 8000 5432 6379 9000 8554 8888; do
  if ss -ltn 2>/dev/null | grep -q ":$p "; then
    warn "端口 $p 已被占用，启动会失败；先停掉占用的进程"
  else
    ok "端口 $p 空闲"
  fi
done

echo
echo "== 3. 磁盘空间 =="
avail=$(df -BG --output=avail /var/lib/docker 2>/dev/null | tail -1 | tr -dc '0-9')
if [ -z "$avail" ]; then avail=$(df -BG --output=avail / | tail -1 | tr -dc '0-9'); fi
echo "   可用 ${avail}G（镜像约 33G + 数据约 12G，建议 ≥ 60G）"
if [ "$avail" -lt 50 ]; then warn "空间偏紧，建议先清理"; else ok "空间充足"; fi

echo
echo "== 4. 宿主机目录 =="
for d in private_assets/videos config models models/face ../dev_assets ../models_cache; do
  if [ -e "$d" ]; then ok "$d"; else warn "$d 不存在（音频/人脸相关功能会不可用）"; fi
done

echo
echo "== 5. compose 配置 =="
if docker compose -f docker-compose.yml -f deploy/linux/docker-compose.linux.yml config --quiet 2>/dev/null; then
  ok "覆盖配置校验通过"
else
  die "compose 配置有误，先修好再部署"
fi

echo
echo "== 6. 内存 =="
mem=$(awk '/MemTotal/{printf "%d", $2/1024/1024}' /proc/meminfo)
echo "   总内存 ${mem}G"
if [ "$mem" -lt 8 ]; then warn "低于 8G，检测与语音服务可能被 OOM kill"; else ok "内存够用"; fi

echo
ok "检查完成。下一步："
echo "   1) cp deploy/linux/.env.example .env 并改掉密码与模型密钥"
echo "   2) bash deploy/linux/deploy.sh"
