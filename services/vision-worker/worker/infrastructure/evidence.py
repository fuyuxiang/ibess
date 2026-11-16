"""Application behavior with explicit runtime dependencies; no startup side effects."""

from __future__ import annotations


def init_minio(
    context,
):
    try:
        client = context.Minio(
            context.os.getenv("MINIO_ENDPOINT", "minio:9000"),
            access_key=context.os.getenv("MINIO_ROOT_USER", "demo"),
            secret_key=context.os.getenv("MINIO_ROOT_PASSWORD", ""),
            secure=False,
        )
        bucket = context.os.getenv("MINIO_BUCKET", "evidence")
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
        # POC 阶段公开读，前端直接访问对象 URL（生产需换鉴权）
        client.set_bucket_policy(
            bucket,
            context.json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"AWS": ["*"]},
                            "Action": ["s3:GetObject"],
                            "Resource": [f"arn:aws:s3:::{bucket}/*"],
                        }
                    ],
                }
            ),
        )
        context.log.info(
            "MinIO 就绪: %s/%s (公开读)",
            context.os.getenv("MINIO_ENDPOINT", "minio:9000"),
            bucket,
        )
        return client, bucket
    except Exception as e:
        context.log.warning("MinIO 初始化失败（证据功能禁用）: %s", e)
        return None, None


def save_keyframe(context, minio_client, bucket, frame, source_id):
    if not minio_client:
        return None
    ok, jpg = context.cv2.imencode(".jpg", frame)
    if not ok:
        return None
    object_key = f"keyframes/{source_id}/{int(context.time.time() * 1000)}.jpg"
    try:
        minio_client.put_object(
            bucket, object_key, context.io.BytesIO(jpg.tobytes()), len(jpg.tobytes())
        )
        return object_key
    except Exception as e:
        context.log.warning("关键帧上传失败: %s", e)
        return None
