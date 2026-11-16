from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_page(viewport={"width": 1280, "height": 900})
    page.goto("http://localhost:3000", wait_until="domcontentloaded")
    page.wait_for_timeout(6000)

    # 点击第一个"详情/处置"
    btn = page.locator('button:has-text("详情/处置")').first
    print("详情按钮存在:", btn.count() > 0)
    btn.click()
    page.wait_for_timeout(1500)

    drawer = page.locator(".ant-drawer-content")
    print("抽屉打开:", drawer.is_visible())
    dt = drawer.inner_text()
    print("=== 抽屉内容 ===")
    print(dt[:250])

    # 处置：确认属实
    confirm = page.locator('button:has-text("确认属实")')
    print("确认按钮存在:", confirm.count() > 0)
    if confirm.count():
        confirm.click()
        page.wait_for_timeout(2000)
        print("=== 处置后抽屉 ===")
        print(drawer.inner_text()[:300])
        # 处置成功消息
        msg = page.locator(".ant-message")
        print("消息提示:", msg.inner_text()[:50] if msg.count() else "无")

    page.screenshot(path="decision_screenshot.png", full_page=False)
    b.close()
print("验证完成")
