#!/usr/bin/env bash
# 把音频素材送入 audio-worker 做转写与说话人分离
#
# 用法：
#   bash POC/scripts/enqueue-audio.sh <容器内音频路径> <source_id>
#   例：bash POC/scripts/enqueue-audio.sh /assets/S06_倾向性发言.wav audio-s06
#
# 前置：
#   1) 只接受 WAV（worker 用 wave.open 读文件头取时长），m4a/mp3 需先转：
#      ffmpeg -i in.m4a -ar 16000 -ac 1 out.wav
#   2) 文件要放在 audio-worker 容器能读到的位置，一般是 dev_assets（挂到 /assets）
#   3) 单文件 ≤ 20MB、时长 ≤ 10 分钟
#
# 同一 source_id + 内容指纹只会分析一次；换素材时请换 source_id。
set -euo pipefail

AUDIO_PATH="${1:?请给出容器内音频路径，如 /assets/S06_倾向性发言.wav}"
SOURCE_ID="${2:?请给出 source_id，如 audio-s06}"

export MSYS_NO_PATHCONV=1
docker exec poc-audio-worker python -c "
import os, sys
from pathlib import Path
from sqlalchemy import create_engine
from worker.recordings_worker import tables, enqueue_source
eng = create_engine(os.environ['DATABASE_URL'], pool_pre_ping=True)
rec, seg = tables(eng)
enqueue_source(eng, rec, Path('/audio-data'), '${AUDIO_PATH}', '${SOURCE_ID}')
print('已提交：${AUDIO_PATH} → ${SOURCE_ID}')
"

echo "查看进度：docker exec poc-postgres psql -U demo -d demo -c \"select title,status,segment_count from audio_recordings where source_id='${SOURCE_ID}'\""
echo "结果页面：http://localhost:3000/audio"
