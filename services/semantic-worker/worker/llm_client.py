"""Model boundary for S06/S07; the worker remains usable with a test client."""

import json
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from worker.result_validator import _insufficient, validate_result
from worker.semantic_rules import build_model_request, missing_context_facts


class ModelClientError(RuntimeError):
    """A safe, non-content-bearing model transport or decoding error."""


class OpenAICompatibleClient:
    """Cloud or self-hosted OpenAI-compatible client with bounded request semantics."""

    def __init__(self, base_url, model, api_key, *, timeout=60, opener=None):
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("model base URL is required")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model name is required")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("model API key is required")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("model timeout must be positive")
        self.endpoint = urljoin(base_url.rstrip("/") + "/", "chat/completions")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.opener = opener or type("DefaultOpener", (), {"open": staticmethod(urlopen)})()

    def complete(self, prompt):
        if not isinstance(prompt, dict) or not isinstance(prompt.get("system"), str) or not isinstance(prompt.get("input"), dict):
            raise ModelClientError("MODEL_REQUEST_INVALID")
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": prompt["system"]},
                {"role": "user", "content": json.dumps(prompt["input"], ensure_ascii=False, separators=(",", ":"))},
            ],
        }
        request = Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise KeyError("content")
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1] if "\n" in content else content[3:]
                if content.endswith("```"):
                    content = content[:-3].rstrip()
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError("model JSON must be object")
            return result
        except (OSError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ModelClientError("MODEL_RESPONSE_INVALID") from exc


class SemanticPipeline:
    def __init__(self, model_client):
        self.model_client = model_client

    def analyze(self, request):
        missing = missing_context_facts(request)
        if missing:
            return _insufficient(request, missing)
        raw_result = self.model_client.complete(build_model_request(request))
        return validate_result(request, raw_result)
