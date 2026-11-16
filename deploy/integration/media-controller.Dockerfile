# 阶段媒体控制器（执行卡 4.2 / 10.2）。构建上下文是 scripts/，只拷两个文件；依赖只装 4.2 交接确认的 ffmpeg + redis + sqlalchemy + psycopg。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir redis sqlalchemy "psycopg[binary]"

WORKDIR /controller
COPY stage_media_controller.py media_run_registry.py ./

CMD ["python", "stage_media_controller.py"]
