#!/usr/bin/env bash
# 5.01：让 B 的 9 路视觉 worker（poc-vw-cam1～9）用上新代码。
# 这些容器是部署机上预先建好的（start-demo.sh 只 docker start），创建命令不在仓库里；容器创建时就绑定了当时的镜像，
# 重新构建镜像后 docker start 老容器跑的仍是旧代码。本脚本逐个读取现有容器的镜像、启动命令、环境变量、挂载、网络、GPU、
# shm 与重启策略，只把镜像换成新构建的那一个，其他原样照抄后重建。
# 安全做法：默认只打印；--apply 时先把旧容器改名为 <名>-old，新容器建好（原来在跑的还要起得来）才删旧的，失败就删新的、把旧的改回原名。
# 不删数据卷、不碰其他容器。参数用数组传给 docker，不经 eval，值里的 $ 与反引号原样保留。
# 用法（项目根，Git Bash）：
#   bash deploy/integration/recreate_vision_workers.sh                 # 只打印
#   bash deploy/integration/recreate_vision_workers.sh --build --apply # 先 docker compose build vision-worker，再重建
#   bash deploy/integration/recreate_vision_workers.sh --image poc-vision-worker:latest --apply
#   bash deploy/integration/recreate_vision_workers.sh --build --set A_VISUAL_REVIEW_ENABLED=0 --apply   # 顺带改环境变量（可重复）
set -u
# Git Bash 会把传给 docker.exe 的类 Unix 路径（PATH=/opt/...、-v /src:/dst）改写成 Windows 路径，必须关掉
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
cd "$(dirname "$0")/../.."

APPLY=0; BUILD=0; IMAGE=""; PATTERN='^poc-vw-cam[0-9]+$'
SETS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --build) BUILD=1 ;;
    --image) IMAGE="$2"; shift ;;
    --pattern) PATTERN="$2"; shift ;;
    --set) case "$2" in *=*) SETS+=("$2") ;; *) echo "--set 需要 KEY=VAL"; exit 2 ;; esac; shift ;;
    *) echo "未知参数：$1"; exit 2 ;;
  esac
  shift
done

log() { echo "[$(date +%H:%M:%S)] $*"; }
command -v docker >/dev/null 2>&1 || { echo "错误：找不到 docker 命令"; exit 1; }
lines() { docker inspect --format "$1" "$2" | tr -d '\r' | sed '/^$/d'; }

mapfile -t names < <(docker ps -a --format '{{.Names}}' | tr -d '\r' | grep -E "$PATTERN" | sort)
[ "${#names[@]}" -eq 0 ] && { log "没有匹配 $PATTERN 的容器，什么也不做"; exit 0; }

if [ "$BUILD" -eq 1 ]; then
  log "构建 vision-worker 镜像（B 的 compose，含 A 扩展与提示词）..."
  docker compose build vision-worker || { log "错误：构建失败，未改动任何容器"; exit 1; }
  # 构建出来的镜像名以 compose 为准；容器原来用的若是别的名字，不换就还是旧代码
  [ -z "$IMAGE" ] && IMAGE=$(docker compose config --images 2>/dev/null | tr -d '\r' | grep -i 'vision' | head -1)
  [ -z "$IMAGE" ] && { log "错误：取不到 vision-worker 镜像名，请用 --image 指定"; exit 1; }
  log "新镜像：$IMAGE"
fi
if [ -n "$IMAGE" ] && ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  log "错误：镜像 $IMAGE 不存在，未改动任何容器"; exit 1
fi

failed=0
for name in "${names[@]}"; do
  old_image=$(lines '{{.Config.Image}}' "$name")
  # 源码以挂载方式进容器的，不用换镜像：重启即生效
  if lines '{{range .Mounts}}{{println .Destination}}{{end}}' "$name" | grep -qx '/worker/worker'; then
    log "$name 挂载了源码目录，跳过重建；执行 docker restart $name 即可"
    continue
  fi
  image="${IMAGE:-$old_image}"
  args=(--name "$name")
  mapfile -t networks < <(lines '{{range $k, $v := .NetworkSettings.Networks}}{{println $k}}{{end}}' "$name")
  args+=(--network "$(lines '{{.HostConfig.NetworkMode}}' "$name")")
  [ "${#networks[@]}" -gt 1 ] && log "警告：$name 连了 ${#networks[@]} 个网络（${networks[*]}），只照抄主网络，其余需手动 docker network connect"
  restart=$(lines '{{.HostConfig.RestartPolicy.Name}}' "$name")
  [ -n "$restart" ] && [ "$restart" != "no" ] && args+=(--restart "$restart")
  runtime=$(lines '{{.HostConfig.Runtime}}' "$name")
  [ -n "$runtime" ] && [ "$runtime" != "runc" ] && args+=(--runtime "$runtime")
  gpu=$(lines '{{range .HostConfig.DeviceRequests}}{{if .DeviceIDs}}device={{join .DeviceIDs ","}}{{else if .Capabilities}}all{{end}}{{end}}' "$name")
  [ -n "$gpu" ] && args+=(--gpus "$gpu")
  shm=$(lines '{{.HostConfig.ShmSize}}' "$name")
  [ -n "$shm" ] && [ "$shm" != "67108864" ] && [ "$shm" != "0" ] && args+=(--shm-size "$shm")
  while IFS= read -r env; do args+=(-e "$env"); done < <(lines '{{range .Config.Env}}{{println .}}{{end}}' "$name")
  for kv in "${SETS[@]}"; do args+=(-e "$kv"); done        # 同名 -e 以后出现的为准，--set 覆盖原值
  skip=0
  while IFS=$'\t' read -r type src dst rw; do
    case "$type" in
      bind)   spec="$src:$dst" ;;
      volume) spec="$src:$dst" ;;
      *) log "警告：$name 有 $type 类型挂载（$dst），脚本不会照抄，跳过此容器请手动处理"; skip=1; break ;;
    esac
    [ "$rw" = "false" ] && spec="$spec:ro"
    args+=(-v "$spec")
  done < <(lines '{{range .Mounts}}{{.Type}}{{"\t"}}{{if eq .Type "volume"}}{{.Name}}{{else}}{{.Source}}{{end}}{{"\t"}}{{.Destination}}{{"\t"}}{{.RW}}{{println}}{{end}}' "$name")
  [ "$skip" -eq 1 ] && { failed=1; continue; }
  mapfile -t cmd < <(lines '{{range .Config.Cmd}}{{println .}}{{end}}' "$name")
  running=$(lines '{{.State.Running}}' "$name")

  log "$name：镜像 $old_image → $image（原来$([ "$running" = "true" ] && echo 在运行 || echo 已停止)）"
  printf '  docker %s' "$([ "$running" = "true" ] && echo "run -d" || echo create)"; printf ' %q' "${args[@]}" "$image" "${cmd[@]}"; echo
  [ "$APPLY" -eq 1 ] || continue

  docker rm -f "$name-old" >/dev/null 2>&1      # 上次失败遗留的备份
  docker rename "$name" "$name-old" || { log "错误：$name 改名失败，未改动"; failed=1; continue; }
  [ "$running" = "true" ] && docker stop "$name-old" >/dev/null
  if [ "$running" = "true" ]; then
    docker run -d "${args[@]}" "$image" "${cmd[@]}" >/dev/null && sleep 5 && [ "$(lines '{{.State.Running}}' "$name")" = "true" ]
  else
    docker create "${args[@]}" "$image" "${cmd[@]}" >/dev/null
  fi
  if [ $? -eq 0 ]; then
    docker rm "$name-old" >/dev/null
    log "$name 已重建"
  else
    log "错误：$name 重建失败，恢复旧容器（新容器日志如下）"
    docker logs --tail 20 "$name" 2>&1 | sed 's/^/    /'
    docker rm -f "$name" >/dev/null 2>&1
    docker rename "$name-old" "$name"
    [ "$running" = "true" ] && docker start "$name" >/dev/null
    failed=1
  fi
done

[ "$APPLY" -eq 1 ] || log "以上只是打印；确认无误后加 --apply 执行（可加 --build 先构建镜像）"
exit "$failed"
