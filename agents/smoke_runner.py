"""Agent 驱动的智能冒烟测试 — 将测试场景拆分为多个 prompt 发给 agent

与传统 web_tester.py smoke 的区别：
- 传统：硬编码 Playwright 步骤，页面改版就挂
- Agent 驱动：描述"要验证什么"，agent 自己决定怎么操作和判断

每个场景 = 1 次 prompt，agent 内部会调用 Playwright MCP 工具完成操作。
所有场景串行执行，结果汇总后推送企微。

用法：
  # 通过 cron 触发（替代原来的 web_tester.py smoke）
  curl -X POST http://localhost:8900/cron/trigger -H 'Content-Type: application/json' \
    -d '{"chatid":"dm_Alan.Li","prompt":"[cron]: 执行智能冒烟测试"}'

  # 或直接调用 API
  curl -X POST http://localhost:8900/smoke/agent -H 'Content-Type: application/json' \
    -d '{"chatid":"dm_Alan.Li","env":"uat"}'
"""
import asyncio
import json
import logging
import os
import time

log = logging.getLogger(__name__)

WORK_DIR = os.getenv("KIRO_WORK_DIR", "/mnt/i/workspace/alan_bot")

# UAT 环境 URL
ENVS = {
    "uat": {
        "base": "https://uat-www.yamibuy.tech",
        "trade": "https://uat-trade.yamibuy.tech",
        "customer": "https://uat-customer.yamibuy.tech",
    },
    "prod": {
        "base": "https://www.yamibuy.com",
        "trade": "https://trade.yamibuy.com",
        "customer": "https://customer.yamibuy.com",
    },
}

# 测试场景：每个场景是一个自然语言描述，agent 自己决定怎么操作
SCENARIOS = [
    {
        "id": "homepage",
        "name": "首页加载",
        "prompt": (
            "请打开 {base}/zh 首页，验证以下内容：\n"
            "1. 页面正常加载（HTTP 200，无白屏）\n"
            "2. 顶部导航栏、搜索框、Logo 正常显示\n"
            "3. 首页有商品展示区域（轮播图或商品卡片）\n"
            "4. 截图保存\n\n"
            "判断标准：\n"
            "- 只要页面核心内容正常渲染就算 PASS\n"
            "- console error 如果是第三方脚本（analytics/ads/tracking）报错，不影响判定\n"
            "- 只有页面白屏、核心功能缺失、HTTP 非 200 才算 FAIL\n\n"
            "回复格式：✅ PASS 或 ❌ FAIL + 简短说明"
        ),
    },
    {
        "id": "search",
        "name": "搜索功能",
        "prompt": (
            "请在 {base}/zh 页面执行搜索操作：\n"
            "1. 在搜索框输入 'ramen' 并搜索\n"
            "2. 验证搜索结果页正常加载，有商品列表\n"
            "3. 验证商品卡片包含：图片、名称、价格\n"
            "4. 截图保存\n\n"
            "判断标准：搜索结果正常展示 = PASS，无结果或页面异常 = FAIL\n"
            "console error 不影响判定（除非导致页面功能不可用）\n\n"
            "回复格式：✅ PASS 或 ❌ FAIL + 简短说明"
        ),
    },
    {
        "id": "pdp",
        "name": "商品详情页",
        "prompt": (
            "请打开一个商品详情页（从 {base}/zh/search?q=snack 搜索结果中点击第一个商品）：\n"
            "1. 验证 PDP 页面正常加载\n"
            "2. 检查关键元素：商品图片、名称、价格、加购按钮\n"
            "3. 截图保存\n\n"
            "判断标准：PDP 核心信息正常展示 = PASS\n"
            "console error 不影响判定\n\n"
            "回复格式：✅ PASS 或 ❌ FAIL + 简短说明"
        ),
    },
    {
        "id": "cart",
        "name": "购物车",
        "prompt": (
            "请打开购物车页面 {trade}/zh/cart：\n"
            "1. 验证购物车页面正常加载（不是白屏或报错页）\n"
            "2. 如果有商品，检查商品信息是否正常显示\n"
            "3. 如果购物车为空，确认空状态提示正常\n"
            "4. 截图保存\n\n"
            "判断标准：\n"
            "- 页面正常渲染（有购物车内容或空状态） = PASS\n"
            "- 跳转到登录页 = FAIL（cookie 过期）\n"
            "- 白屏/500 = FAIL\n\n"
            "回复格式：✅ PASS 或 ❌ FAIL + 简短说明"
        ),
    },
    {
        "id": "account",
        "name": "个人中心",
        "prompt": (
            "请打开个人中心订单页 {customer}/zh/orders：\n"
            "1. 验证页面正常加载（不是登录页）\n"
            "2. 检查订单列表或空状态是否正常显示\n"
            "3. 截图保存\n\n"
            "判断标准：\n"
            "- 页面正常渲染 = PASS\n"
            "- 跳转到登录页 = FAIL（cookie 过期）\n\n"
            "回复格式：✅ PASS 或 ❌ FAIL + 简短说明"
        ),
    },
    {
        "id": "mobile_home",
        "name": "H5首页",
        "prompt": (
            "请用移动端视口（375x812）打开 {base}/zh：\n"
            "1. 验证 H5 首页正常加载\n"
            "2. 检查底部导航栏是否显示\n"
            "3. 截图保存\n\n"
            "判断标准：移动端布局正常渲染 = PASS\n\n"
            "回复格式：✅ PASS 或 ❌ FAIL + 简短说明"
        ),
    },
]

# Agent 的系统提示
SMOKE_AGENT_SYSTEM = """你是一个 Web 自动化测试工程师。你的任务是使用 Playwright 浏览器工具验证网页功能。

规则：
1. 使用 launch_browser 启动浏览器（如果还没启动）
2. 使用 execute_script 执行 Playwright 操作
3. 每个测试场景独立判断通过/失败
4. 发现问题时给出具体描述（URL、错误信息、截图）
5. 回复格式必须包含：
   - 状态：✅ PASS 或 ❌ FAIL
   - 详情：具体验证了什么，发现了什么
   - 如果失败：可能的原因分析

保持简洁，每个场景回复不超过 300 字。
"""


async def run_agent_smoke(chatid: str, env: str, pool, ws):
    """执行 agent 驱动的冒烟测试

    Args:
        chatid: 推送结果的 chatid
        env: 测试环境 (uat/prod)
        pool: ProcessPool 实例
        ws: WsClient 实例（用于推送消息）
    """
    env_config = ENVS.get(env, ENVS["uat"])
    chat_type = 1 if chatid.startswith("dm_") else 2
    results = []
    start_time = time.time()

    await ws.send_msg(chatid, chat_type,
        f"🧪 **智能冒烟测试启动** ({env.upper()})\n"
        f"- 场景数: {len(SCENARIOS)}\n"
        f"- 模式: Agent 驱动（自主判断）\n---")

    # 获取或创建 agent 进程
    proc = await pool.get_or_create(
        f"{chatid}/_smoke_agent", agent=None,
        cwd=WORK_DIR, mode="full")

    # 首次发送系统提示
    init_prompt = (
        f"{SMOKE_AGENT_SYSTEM}\n\n"
        f"测试环境: {env.upper()}\n"
        f"Base URL: {env_config['base']}\n"
        f"Trade URL: {env_config['trade']}\n"
        f"Customer URL: {env_config['customer']}\n\n"
        f"请先启动浏览器，准备开始测试。回复「准备就绪」即可。"
    )
    await proc.send(init_prompt, timeout=60)

    # 逐个场景执行
    for i, scenario in enumerate(SCENARIOS, 1):
        scenario_prompt = scenario["prompt"].format(**env_config)
        full_prompt = f"[测试场景 {i}/{len(SCENARIOS)}] {scenario['name']}\n\n{scenario_prompt}"

        try:
            reply = await proc.send(full_prompt, timeout=120)
            # 判断通过/失败
            passed = "✅" in reply or "PASS" in reply.upper()
            status = "passed" if passed else "failed"
            results.append({
                "id": scenario["id"],
                "name": scenario["name"],
                "status": status,
                "detail": reply[:500],
            })

            # 实时推送每个场景结果
            icon = "✅" if passed else "❌"
            await ws.send_msg(chatid, chat_type,
                f"{icon} [{i}/{len(SCENARIOS)}] **{scenario['name']}**\n{reply[:800]}")

        except Exception as e:
            results.append({
                "id": scenario["id"],
                "name": scenario["name"],
                "status": "error",
                "detail": str(e),
            })
            await ws.send_msg(chatid, chat_type,
                f"⚠️ [{i}/{len(SCENARIOS)}] **{scenario['name']}** — 执行异常: {e}")

        # 场景间短暂间隔
        await asyncio.sleep(2)

    # 汇总
    duration = int(time.time() - start_time)
    passed_count = sum(1 for r in results if r["status"] == "passed")
    failed_count = sum(1 for r in results if r["status"] == "failed")
    error_count = sum(1 for r in results if r["status"] == "error")

    summary = (
        f"📊 **智能冒烟测试完成** ({env.upper()})\n"
        f"- 耗时: {duration}s\n"
        f"- 通过: {passed_count}/{len(results)}\n"
        f"- 失败: {failed_count}\n"
        f"- 异常: {error_count}\n"
    )

    if failed_count > 0:
        summary += "\n❌ 失败场景:\n"
        for r in results:
            if r["status"] == "failed":
                summary += f"  • {r['name']}\n"

    await ws.send_msg(chatid, chat_type, summary)

    log.info("Agent smoke 完成 chatid=%s env=%s passed=%d failed=%d duration=%ds",
             chatid, env, passed_count, failed_count, duration)

    return results
