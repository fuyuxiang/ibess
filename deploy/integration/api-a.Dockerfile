# api 派生镜像（执行卡 10.2 / 4.1 交接）：在同事的 api 镜像上只加 ffmpeg（带 ffprobe），供素材登记探测时长与流信息。
# 不改 services/api/Dockerfile（D-27）。先用同事的 compose 构建 poc-api，再构建本镜像。
ARG BASE_IMAGE=poc-api
FROM ${BASE_IMAGE}

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
