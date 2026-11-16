# Build from repository root.
# 依赖 GPU 推理基线镜像，先构建：
#   docker build -t poc-inference-base:latest deploy/inference-base
FROM poc-inference-base:latest

COPY requirements/common.lock /tmp/common.lock
RUN pip install --no-cache-dir -r /tmp/common.lock
COPY packages/ibess-common /opt/ibess-common
RUN pip install --no-deps /opt/ibess-common

WORKDIR /worker

COPY services/audio-worker/worker ./worker

CMD ["python", "-m", "worker.recordings_worker"]
