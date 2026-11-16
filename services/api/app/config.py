import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://demo@localhost:5432/demo"
    redis_url: str = "redis://localhost:6379/0"
    minio_endpoint: str = "localhost:9000"
    # 浏览器可访问的 MinIO 地址：部署到服务器时用 MINIO_PUBLIC_ENDPOINT 覆盖成 http://<服务器IP或域名>:9000
    minio_public_endpoint: str = os.getenv("MINIO_PUBLIC_ENDPOINT", "localhost:9000")
    minio_root_user: str = "demo"
    minio_root_password: str = ""
    minio_bucket: str = "evidence"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
