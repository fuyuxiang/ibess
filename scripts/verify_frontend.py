import os
from pathlib import Path
from playwright.sync_api import sync_playwright

OUTPUT_DIR = Path(os.getenv("POC_SCREENSHOT_DIR") or Path(__file__).resolve().parents[1] / "tmp" / "screenshots")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
OUT = str(OUTPUT_DIR / "slice_screenshot.png")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto("http://localhost:3000", wait_until="domcontentloaded")
    page.wait_for_timeout(6000)  # 等 React 渲染 + SSE 事件（SSE 长连接导致 networkidle 永不满足）
    page.screenshot(path=OUT, full_page=True)

    body = page.inner_text("body")
    print("=== 页面文本 ===")
    print(body[:900])

    print("=== 告警条目数 ===")
    print(page.locator(".ant-list-item").count())

    # 视频元素状态
    video = page.locator("video").first
    if video.count():
        print("=== video 元素 ===")
        print("readyState:", video.evaluate("el => el.readyState"))
        print("paused:", video.evaluate("el => el.paused"))

    browser.close()
print("截图已保存:", OUT)
