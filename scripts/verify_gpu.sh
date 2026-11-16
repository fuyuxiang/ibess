#!/bin/bash
# GPU 三件套验证脚本（在 inference-base 容器内运行）
# 用法: docker run --gpus all -v <assets目录>:/assets poc-inference-base:latest bash /scripts/verify_gpu.sh
# 素材: /assets/bus.jpg (YOLO 测试图), /assets/asr_zh.wav (ASR 测试音频)
set -e
echo "========== [1/3] torch GPU 验证 =========="
python -c "
import torch
print('PyTorch 版本 :', torch.__version__)
print('CUDA 可用    :', torch.cuda.is_available())
if torch.cuda.is_available():
    print('GPU 名称     :', torch.cuda.get_device_name(0))
    print('CUDA 版本   :', torch.version.cuda)
    print('显存总量     : %.1f GB' % (torch.cuda.get_device_properties(0).total_memory / 1024**3))
    # 实际张量计算验证
    x = torch.randn(1000, 1000, device='cuda')
    y = (x @ x).sum().item()
    print('GPU 张量计算 : OK (%.2f)' % y)
else:
    print('!!! CUDA 不可用 !!!')
    exit(1)
"

echo "========== [2/3] YOLO 推理验证 =========="
python -c "
from ultralytics import YOLO
import time, os
assert os.path.exists('/assets/bus.jpg'), '缺少测试图 /assets/bus.jpg'
assert os.path.exists('/assets/yolov8n.pt'), '缺少权重 /assets/yolov8n.pt'
model = YOLO('/assets/yolov8n.pt')
t0 = time.time()
results = model.predict('/assets/bus.jpg', conf=0.25, verbose=False)
dt = time.time() - t0
boxes = results[0].boxes
print('模型        : yolov8n.pt')
print('推理耗时    : %.2f s' % dt)
print('检测目标数  : %d' % len(boxes))
for i, b in enumerate(boxes):
    print('  %d: %s  置信度 %.2f' % (i + 1, results[0].names[int(b.cls)], float(b.conf)))
print('YOLO 验证   : OK')
"

echo "========== [3/3] FunASR 转写验证 =========="
python -c "
from funasr import AutoModel
import os, time, torch
assert os.path.exists('/assets/asr_zh.wav'), '缺少测试音频 /assets/asr_zh.wav'
print('首次加载 Paraformer 模型（从 ModelScope 下载，仅首次）...')
t0 = time.time()
model = AutoModel(model='paraformer-zh', vad_model='fsmn-vad', punc_model='ct-punc', disable_update=True)
print('模型加载耗时: %.1f s' % (time.time() - t0))
t0 = time.time()
res = model.generate(input='/assets/asr_zh.wav', batch_size_s=300)
dt = time.time() - t0
text = res[0].get('text', '')
print('转写耗时    : %.1f s' % dt)
print('转写文本    : %s' % text)
assert text and text.strip(), '转写结果为空'
print('FunASR 验证 : OK')
"

echo "========================================="
echo "GPU 三件套验证全部通过 ✅"
