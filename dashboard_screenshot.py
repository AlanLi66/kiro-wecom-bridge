#!/usr/bin/env python3
"""
看板截图推送 — 用 Playwright 截图 dashboard 页面，推送到企微。

企微智能机器人 aibot_send_msg 只支持 markdown 文本，不支持直接发图片。
因此采用方案：截图 + 文字摘要一起推送，截图保存到本地供需要时查看。

用法：
  python3 dashboard_screenshot.py              # 截图 + 推送文字摘要
  python3 dashboard_screenshot.py --only-shot  # 只截图不推送

cron 示例（每天 9:10，在 dashboard 文字版之后）：
  10 9 * * * /mnt/i/workspace/kiro-wecom-bridge/.venv/bin/python3 /mnt/i/workspace/kiro-wecom-bridge/dashboard_screenshot.py 2>&1
"""

import asyncio
import json
import os
import sys
import urllib.request
from datetime import datetime

BRIDGE_PORT = int(os.getenv("PORT", "8900"))
DASHBOARD_URL = f"http://localhost:{BRIDGE_PORT}/dashboard"
SCREENSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports", "dashboard")
WECOM_SEND_URL = f"http://localhost:{BRIDGE_PORT}/send"
WECOM_CHATID = "dm_Alan.Li"


async def take_screenshot() -> str:
    """用 Playwright 截图看板页面，返回截图路径"""
    from playwright.async_api import async_playwright

    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = os.path.join(SCREENSHOT_DIR, f"dashboard_{timestamp}.png")
    latest_path = os.path.join(SCREENSHOT_DIR, "latest.png")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1400, "height": 900})

        await page.goto(DASHBOARD_URL, wait_until="networkidle")
        # 等待数据加载完成
        await page.wait_for_selector(".card", timeout=10000)
        # 额外等 1 秒确保渲染完成
        await asyncio.sleep(1)

        # 全页面截图
        await page.screenshot(path=screenshot_path, full_page=True)
        await browser.close()

    # 复制一份 latest
    import shutil
    shutil.copy2(screenshot_path, latest_path)

    print(f"✅ 截图已保存: {screenshot_path}")
    return screenshot_path


def cleanup_old_screenshots(keep_days: int = 3):
    """清理超过 N 天的旧截图"""
    if not os.path.exists(SCREENSHOT_DIR):
        return
    import glob
    now = datetime.now()
    for f in glob.glob(os.path.join(SCREENSHOT_DIR, "dashboard_*.png")):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(f))
            if (now - mtime).days >= keep_days:
                os.remove(f)
        except Exception:
            pass


def send_wecom(content: str) -> bool:
    """推送消息到企业微信"""
    try:
        data = json.dumps({
            "chatid": WECOM_CHATID,
            "content": content,
            "chat_type": 1,
        }).encode("utf-8")
        req = urllib.request.Request(
            WECOM_SEND_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"⚠️ 企微推送失败: {e}")
        return False


def get_summary() -> str:
    """获取文字版摘要"""
    try:
        url = f"http://localhost:{BRIDGE_PORT}/cron/dashboard"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get("markdown", "")
    except Exception as e:
        return f"⚠️ 获取摘要失败: {e}"


async def main():
    only_shot = "--only-shot" in sys.argv

    # 清理旧截图
    cleanup_old_screenshots()

    # 截图
    screenshot_path = await take_screenshot()

    if only_shot:
        return

    # 推送文字摘要 + 截图提示
    summary = get_summary()
    msg = f"{summary}\n\n📸 看板截图已保存: `reports/dashboard/latest.png`"
    if send_wecom(msg):
        print("✅ 已推送到企微")
    else:
        print("❌ 推送失败")


if __name__ == "__main__":
    asyncio.run(main())
