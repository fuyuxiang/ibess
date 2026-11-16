"""关键帧清理：删除 MinIO 中超过 N 小时的关键帧对象（默认24小时）。

用法（api 容器内执行，库已内置 minio SDK）：
  docker cp scripts/clean-keyframes.py poc-api:/tmp/clean.py
  docker exec poc-api python /tmp/clean.py
演示前建议执行一次，保持证据桶体积可控（2秒截图间隔下约 450KB/s×路数）。
"""
import os
import time

from minio import Minio

hours = int(os.getenv("KEYFRAME_KEEP_HOURS", "24"))
client = Minio(os.getenv("MINIO_ENDPOINT", "minio:9000"),
               access_key=os.getenv("MINIO_ROOT_USER", "demo"),
               secret_key=os.getenv("MINIO_ROOT_PASSWORD", ""),
               secure=False)
cutoff = time.time() * 1000 - hours * 3600 * 1000
deleted = kept = 0
for obj in client.list_objects(os.getenv("MINIO_BUCKET", "evidence"), prefix="keyframes/", recursive=True):
    name = obj.object_name.rsplit("/", 1)[-1].split(".")[0]
    try:
        timestamp = int(name)
    except ValueError:
        kept += 1
        continue
    if timestamp < cutoff:
        client.remove_object(os.getenv("MINIO_BUCKET", "evidence"), obj.object_name)
        deleted += 1
    else:
        kept += 1
print(f"删除 {deleted} 张，保留 {kept} 张（{hours}小时内）")
