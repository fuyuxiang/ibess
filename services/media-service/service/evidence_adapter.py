"""执行卡 9.2：契约 EvidenceRef 构造与证据登记器。

EvidenceRef 只带作用域、媒体引用、时间、摘要与状态——不带路径、对象键、URL、凭据（契约）。
登记器协议：register(ref, file_path) -> dict。9.1（B）未交付前用 FileEvidenceRegistrar 写私有目录 + JSON 元数据；
HttpEvidenceRegistrar 按「PUT /internal/evidence/{evidence_id}」写桩，接口形状待 9.1 冻结后对照。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Protocol

CONTRACT_VERSION = "2.0.0"
STATUSES = ("pending", "ready", "missing", "failed")
EVIDENCE_TYPES = ("keyframe", "video", "audio", "transcript", "scoring", "gap")
FORBIDDEN_KEYS = ("object_key", "path", "file_path", "url", "token")


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_evidence_ref(*, evidence_id: str, project_id: Optional[str], room_id: Optional[str], session_id: Optional[str],
                       source_id: str, media_run_id: Optional[str], asset_id: Optional[str], evidence_type: str,
                       start_ms: Optional[int], end_ms: Optional[int], status: str, sha256: Optional[str] = None) -> dict:
    if status not in STATUSES or evidence_type not in EVIDENCE_TYPES:
        raise ValueError("status / evidence_type 非法")
    if not isinstance(evidence_id, str) or not evidence_id.strip() or not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("evidence_id / source_id 必填")
    if status == "ready":
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise ValueError("ready 必须有 SHA256")
        if evidence_type in ("video", "audio", "keyframe") and (media_run_id is None or start_ms is None or end_ms is None):
            raise ValueError("ready 的媒体证据必须有媒体轮次与时间")
    else:
        sha256 = None
    if (start_ms is None) != (end_ms is None) or (start_ms is not None and (start_ms < 0 or end_ms < start_ms)):
        raise ValueError("起止时间必须同时存在且 end ≥ start")
    return {
        "contract_version": CONTRACT_VERSION, "project_id": project_id, "room_id": room_id, "session_id": session_id,
        "source_id": source_id, "evidence_id": evidence_id, "media_run_id": media_run_id, "asset_id": asset_id,
        "evidence_type": evidence_type, "start_ms": start_ms, "end_ms": end_ms, "sha256": sha256, "status": status,
    }


def assert_no_leak(ref: dict) -> None:
    for key in ref:
        if key in FORBIDDEN_KEYS:
            raise ValueError(f"EvidenceRef 不得带 {key}")


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in str(value))
    cleaned = cleaned.strip(".")
    return cleaned or "_"


class EvidenceRegistrar(Protocol):
    def register(self, ref: dict, file_path: Optional[Path]) -> dict: ...


class NullRegistrar:
    """测试 / 过渡：只记内存。"""

    def __init__(self) -> None:
        self.registered: list[tuple[dict, Optional[Path]]] = []

    def register(self, ref: dict, file_path: Optional[Path]) -> dict:
        assert_no_leak(ref)
        self.registered.append((dict(ref), file_path))
        return dict(ref)


class FileEvidenceRegistrar:
    """私有目录登记：<root>/<session_id 或 unbound>/<evidence_id>.mp4 + .json；摘要落盘前再算一次比对。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        if not self.root.is_absolute():
            raise ValueError("证据根目录必须是绝对路径")

    def register(self, ref: dict, file_path: Optional[Path]) -> dict:
        assert_no_leak(ref)
        safe_id = _safe_name(ref["evidence_id"])
        folder = (self.root / _safe_name(ref.get("session_id") or "unbound")).resolve()
        if folder != self.root.resolve() and self.root.resolve() not in folder.parents:
            raise ValueError("证据目录越出根目录")
        folder.mkdir(parents=True, exist_ok=True)
        if ref["status"] == "ready":
            if file_path is None or not Path(file_path).is_file():
                raise ValueError("ready 证据必须有文件")
            digest = sha256_of_file(file_path)
            if digest != ref["sha256"]:
                raise ValueError("文件摘要与 EvidenceRef 不一致")
            target = folder / f"{safe_id}.mp4"
            shutil.copyfile(file_path, target)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
        meta = folder / f"{safe_id}.json"
        fd, tmp = tempfile.mkstemp(dir=str(folder), prefix=safe_id, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(ref, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, meta)
        return dict(ref)


class HttpEvidenceRegistrar:
    """9.1 接口桩：PUT {base}/internal/evidence/{evidence_id}（Bearer；JSON 元数据 + 文件 base64 不在本桩里传，
    文件上传方式待 9.1 冻结）。网络错误抛 RuntimeError(code)，调用方记 failed。"""

    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0, opener: Optional[Callable] = None) -> None:
        if not token or not token.strip():
            raise ValueError("token 不能为空")
        self.base_url = base_url.rstrip("/")
        self._token = token.strip()
        self.timeout = timeout
        self.opener = opener or urllib.request.urlopen

    def register(self, ref: dict, file_path: Optional[Path]) -> dict:
        assert_no_leak(ref)
        body = json.dumps({"evidence": ref, "has_file": file_path is not None}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(f"{self.base_url}/internal/evidence/{ref['evidence_id']}", data=body, method="PUT",
                                         headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"})
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP_{exc.code}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise RuntimeError(type(exc).__name__) from None
        return json.loads(raw.decode("utf-8")) if raw else dict(ref)

    def __repr__(self) -> str:
        return f"<HttpEvidenceRegistrar {self.base_url}>"
