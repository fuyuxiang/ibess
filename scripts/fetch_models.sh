#!/bin/bash
# 模型权重下载脚本（离线化准备，内网部署复用）
# 把权重固化到 models_cache 目录，运行时挂载，避免内网无外网无法下载
#
# 用法（在 inference-base 容器内执行）：
#   docker run --rm --gpus all \
#     -v "$PWD/../../models_cache:/root/.cache" \
#     -v "$PWD:/scripts" \
#     poc-inference-base:latest bash /scripts/fetch_models.sh
set -e

echo "========== 下载 FunASR 模型（ModelScope） =========="
python - <<'PY'
from funasr import AutoModel

# paraformer-zh + fsmn-vad + ct-punc，下载到 ~/.cache/modelscope
model = AutoModel(
    model='paraformer-zh',
    vad_model='fsmn-vad',
    punc_model='ct-punc',
    disable_update=True,
)
print('FunASR 模型就绪: paraformer-zh + fsmn-vad + ct-punc')
PY

echo "========== 校验 YOLO 权重 =========="
YOLO_PT="${YOLO_WEIGHTS:-/assets/yolov8n.pt}"
if [ -f "$YOLO_PT" ]; then
    echo "yolov8n.pt 就绪: $YOLO_PT ($(du -h "$YOLO_PT" | cut -f1))"
else
    echo "提示: yolov8n.pt 未找到，请放置到 $YOLO_PT"
fi

echo "========== 权重固化清单 =========="
du -sh /root/.cache/modelscope 2>/dev/null || echo "modelscope 缓存为空"
echo "完成 ✅ 权重已固化，可随 models_cache 目录分发到内网。"
