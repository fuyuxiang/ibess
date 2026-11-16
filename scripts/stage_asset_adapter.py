"""执行卡 4.3：把 4.1 登记的音频 / 评分素材交给 0.3 上传并绑定到场次（脚本，管理员凭据只从环境变量读）。

流程：GET /api/sessions/{id}/assets（4.1）→ 对 audio 素材：先查已有绑定（有则不再上传）→ multipart 调 POST /api/audio/recordings（0.3，不改 ASR）
→ POST /api/sessions/{id}/media-bindings（本卡）；scoring 素材只绑定并标 scoring_check_pending（9.3 未交付）；video 素材拒绝（归 4.2）。
request_id 由 (session, asset_id, started_at) 确定性生成，重跑不会重复绑定。所有 HTTP 走可注入的 Transport，测试用假对象。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional, Protocol

log = logging.getLogger("stage-asset-adapter")


class Transport(Protocol):
    def request(self, method: str, path: str, *, headers: dict, body: Optional[bytes], content_type: Optional[str]) -> tuple[int, object]: ...


class UrllibTransport:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, path: str, *, headers: dict, body: Optional[bytes], content_type: Optional[str]) -> tuple[int, object]:
        request = urllib.request.Request(self.base_url + path, data=body, method=method, headers={**headers, **({"Content-Type": content_type} if content_type else {})})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return response.status, (json.loads(raw.decode("utf-8")) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else None
            except ValueError:
                payload = None
            return exc.code, payload


def encode_multipart(field: str, filename: str, data: bytes, content_type: str) -> tuple[bytes, str]:
    boundary = "----poc" + uuid.uuid4().hex
    safe_name = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{safe_name}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n").encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return body, f"multipart/form-data; boundary={boundary}"


def request_id_for(session_id: str, asset_id: str, started_at: str) -> str:
    return hashlib.sha256(f"{session_id}|{asset_id}|{started_at}".encode("utf-8")).hexdigest()[:32]


class StageAssetAdapter:
    def __init__(self, transport: Transport, *, cookie: str, csrf: str, assets_root: Path) -> None:
        if not cookie or not csrf:
            raise ValueError("缺管理员凭据（POC_ADMIN_COOKIE / POC_ADMIN_CSRF）")
        self.transport = transport
        self._cookie = cookie
        self._csrf = csrf
        self.assets_root = Path(assets_root)

    # ---- HTTP ----
    def _headers(self, write: bool) -> dict:
        headers = {"Cookie": f"eval_admin_session={self._cookie}", "Accept": "application/json"}
        if write:
            headers.update({"X-CSRF-Token": self._csrf, "X-Requested-With": "XMLHttpRequest"})
        return headers

    def _get(self, path: str) -> tuple[int, object]:
        return self.transport.request("GET", path, headers=self._headers(False), body=None, content_type=None)

    def _post_json(self, path: str, payload: dict) -> tuple[int, object]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self.transport.request("POST", path, headers=self._headers(True), body=body, content_type="application/json")

    # ---- 步骤 ----
    def list_assets(self, session_id: str) -> list[dict]:
        status, payload = self._get(f"/api/sessions/{session_id}/assets")
        if status != 200 or not isinstance(payload, dict):
            raise RuntimeError(f"ASSETS_HTTP_{status}")
        return list(payload.get("assets", []))

    def existing_bindings(self, session_id: str) -> dict[str, dict]:
        status, payload = self._get(f"/api/sessions/{session_id}/media-bindings")
        if status != 200 or not isinstance(payload, dict):
            raise RuntimeError(f"BINDINGS_HTTP_{status}")
        return {b["asset_id"]: b for b in payload.get("bindings", []) if b.get("asset_id")}

    def upload_audio(self, path: Path) -> str:
        if not path.is_file():
            raise RuntimeError("ASSET_FILE_MISSING")
        data = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "audio/wav"
        body, ctype = encode_multipart("file", path.name, data, content_type)
        status, payload = self.transport.request("POST", "/api/audio/recordings", headers=self._headers(True), body=body, content_type=ctype)
        if status not in (200, 201, 202) or not isinstance(payload, dict) or not payload.get("recording_id"):
            raise RuntimeError(f"UPLOAD_HTTP_{status}")
        return str(payload["recording_id"])

    def bind(self, session_id: str, *, kind: str, subject_id: str, asset_id: str, started_at: str, duration_ms: Optional[int]) -> dict:
        payload = {"request_id": request_id_for(session_id, asset_id, started_at), "kind": kind, "subject_id": subject_id,
                   "started_at": started_at, "duration_ms": duration_ms, "asset_id": asset_id}
        status, body = self._post_json(f"/api/sessions/{session_id}/media-bindings", payload)
        if status not in (200, 201) or not isinstance(body, dict):
            code = body.get("detail", {}).get("code") if isinstance(body, dict) and isinstance(body.get("detail"), dict) else None
            raise RuntimeError(f"BIND_HTTP_{status}" + (f"_{code}" if code else ""))
        return body

    def process(self, session_id: str, started_at: str, *, asset_ids: Optional[list[str]] = None) -> list[dict]:
        """逐条处理，失败项记报告继续；报告里不含凭据与绝对路径。"""
        reports: list[dict] = []
        try:
            assets = self.list_assets(session_id)
            bound = self.existing_bindings(session_id)
        except Exception as exc:  # noqa: BLE001
            return [{"asset_id": None, "action": "failed", "error": str(exc)}]
        for asset in assets:
            asset_id = asset.get("asset_id")
            if asset_ids and asset_id not in asset_ids:
                continue
            report = {"asset_id": asset_id, "asset_type": asset.get("asset_type")}
            try:
                if asset.get("asset_type") == "video":
                    report.update(action="rejected", error="video 素材由 4.2 推流控制器处理，不走本脚本")
                elif asset_id in bound:
                    existing = bound[asset_id]
                    report.update(action="skipped", binding_id=existing["binding_id"], recording_id=existing.get("subject_id") if existing.get("kind") == "recording" else None)
                elif asset.get("asset_type") == "audio":
                    path = self.assets_root.joinpath(*asset["relative_path"].split("/"))
                    recording_id = self.upload_audio(path)
                    binding = self.bind(session_id, kind="recording", subject_id=recording_id, asset_id=asset_id, started_at=started_at,
                                        duration_ms=asset.get("duration_ms"))
                    report.update(action="uploaded_and_bound", recording_id=recording_id, binding_id=binding["binding_id"],
                                  segments=len(binding.get("timeline", [])))
                elif asset.get("asset_type") == "scoring":
                    binding = self.bind(session_id, kind="scoring", subject_id=asset_id, asset_id=asset_id, started_at=started_at, duration_ms=None)
                    report.update(action="bound", binding_id=binding["binding_id"], next_step="scoring_check_pending")
                else:
                    report.update(action="rejected", error=f"未知素材类型 {asset.get('asset_type')}")
            except Exception as exc:  # noqa: BLE001  报告只给错误码或异常类型名，不带路径 / 凭据
                report.update(action="failed", error=str(exc)[:64] if isinstance(exc, RuntimeError) else type(exc).__name__)
            reports.append(report)
        return reports


def build_from_env(env: Optional[dict] = None) -> StageAssetAdapter:
    env = os.environ if env is None else env
    root = env.get("STAGE_ASSETS_ROOT", "")
    if not root or not Path(root).is_absolute() or not Path(root).is_dir():
        raise ValueError("STAGE_ASSETS_ROOT 必须是存在的绝对目录")
    return StageAssetAdapter(UrllibTransport(env.get("POC_API_BASE_URL", "http://localhost:8000")),
                             cookie=env.get("POC_ADMIN_COOKIE", ""), csrf=env.get("POC_ADMIN_CSRF", ""), assets_root=Path(root))


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="把 4.1 登记的音频 / 评分素材上传并绑定到场次")
    parser.add_argument("--session", required=True, help="场次 session_id（必填，没有默认场次）")
    parser.add_argument("--started-at", required=True, help="素材起点 UTC，如 2026-10-19T02:00:00Z")
    parser.add_argument("--asset", action="append", help="只处理指定 asset_id，可多次")
    args = parser.parse_args(argv)
    try:
        adapter = build_from_env()
    except ValueError as exc:
        print(json.dumps({"action": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2
    reports = adapter.process(args.session, args.started_at, asset_ids=args.asset)
    print(json.dumps(reports, ensure_ascii=False, indent=1))
    return 0 if all(r.get("action") != "failed" for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
