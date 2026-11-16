"""从公开模板创建仅供本机使用的 .env，不覆盖已有配置，也不输出凭据。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets

from configure_phase_consumer import configure


def initialize(root: Path) -> Path:
    template = (root / ".env.example").read_text(encoding="utf-8")
    values = {
        name: secrets.token_urlsafe(32)
        for name in (
            "POSTGRES_PASSWORD",
            "MINIO_ROOT_PASSWORD",
            "PHASE_CONTROL_TOKEN_EVENT_SERVICE",
        )
    }
    lines = []
    for line in template.splitlines():
        key = line.partition("=")[0]
        lines.append(f"{key}={values[key]}" if key in values else line)
    # 本机直接运行 API 时使用；容器内由 Compose 覆盖成服务名地址。
    lines.append(
        "DATABASE_URL=postgresql+psycopg://demo:"
        + values["POSTGRES_PASSWORD"]
        + "@localhost:5432/demo"
    )
    target = root / ".env"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    configure(root)
    with target.open("a") as stream:
        stream.write("PHASE_CONTROL_CONSUMERS_PATH=./config/phase-consumers.local\n")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args()
    try:
        initialize(args.root)
    except FileExistsError:
        parser.exit(1, ".env 已存在，保留原配置；请按 .env.example 手动补充配置项。\n")
    print(
        "已创建权限为 600 的本机 .env；基础设施口令已随机生成，账号密码通过页面设置。"
    )
