#!/usr/bin/env bash
# 兼容原全量入口，核心服务启动统一复用start_poc.sh。
set -euo pipefail
task_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$task_script_dir/.."
export MSYS_NO_PATHCONV=1
bash scripts/start_poc.sh --build --workers
video_available=false
if [[ -n "${RTSP_URL:-}" ]]; then
  video_available=true
else
  video='../dev_assets/meeting_room.mp4'
  [[ -f "$video" ]] || video='../dev_assets/test_people.mp4'
  if [[ -f "$video" ]]; then
    if docker container inspect poc-publisher >/dev/null 2>&1; then docker rm -f poc-publisher; fi
    docker run -d --name poc-publisher --network poc_default --restart unless-stopped \
      -v "$task_script_dir/../../dev_assets:/assets:ro" poc-inference-base:latest \
      ffmpeg -re -stream_loop -1 -i "/assets/$(basename "$video")" \
      -c copy -f rtsp rtsp://mediamtx:8554/cam1
    video_available=true
  fi
fi
if "$video_available"; then
  docker compose build vision-worker
  docker compose up -d --no-build vision-worker
  echo '视频检测服务已启动，实际采集状态需检查输入流。'
else
  echo '未配置有效视频输入，暂不额外启动视频检测。平台仍可使用。'
fi
