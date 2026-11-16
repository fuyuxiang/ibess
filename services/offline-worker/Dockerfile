# Build from repository root; reuse the same live-rule sources.
FROM poc-inference-base:latest

# 国内网络构建更快；官方源可用 --build-arg PIP_INDEX_URL=https://pypi.org/simple 覆盖
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OFFLINE_VISION_WORKER_DIR=/rules/vision-worker \
    OFFLINE_EVENT_SERVICE_DIR=/rules/event-service

WORKDIR /worker

COPY services/offline-worker/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock
COPY packages/ibess-common /opt/ibess-common
RUN pip install --no-deps /opt/ibess-common

# 与实时同一份规则代码（只读使用）；离线的版本清单按这些文件的摘要冻结
COPY services/vision-worker/worker /rules/vision-worker/worker
COPY services/event-service/service /rules/event-service/service
COPY services/offline-worker/offline ./offline

CMD ["python", "-m", "offline.runner"]
