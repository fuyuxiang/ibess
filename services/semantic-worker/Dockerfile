# Build from the repository root with -f services/semantic-worker/Dockerfile
FROM python:3.12-slim

# 国内网络构建更快；官方源可用 --build-arg PIP_INDEX_URL=https://pypi.org/simple 覆盖
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /worker

COPY services/semantic-worker/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock
COPY packages/ibess-common /opt/ibess-common
RUN pip install --no-deps /opt/ibess-common

COPY services/semantic-worker/worker ./worker
COPY services/semantic-worker/prompts ./prompts

CMD ["python", "-m", "worker.runner"]
