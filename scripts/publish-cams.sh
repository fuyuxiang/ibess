#!/usr/bin/env bash
# 演示推流：一段真实素材对一路，一一对应，不复用、不裁切、不做第二机位。
# 素材来源：通过 POC_VIDEO_DIR 指定仓库外的授权素材目录。
# 真实素材只有 10 段视频，因此就是 10 路；AI 生成片段（H3-Max / MiniMax）一律不用。
# cam7（S09 争吵）额外保留音轨，供 audio-worker 现场拾音做转写与敏感词命中。
# 用法：bash scripts/publish-cams.sh
set -u
cd "$(dirname "$0")/.."

# 本机目录由 .env 或环境变量配置；默认位置已被 Git 忽略。
# Windows 上 Git Bash 的 pwd 返回 /d/... 形式，docker -v 认不出来，需要转成 D:/...；Linux 无需转换
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
command -v cygpath >/dev/null 2>&1 && REPO_ROOT="$(cygpath -w "$REPO_ROOT")"
source "$REPO_ROOT/scripts/load-env.sh"
load_poc_env "$REPO_ROOT/.env"
HOST_VIDEO_DIR="${POC_VIDEO_DIR:-$REPO_ROOT/private_assets/videos}"
[ -d "$HOST_VIDEO_DIR" ] || { echo "授权素材目录不存在；请在 .env 配置 POC_VIDEO_DIR"; exit 1; }
IMG="${POC_INFER_IMAGE:-poc-inference-base:latest}"
NET=poc_default
RTSP=rtsp://mediamtx:8554
FPS=15
R="/videos/真实素材/视频"

publish() { # $1=cam  $2=素材文件  $3=1 时保留音轨  $4=输出宽度（默认 960）
  local cam="$1" file="$2" audio="${3:-0}" width="${4:-960}" acodec="-an"
  # 1280 宽不加码率上限会撑爆读端解码（实测 h264 报 Invalid level prefix / 宏块解码错），1500k 足够
  local BITRATE="900k"
  [ "$width" -gt 960 ] && BITRATE="1500k"
  [ "$audio" = "1" ] && acodec="-c:a aac -b:a 64k -ar 16000 -ac 1"
  docker rm -f "poc-pub-$cam" >/dev/null 2>&1 || true
  docker run -d --name "poc-pub-$cam" --network "$NET" --restart unless-stopped \
    -v "$HOST_VIDEO_DIR:/videos:ro" --entrypoint sh "$IMG" -c \
    "exec ffmpeg -hide_banner -loglevel error -re -stream_loop -1 -i \"$file\" $acodec \
     -vf \"setpts=N/(${FPS}*TB),scale=${width}:-2\" -r ${FPS} -g 30 -keyint_min 30 -sc_threshold 0 \
     -c:v libx264 -preset ultrafast -tune zerolatency -b:v ${BITRATE} -maxrate ${BITRATE} -bufsize $(( ${BITRATE%k} * 2 ))k -f rtsp -rtsp_transport tcp ${RTSP}/${cam}" \
    >/dev/null
  printf "  %-6s <- %s %s\n" "$cam" "${file#$R/}" "$([ "$audio" = "1" ] && echo '[带音轨]')"
}

echo "[$(date +%H:%M:%S)] 重建 10 路真实素材推流（一段素材一路）..."

publish cam1  "$R/S01 携带通讯设备入场-负样本-1.mp4"     # 入口 · 正常核验放行
# 入口手机在整段 21 秒里只有第 14 秒清晰（原片 2560 也只有 conf 0.12），960 推流检不出；
# 这一路同样推 1280，并把投票放宽到单次命中。
publish cam2  "$R/S01 携带通讯设备入场-正样本-1.mp4" 0 1280  # 入口 · 携带通讯设备入场（720p）
publish cam3  "$R/S02 疑似使用手机对外联络-负样本-1.mp4" # 评标室 · 疑似通话（一）
publish cam4  "$R/S02 疑似使用手机对外联络-负样本-2.mp4" # 评审期间 · 疑似通话（二）
publish cam5  "$R/S04 身份核验与换人-负样本-1.mp4"       # 评标室全景 · 身份核验与换人
# 桌拍动作在原片里只出现在第 60~115 秒，整段 171 秒循环意味着事件最快 3 分钟一次；
# 这一段是同一段真实素材截出来的高光循环（_loop/），只为提高演示时的命中密度。
# 桌拍手机在 960 画面里只有 18 像素，实测检不出；这一路单独推 1280 宽，手机像素翻倍后可检出。
publish cam6  "/videos/_loop/S08_cam6_highlight.mp4" 0 1280  # 桌面 · 偷拍与带离资料（高光循环 · 720p）
publish cam7  "$R/S09 争吵威胁与推搡-负样本-1.mp4" 1     # 现场 · 争吵威胁与推搡（带音轨）
publish cam8  "$R/S10 遮挡与断流-负样本-1.mp4"           # 摄像头遮挡 · 断流
publish cam9  "$R/S03 擅自离岗-负样本-1.mp4"             # 工位 · 擅自离岗（座位 ROI）
publish cam10 "$R/S05 非授权接触与串岗-负样本-1.mp4"     # 区域 · 非授权接触与串岗（区域 ROI）

# 早期 12 路方案里的第 11、12 路已下线，容器一并清理
docker rm -f poc-pub-cam11 poc-pub-cam12 >/dev/null 2>&1 || true

sleep 6
echo "[$(date +%H:%M:%S)] 推流状态："
for cam in cam1 cam2 cam3 cam4 cam5 cam6 cam7 cam8 cam9 cam10; do
  state=$(docker ps --filter "name=^poc-pub-${cam}$" --format '{{.Status}}')
  printf "  %-6s %s\n" "$cam" "${state:-未运行}"
done
