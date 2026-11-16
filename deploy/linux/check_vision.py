"""验证配置的 MiniMax 模型是否支持图片输入（多模态）。"""
import base64
import json
import os
import sys
import urllib.request

BASE = os.environ["SEMANTIC_MODEL_BASE_URL"].rstrip("/")
KEY = os.environ["SEMANTIC_MODEL_API_KEY"]
MODEL = os.environ["SEMANTIC_MODEL_NAME"]
IMG = sys.argv[1]

with open(IMG, "rb") as f:
    b64 = base64.b64encode(f.read()).decode()

payload = {
    "model": MODEL,
    "messages": [{
        "role": "user",
        "content": [
            {"type": "text", "text": "这张图片里是什么场景？画面里有几个人？只回答一句话。"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
        ],
    }],
    "max_tokens": 120,
}
req = urllib.request.Request(
    BASE + "/chat/completions",
    data=json.dumps(payload).encode(),
    headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=90) as resp:
        body = json.loads(resp.read().decode())
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    msg = message.get("content") or message.get("reasoning_content")
    if msg is None:
        print("RAW:", json.dumps(body, ensure_ascii=False)[:400])
    else:
        print("RESULT_OK:", str(msg)[:200])
except urllib.error.HTTPError as exc:
    detail = exc.read().decode("utf-8", "ignore")[:300]
    print("HTTP", exc.code, detail)
except Exception as exc:
    print("ERR", type(exc).__name__, str(exc)[:160])
