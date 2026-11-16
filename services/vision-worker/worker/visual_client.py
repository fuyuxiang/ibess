"""执行卡 7.1：多模态模型客户端（OpenAI 兼容 chat/completions，图片走 base64 JPEG）。

只接多模态模型：图片发给纯文本模型不会有视觉能力（计划 4.4）。并发 1、单次超时 60 秒、网络 / 5xx / 429 限流最多重试 2 次、
其余 4xx 不重试（429 是暂时限流，重试有意义）。任何传输层异常都转成 VisualModelError，复核器才能记 failed 观察。
凭据只从环境变量或文件读，不进日志、不进 repr。模型未配置时 from_env 返回 None，由复核器记 MODEL_NOT_CONFIGURED。
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

PROMPT_FILE = re.compile(r"^(?P<name>[a-z0-9-]+)-v(?P<version>\d+)\.txt$")


class VisualModelError(RuntimeError):
    def __init__(self, code: str, message: str = "", *, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(f"{code}: {message}" if message else code)


@dataclass(frozen=True)
class VisualModelConfig:
    base_url: str
    model: str
    api_key: str
    timeout_seconds: float = 60.0
    max_retries: int = 2

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> Optional["VisualModelConfig"]:
        env = os.environ if env is None else env
        base_url, model = env.get("VISUAL_MODEL_BASE_URL", "").strip(), env.get("VISUAL_MODEL_NAME", "").strip()
        key = env.get("VISUAL_MODEL_API_KEY", "").strip()
        key_file = env.get("VISUAL_MODEL_API_KEY_FILE", "").strip()
        if not key and key_file and Path(key_file).is_file():
            key = Path(key_file).read_text(encoding="utf-8").strip()
        if not base_url or not model or not key:
            return None
        try:
            timeout = float(env.get("VISUAL_MODEL_TIMEOUT", "60") or 60)
            retries = int(env.get("VISUAL_MODEL_MAX_RETRIES", "2") or 2)
        except ValueError:
            return None                              # 配置写错按「未配置」处理（受限状态），不让入口崩
        if timeout <= 0 or retries < 0:
            return None
        return cls(base_url=base_url, model=model, api_key=key, timeout_seconds=timeout, max_retries=retries)

    def __repr__(self) -> str:  # 不泄露 key
        return f"VisualModelConfig(base_url={self.base_url!r}, model={self.model!r}, timeout={self.timeout_seconds})"


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    text: str

    @property
    def label(self) -> str:
        return f"{self.name}-{self.version}"


def load_prompt(prompt_dir: Path, name: str, version: Optional[int] = None) -> Prompt:
    """按文件名 `<name>-v<N>.txt` 取版本；不给版本取最大。"""
    candidates = []
    for path in Path(prompt_dir).glob(f"{name}-v*.txt"):
        match = PROMPT_FILE.match(path.name)
        if match and match.group("name") == name:
            candidates.append((int(match.group("version")), path))
    if not candidates:
        raise FileNotFoundError(f"没有提示词 {name}")
    if version is not None:
        chosen = next((p for v, p in candidates if v == version), None)
        if chosen is None:
            raise FileNotFoundError(f"没有提示词 {name}-v{version}")
        chosen_version = version
    else:
        chosen_version, chosen = max(candidates)
    text = chosen.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("提示词为空")
    return Prompt(name=name, version=f"v{chosen_version}", text=text)


def encode_jpeg(frame: np.ndarray, *, max_side: int = 1024, quality: int = 85) -> bytes:
    if not isinstance(frame, np.ndarray) or frame.ndim not in (2, 3) or frame.size == 0:
        raise ValueError("帧必须是非空图像数组")
    height, width = frame.shape[:2]
    scale = max_side / max(height, width)
    if scale < 1.0:
        frame = cv2.resize(frame, (max(1, int(width * scale)), max(1, int(height * scale))), interpolation=cv2.INTER_AREA)
    ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise ValueError("JPEG 编码失败")
    return buffer.tobytes()


def crop_roi(frame: np.ndarray, bbox: tuple, *, pad: float = 0.25, min_side_px: int = 64) -> np.ndarray:
    """按归一化框裁剪并外扩 pad 比例，太小的放大到 min_side_px（给模型看清局部）。"""
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = bbox
    bw, bh = x2 - x1, y2 - y1
    px1 = int(max(0.0, x1 - bw * pad) * width)
    py1 = int(max(0.0, y1 - bh * pad) * height)
    px2 = int(min(1.0, x2 + bw * pad) * width)
    py2 = int(min(1.0, y2 + bh * pad) * height)
    crop = frame[py1:max(py1 + 1, py2), px1:max(px1 + 1, px2)]
    ch, cw = crop.shape[:2]
    if min(ch, cw) < min_side_px:
        factor = min_side_px / max(1, min(ch, cw))
        crop = cv2.resize(crop, (max(1, int(cw * factor)), max(1, int(ch * factor))), interpolation=cv2.INTER_CUBIC)
    return crop


def image_part(jpeg: bytes) -> dict:
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")}}


class OpenAICompatibleVisionClient:
    """complete(messages) -> 模型文本。messages 为 OpenAI 多模态格式。单飞锁保证并发 1。"""

    def __init__(self, config: VisualModelConfig, *, opener: Optional[Callable] = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.config = config
        self.endpoint = config.base_url.rstrip("/") + "/chat/completions"
        self.opener = opener or urllib.request.urlopen
        self.sleep = sleep
        self._lock = threading.Lock()
        self.calls = 0

    def complete(self, messages: list[dict], *, temperature: float = 0.0, max_tokens: int = 400) -> str:
        request_body = {"model": self.config.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
                        "response_format": {"type": "json_object"}}
        if 'minimax-m3' in self.config.model.lower():
            # M3.1 always reasons; the default maximum reasoning depth can exhaust short JSON budgets.
            request_body.pop('max_tokens')
            request_body.update(reasoning_effort='low', max_completion_tokens=max(4096,max_tokens))
        body = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
        attempts = self.config.max_retries + 1
        last: Optional[VisualModelError] = None
        with self._lock:
            for attempt in range(attempts):
                self.calls += 1
                request = urllib.request.Request(self.endpoint, data=body, method="POST", headers={
                    "Content-Type": "application/json", "Authorization": f"Bearer {self.config.api_key}", "Accept": "application/json"})
                try:
                    with self.opener(request, timeout=self.config.timeout_seconds) as response:
                        raw = response.read()
                    return self._extract(raw)
                except urllib.error.HTTPError as exc:
                    retryable = exc.code >= 500 or exc.code == 429
                    last = VisualModelError(f"UPSTREAM_{exc.code}", retryable=retryable)
                    if not retryable:
                        raise last from None
                except TimeoutError:
                    last = VisualModelError("TIMEOUT", retryable=True)
                except http.client.HTTPException:          # IncompleteRead、RemoteDisconnected 等
                    last = VisualModelError("NETWORK", retryable=True)
                except (urllib.error.URLError, OSError) as exc:
                    if isinstance(getattr(exc, "reason", None), TimeoutError) or "timed out" in str(exc).lower():
                        last = VisualModelError("TIMEOUT", retryable=True)
                    else:
                        last = VisualModelError("NETWORK", retryable=True)
                if attempt < attempts - 1:
                    self.sleep(min(8.0, 2.0 ** attempt))
        raise last if last is not None else VisualModelError("UNKNOWN")

    @staticmethod
    def _extract(raw: bytes) -> str:
        try:
            data = json.loads(raw.decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise VisualModelError("BAD_RESPONSE", "上游响应不是 chat.completions 形状") from None
        if isinstance(content, list):
            content = "".join(part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str))
        if not isinstance(content, str):
            raise VisualModelError("BAD_RESPONSE", "内容不是文本")
        return content

    def __repr__(self) -> str:
        return f"<OpenAICompatibleVisionClient {self.config.model} @ {self.config.base_url}>"
