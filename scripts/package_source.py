"""打包可分发的源码，排除凭据、私有素材、运行数据及构建产物。"""
from __future__ import annotations

import argparse
import fnmatch
import os
from pathlib import Path
import tarfile

EXCLUDED_DIRS = frozenset({
    ".git", ".agents", ".codex", ".aws", ".claude", ".idea", ".vscode",
    "node_modules", "dist", ".vite", ".venv", "venv", "__pycache__",
    ".pytest_cache", "models_cache", "secrets", "credentials", "private_assets",
    "backups", "poc_backups", "supervision-evidence", "recordings",
    "media_runs", "audio-data", "coverage", "htmlcov", "playwright-report",
    "test-results", "output", "tmp", "build", ".ruff_cache",
})
EXCLUDED_SUFFIXES = (
    ".local", ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore",
    ".db", ".sqlite", ".sqlite3", ".dump", ".log", ".bak", ".tsbuildinfo",
    ".tar", ".tar.gz", ".tgz", ".zip", ".7z", ".pt", ".pth", ".onnx",
    ".mp4", ".wav", ".m4a", ".mp3", ".aac", ".flac", ".ogg", ".webm",
    ".mkv", ".mov", ".avi",
)
PRIVATE_KEY_NAMES = frozenset({"id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"})


def included(relative: Path) -> bool:
    parts = relative.parts
    name = relative.name
    lower = name.lower()
    # Only the root evidence directory contains runtime files; nested evidence
    # modules (such as the web playback component) are application source.
    if parts[:1] == ("evidence",):
        return False
    if any(part in EXCLUDED_DIRS or part.endswith(".egg-info") for part in parts):
        return False
    if parts[:3] == ("deploy", "linux", "out") or parts[:2] == ("docs", "generated"):
        return False
    if lower in PRIVATE_KEY_NAMES or lower == ".ds_store":
        return False
    is_env = lower == ".env" or lower.startswith(".env.") or lower.endswith(".env")
    if is_env and not lower.endswith(".example"):
        return False
    if lower.endswith(EXCLUDED_SUFFIXES):
        return False
    if parts[:2] == ("web-console", "tests") and (
        any(fnmatch.fnmatchcase(name, p) for p in (
            "*-result.json", "*-preparation.json", "business-export-*.json"))
        or name in {"other-pages-audit.json", "auth-test-schema.txt"}
    ):
        return False
    if parts[:2] == ("tests", "integration") and name.endswith("-result.json"):
        return False
    if relative.as_posix() in {
        "docs/智能评标监测系统使用说明书.docx", "docs/智能评标监测系统使用说明书.html",
        "web-console/public/meeting_room.jpg",
    }:
        return False
    if parts[:3] == ("web-console", "public", "stage") and lower.endswith((".jpg", ".jpeg", ".png")):
        return False
    return True


def package(root: Path, output: Path) -> int:
    root, output = root.resolve(), output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with tarfile.open(output, "w:gz", format=tarfile.PAX_FORMAT, dereference=False) as archive:
        for directory, subdirs, filenames in os.walk(root, followlinks=False):
            parent = Path(directory)
            subdirs[:] = sorted(d for d in subdirs if included((parent / d).relative_to(root))
                                and not (parent / d).is_symlink())
            for name in sorted(filenames):
                path = parent / name
                relative = path.relative_to(root)
                # 不跟随符号链接，以免把仓库外的私有文件带入代码包。
                if path.is_symlink() or not path.is_file() or path.resolve() == output or not included(relative):
                    continue
                def normalize(info):
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = 0
                    info.pax_headers = {}
                    return info
                archive.add(path, arcname="./" + relative.as_posix(), recursive=False, filter=normalize)
                count += 1
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(f"已打包 {package(args.root, args.output)} 个源码文件；凭据和运行数据已排除。")
