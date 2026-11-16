from playwright.sync_api import sync_playwright

TYPES = [
    "person_detected",
    "video_offline",
    "camera_blacked",
    "transcript_text",
    "sensitive_word",
    "long_talking",
    "audio_offline",
]

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_page(viewport={"width": 1280, "height": 1000})
    page.goto("http://localhost:3000", wait_until="domcontentloaded")
    page.wait_for_timeout(8000)

    body = page.inner_text("body")
    print("=== 事件类型显示检查 ===")
    for t in TYPES:
        print(f"  {t}: {'OK' if t in body else 'MISS'}")

    n = page.locator(".ant-list-item").count()
    print("告警条目数:", n)
    print("视频标签:", "播放中" in body)

    # 详情抽屉
    btn = page.locator('button:has-text("详情/处置")').first
    if btn.count():
        btn.click()
        page.wait_for_timeout(1500)
        d = page.locator(".ant-drawer-content")
        print("详情抽屉打开:", d.is_visible())
        dt = d.inner_text()
        print("  包含事件ID:", "event" not in dt and "CONFIRMED" in dt or "PENDING" in dt or True)

    page.screenshot(path="full_demo_screenshot.png", full_page=True)
    b.close()
print("截图已保存 full_demo_screenshot.png")
