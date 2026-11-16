"""Generate a local consumer digest and exact scopes without echoing service credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from uuid import UUID


def configure(root: Path, scope: dict | list[dict] | None = None) -> Path:
    # Parse only KEY=value data; never source or evaluate .env shell syntax.
    values = {}
    for line in (root / ".env").read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#"):
            values[key] = value.strip().strip("\"'")
    token = values.get("PHASE_CONTROL_TOKEN_EVENT_SERVICE", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
        raise ValueError(
            "请在本机 .env 配置有效的阶段消费者令牌，不要把令牌粘贴到命令行"
        )
    path = root / "config/phase-consumers.local"
    mapping = json.loads(path.read_text()) if path.exists() else {}
    entry = mapping.setdefault("event-service", {"scopes": []})
    if set(mapping) != {"event-service"} or not isinstance(entry.get("scopes"), list):
        raise ValueError("本工具仅管理 event-service 的本机配置，请分别管理其他消费者")
    for scope in [scope] if isinstance(scope, dict) else scope or []:
        if set(scope) != {"project_id", "room_id", "session_id", "source_id"}:
            raise ValueError("作用域必须完整包含项目、评标室、场次和来源")
        for key in ("project_id", "room_id", "session_id"):
            UUID(scope[key])
        if not isinstance(scope["source_id"], str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", scope["source_id"]
        ):
            raise ValueError("来源标识格式无效")
        if scope not in entry["scopes"]:
            entry["scopes"].append(scope)
    entry["token_sha256"] = hashlib.sha256(token.encode("ascii")).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".phase-consumers-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(mapping, ensure_ascii=False, indent=2) + "\n")
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return path


def scopes_for_session(root: Path, session_id: str) -> list[dict]:
    """Read deployment scopes inside the API container; never call a public admin endpoint."""
    session_id = str(UUID(session_id))
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "api",
            "python",
            "-m",
            "app.maintenance_scope",
            session_id,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    scopes = json.loads(result.stdout)
    if (
        not isinstance(scopes, list)
        or not scopes
        or any(
            not isinstance(scope, dict) or scope.get("session_id") != session_id
            for scope in scopes
        )
    ):
        raise ValueError("未返回本场准确监测范围")
    return scopes


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--scope-file", type=Path, help="兼容已有准确作用域 JSON 文件")
    choice.add_argument(
        "--session-id", help="从运行中的 API 容器读取本场所有启用通道范围"
    )
    args = parser.parse_args()
    try:
        scopes = (
            scopes_for_session(args.root, args.session_id)
            if args.session_id
            else (json.loads(args.scope_file.read_text()) if args.scope_file else None)
        )
        configure(args.root, scopes)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        parser.exit(
            1, "生成失败：请检查 API 容器、本机场次、.env 和消费者配置；未输出凭据。\n"
        )
    print("已更新 config/phase-consumers.local；请重新创建 API 容器以加载配置。")
