# Build from the repository root with -f services/media-service/Dockerfile
FROM python:3.12-slim

# 国内网络构建更快；官方源可用 --build-arg PIP_INDEX_URL=https://pypi.org/simple 覆盖
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/media-service/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock
COPY packages/ibess-common /opt/ibess-common
RUN pip install --no-deps /opt/ibess-common

COPY services/media-service/service ./service

CMD ["python", "-m", "service.main"]
