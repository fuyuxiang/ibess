# cu128 GPU 推理基线镜像
# 覆盖两代架构：RTX 4090（Ampere sm_86）与 RTX 5060（Blackwell sm_120）
# 本地 5060 开发 / 测试环境 4090 通用，一份镜像两端一致
#
# 依赖 torch 2.8.0-cuda12.8 镜像，必须先拉取：
#   docker pull pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime

FROM pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_ENDPOINT=https://hf-mirror.com

# 系统依赖：OpenCV 运行库、FFmpeg、音频解码
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libsndfile1 \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Dependency graph is locked and shares the application's compatibility constraints.
COPY deploy/inference-base/requirements.lock /tmp/inference.lock
RUN pip install --index-url "$PIP_INDEX_URL" -r /tmp/inference.lock

# 预下载模型权重，构建时离线化（供内网单机复用）
# 方式一（推荐）：运行时挂载 POC/models_cache 到 /root/.cache，
#   权重用 POC/scripts/fetch_models.sh 一次性下载固化
# 方式二（自包含，镜像更大）：构建时 COPY 权重进镜像
#   COPY models/modelscope /root/.cache/modelscope
#   COPY models/yolov8n.pt /weights/yolov8n.pt
