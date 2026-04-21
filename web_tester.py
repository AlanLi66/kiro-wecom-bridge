#!/usr/bin/env python3
"""Web E2E 测试器 — Playwright 驱动，支持健康检查、冒烟测试、自由操作。
结果存 SQLite + HTML 报告，退化推企微告警。

用法:
  python3 web_tester.py health [--url URL]
  python3 web_tester.py smoke [--env uat|prod]
  python3 web_tester.py run --steps '[...]'
  python3 web_tester.py login --env uat|prod
  python3 web_tester.py report
"""
import json, os, sys, sqlite3, argparse, time, base64, traceback
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

BRIDGE_DIR = Path(__file__).resolve().parent
REPORT_DIR = BRIDGE_DIR / "reports" / "web_test"
DB_PATH = BRIDGE_DIR / "reports" / "web_test.db"
SCREENSHOT_DIR = REPORT_DIR / "screenshots"
COOKIE_DIR = BRIDGE_DIR / "web_test_cookies"
BRIDGE_PORT = int(os.getenv("PORT", "8900"))

# ── 环境配置 ──────────────────────────────────
ENVS = {
    "uat": {
        "base_url": "https://uat-www.yamibuy.tech",
        "trade_url": "https://uat-trade.yamibuy.tech",
        "customer_url": "https://uat-customer.yamibuy.tech",
        "name": "UAT",
    },
    "prod": {
        "base_url": "https://www.yamibuy.com",
        "trade_url": "https://trade.yamibuy.com",
        "customer_url": "https://customer.yamibuy.com",
        "name": "Production",
    },
}

# ── 冒烟测试用例 ──────────────────────────────
SMOKE_TESTS = [
    {
        "id": "homepage",
        "name": "首页加载+加购",
        "needs_login": True,
        "steps": [
            {"action": "goto", "path": "/zh"},
            {"action": "wait", "selector": "[data-qa-header-logo-img]", "timeout": 10000},
            {"action": "screenshot", "name": "homepage"},
            {"action": "check_console_errors"},
            # 首页没有商品卡片加购按钮，搜索一个商品再加购
            {"action": "fill", "selector": "[data-qa-header-search-input]", "text": "bread"},
            {"action": "click", "selector": "[data-qa-index-search-btn]", "timeout": 5000},
            {"action": "wait_navigation", "timeout": 15000},
            {"action": "wait", "selector": "[data-qa-itemcard-addcart-btn]", "timeout": 15000},
            {"action": "click", "selector": "[data-qa-itemcard-addcart-btn]", "timeout": 5000},
            {"action": "sleep", "seconds": 2},
            {"action": "screenshot", "name": "homepage_add_cart"},
            {"action": "check_status"},
        ],
    },
    {
        "id": "search",
        "name": "搜索+加购",
        "needs_login": True,
        "steps": [
            {"action": "goto", "path": "/zh/search?q=bread"},
            {"action": "wait", "selector": "[data-qa-itemcard]", "timeout": 15000},
            {"action": "screenshot", "name": "search_result"},
            {"action": "click", "selector": "[data-qa-itemcard-addcart-btn]", "timeout": 5000},
            {"action": "sleep", "seconds": 2},
            {"action": "screenshot", "name": "search_add_cart"},
            {"action": "check_console_errors"},
            {"action": "check_status"},
        ],
    },
    {
        "id": "pdp",
        "name": "商品详情页+加购",
        "needs_login": True,
        "steps": [
            {"action": "goto", "path": "/zh/search?q=bread"},
            {"action": "wait", "selector": "[data-qa-itemcard-name-txt]", "timeout": 15000},
            {"action": "click", "selector": "[data-qa-itemcard-name-txt]", "timeout": 5000},
            {"action": "wait_navigation", "timeout": 15000},
            {"action": "wait", "selector": "[data-qa-pdp-addcart-btn]", "timeout": 10000},
            {"action": "screenshot", "name": "pdp"},
            # PDP 有两个加购按钮（sticky header + 正文），点击可见的那个
            {"action": "evaluate", "script": "(() => { const btns = document.querySelectorAll('[data-qa-pdp-addcart-btn]'); for (const b of btns) { if (b.offsetParent !== null) { b.click(); return 'clicked'; } } return 'not found'; })()"},
            {"action": "sleep", "seconds": 2},
            {"action": "screenshot", "name": "pdp_add_cart"},
            {"action": "check_console_errors"},
            {"action": "check_status"},
        ],
    },
    {
        "id": "cart",
        "name": "购物车+结算下单",
        "needs_login": True,
        "steps": [
            # 进入购物车
            {"action": "goto", "url_key": "trade_url", "path": "/zh/cart"},
            {"action": "wait", "selector": "[data-qa-cart-title]", "timeout": 15000},
            {"action": "screenshot", "name": "cart"},
            # 加购推荐商品
            {"action": "wait", "selector": "[data-qa-itemcard-addcart-btn]", "timeout": 10000},
            {"action": "click", "selector": "[data-qa-itemcard-addcart-btn]", "timeout": 5000},
            {"action": "sleep", "seconds": 2},
            {"action": "screenshot", "name": "cart_add_recommend"},
            # 点击去结算
            {"action": "click", "selector": "[data-qa-cart-checkout-btn]", "timeout": 5000},
            {"action": "wait_navigation", "timeout": 15000},
            {"action": "wait", "selector": "[data-qa-place-order-btn]", "timeout": 15000},
            {"action": "screenshot", "name": "checkout"},
            # 切换到银行卡支付（可能已选中，用 unchecked 版本点击；如果已选中则跳过）
            {"action": "sleep", "seconds": 2},
            {"action": "evaluate", "script": "(() => { const unchecked = document.querySelector('[data-qa-unchecked-creditcard-rbtn]'); if (unchecked) { unchecked.click(); return 'switched to creditcard'; } const checked = document.querySelector('[data-qa-checked-creditcard-rbtn]'); if (checked) { return 'already creditcard'; } return 'creditcard rbtn not found'; })()"},
            {"action": "sleep", "seconds": 1},
            {"action": "screenshot", "name": "checkout_creditcard"},
            # 提交订单
            {"action": "click", "selector": "[data-qa-place-order-btn]", "timeout": 5000},
            # 等待 CVV 弹窗出现
            {"action": "wait", "selector": "[data-qa-cvv-input]", "timeout": 15000},
            {"action": "screenshot", "name": "checkout_cvv_dialog"},
            # 输入 CVV
            {"action": "click", "selector": "[data-qa-cvv-input]", "timeout": 5000},
            {"action": "type", "selector": "[data-qa-cvv-input]", "text": "111"},
            {"action": "screenshot", "name": "checkout_cvv"},
            # 确认提单
            {"action": "click", "selector": "[data-qa-cvv-confirm-btn]", "timeout": 5000},
            {"action": "sleep", "seconds": 5},
            # 进入支付结果页
            {"action": "screenshot", "name": "payment_result"},
            {"action": "check_console_errors"},
            {"action": "check_status"},
        ],
    },
    {
        "id": "account",
        "name": "个人中心",
        "needs_login": True,
        "steps": [
            {"action": "goto", "url_key": "customer_url", "path": "/zh/orders"},
            {"action": "wait", "selector": "[data-qa-orders-items-container]", "timeout": 15000},
            {"action": "screenshot", "name": "account"},
            {"action": "check_console_errors"},
            {"action": "check_status"},
        ],
    },
]


# ── DB ────────────────────────────────────────
def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS test_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_time TEXT NOT NULL,
            env TEXT NOT NULL,
            test_type TEXT NOT NULL,
            total_tests INTEGER DEFAULT 0,
            passed INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0,
            errors INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS test_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_time TEXT NOT NULL,
            env TEXT NOT NULL,
            test_id TEXT NOT NULL,
            test_name TEXT NOT NULL,
            status TEXT NOT NULL,
            duration_ms INTEGER DEFAULT 0,
            url TEXT DEFAULT '',
            http_status INTEGER DEFAULT 0,
            console_errors TEXT DEFAULT '[]',
            screenshot TEXT DEFAULT '',
            error_message TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_runs_time ON test_runs(run_time);
        CREATE INDEX IF NOT EXISTS idx_results_time ON test_results(run_time);
    """)
    return conn


# ── Playwright 执行引擎 ──────────────────────
class WebTestRunner:
    """Playwright 测试执行器"""

    def __init__(self, env: str = "uat", headless: bool = True):
        self.env = env
        self.env_config = ENVS.get(env, ENVS["uat"])
        self.base_url = self.env_config["base_url"]
        self.headless = headless
        self.browser = None
        self.context = None
        self.page = None
        self.console_errors = []
        self.results = []

    def _cookie_path(self) -> Path:
        COOKIE_DIR.mkdir(parents=True, exist_ok=True)
        return COOKIE_DIR / f"{self.env}_cookies.json"

    def start(self, use_cookies: bool = True):
        """启动浏览器"""
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=self.headless)

        cookie_path = self._cookie_path()
        if use_cookies and cookie_path.exists():
            self.context = self.browser.new_context(
                storage_state=str(cookie_path),
                viewport={"width": 1440, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
        else:
            self.context = self.browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )

        self.page = self.context.new_page()
        self.console_errors = []
        self.page.on("console", self._on_console)
        self.page.on("pageerror", self._on_page_error)

    def _on_console(self, msg):
        if msg.type == "error":
            self.console_errors.append({"type": "console.error", "text": msg.text[:500]})

    def _on_page_error(self, error):
        self.console_errors.append({"type": "page_error", "text": str(error)[:500]})

    def save_cookies(self):
        """保存登录态 cookie"""
        if self.context:
            cookie_path = self._cookie_path()
            self.context.storage_state(path=str(cookie_path))
            print(f"✅ Cookie 已保存: {cookie_path}", file=sys.stderr)

    def stop(self):
        """关闭浏览器"""
        if self.browser:
            self.browser.close()
        if self._pw:
            self._pw.stop()

    def _resolve_url(self, path_or_url: str) -> str:
        if path_or_url.startswith("http"):
            return path_or_url
        return self.base_url.rstrip("/") + "/" + path_or_url.lstrip("/")

    def _take_screenshot(self, name: str) -> str:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"{name}_{ts}.png"
        path = SCREENSHOT_DIR / fname
        self.page.screenshot(path=str(path), full_page=False)
        return str(path)

    def execute_step(self, step: dict) -> dict:
        """执行单个测试步骤"""
        action = step.get("action", "")
        result = {"action": action, "success": True, "detail": ""}

        try:
            if action == "goto":
                url_key = step.get("url_key")
                if url_key and url_key in self.env_config:
                    base = self.env_config[url_key].rstrip("/")
                    url = base + "/" + step.get("path", "/").lstrip("/")
                else:
                    url = self._resolve_url(step.get("url", step.get("path", "/")))
                resp = self.page.goto(url, wait_until="domcontentloaded", timeout=step.get("timeout", 30000))
                # 等待 React hydration 完成
                self.page.wait_for_timeout(2000)
                result["detail"] = f"HTTP {resp.status if resp else '?'} → {url}"
                result["http_status"] = resp.status if resp else 0
                result["url"] = url
                # 自动关闭 cookie consent banner（如果存在）
                try:
                    btn = self.page.query_selector("[data-qa-index-accept-cookie-btn]")
                    if btn and btn.is_visible():
                        btn.click()
                        time.sleep(0.5)
                except Exception:
                    pass

            elif action == "wait":
                sel = step.get("selector", "body")
                self.page.wait_for_selector(sel, timeout=step.get("timeout", 10000))
                result["detail"] = f"Found: {sel}"

            elif action == "wait_navigation":
                self.page.wait_for_load_state("domcontentloaded", timeout=step.get("timeout", 15000))
                result["detail"] = f"Navigation complete: {self.page.url}"

            elif action == "click":
                sel = step["selector"]
                self.page.click(sel, timeout=step.get("timeout", 10000))
                result["detail"] = f"Clicked: {sel}"

            elif action == "click_first_link":
                sel = step["selector"]
                links = self.page.query_selector_all(sel)
                if links:
                    href = links[0].get_attribute("href") or ""
                    links[0].click()
                    result["detail"] = f"Clicked first link: {href}"
                else:
                    result["success"] = False
                    result["detail"] = f"No links found: {sel}"

            elif action == "fill":
                sel = step["selector"]
                # 尝试多个选择器（逗号分隔）
                selectors = [s.strip() for s in sel.split(",")]
                filled = False
                for s in selectors:
                    try:
                        el = self.page.query_selector(s)
                        if el:
                            el.fill(step["text"])
                            result["detail"] = f"Filled '{step['text']}' into {s}"
                            filled = True
                            break
                    except Exception:
                        continue
                if not filled:
                    result["success"] = False
                    result["detail"] = f"No input found for: {sel}"

            elif action == "press":
                self.page.keyboard.press(step["key"])
                result["detail"] = f"Pressed: {step['key']}"

            elif action == "type":
                sel = step["selector"]
                self.page.type(sel, step["text"])
                result["detail"] = f"Typed into {sel}"

            elif action == "screenshot":
                path = self._take_screenshot(step.get("name", "screenshot"))
                result["detail"] = f"Screenshot: {path}"
                result["screenshot"] = path

            elif action == "check_console_errors":
                errs = [e for e in self.console_errors]
                result["console_errors"] = errs
                if errs:
                    result["detail"] = f"{len(errs)} console error(s)"
                else:
                    result["detail"] = "No console errors"

            elif action == "check_status":
                result["detail"] = f"Current URL: {self.page.url}"
                result["url"] = self.page.url

            elif action == "get_text":
                sel = step["selector"]
                el = self.page.query_selector(sel)
                text = el.inner_text() if el else ""
                result["detail"] = text[:1000]

            elif action == "get_attribute":
                sel = step["selector"]
                attr = step["attribute"]
                el = self.page.query_selector(sel)
                val = el.get_attribute(attr) if el else ""
                result["detail"] = f"{attr}={val}"

            elif action == "evaluate":
                js = step["script"]
                val = self.page.evaluate(js)
                result["detail"] = json.dumps(val, ensure_ascii=False, default=str)[:2000]

            elif action == "wait_for_url":
                import re as _re
                pattern = step.get("pattern", "")
                self.page.wait_for_url(_re.compile(pattern), timeout=step.get("timeout", 15000))
                result["detail"] = f"URL matched: {self.page.url}"

            elif action == "sleep":
                time.sleep(step.get("seconds", 1))
                result["detail"] = f"Slept {step.get('seconds', 1)}s"

            else:
                result["success"] = False
                result["detail"] = f"Unknown action: {action}"

        except Exception as e:
            result["success"] = False
            result["detail"] = f"Error: {str(e)[:500]}"

        return result

    def run_test(self, test: dict) -> dict:
        """执行一个完整测试用例"""
        test_id = test.get("id", "unknown")
        test_name = test.get("name", test_id)
        steps = test.get("steps", [])

        start_time = time.time()
        self.console_errors = []
        step_results = []
        status = "passed"
        error_msg = ""
        http_status = 0
        url = ""
        screenshot = ""
        all_console_errors = []

        for step in steps:
            r = self.execute_step(step)
            step_results.append(r)
            if r.get("http_status"):
                http_status = r["http_status"]
            if r.get("url"):
                url = r["url"]
            if r.get("screenshot"):
                screenshot = r["screenshot"]
            if r.get("console_errors"):
                all_console_errors.extend(r["console_errors"])
            if not r["success"]:
                status = "failed"
                error_msg = r["detail"]
                break

        duration = int((time.time() - start_time) * 1000)

        return {
            "test_id": test_id,
            "test_name": test_name,
            "status": status,
            "duration_ms": duration,
            "url": url,
            "http_status": http_status,
            "console_errors": all_console_errors,
            "screenshot": screenshot,
            "error_message": error_msg,
            "steps": step_results,
        }


# ── 命令: health ──────────────────────────────
def cmd_health(args):
    """页面健康检查"""
    urls = []
    if args.url:
        urls = [args.url]
    else:
        env_config = ENVS.get(args.env, ENVS["uat"])
        base = env_config["base_url"]
        trade = env_config.get("trade_url", base)
        urls = [
            f"{base}/zh",
            f"{base}/zh/search?q=ramen",
            f"{trade}/zh/cart",
        ]

    runner = WebTestRunner(env=args.env, headless=True)
    runner.start(use_cookies=True)
    results = []

    try:
        for url in urls:
            test = {
                "id": f"health_{url.split('/')[-1] or 'root'}",
                "name": f"Health: {url}",
                "steps": [
                    {"action": "goto", "url": url},
                    {"action": "wait", "selector": "body", "timeout": 15000},
                    {"action": "screenshot", "name": f"health_{url.replace('/', '_').replace(':', '')}"},
                    {"action": "check_console_errors"},
                    {"action": "check_status"},
                ],
            }
            r = runner.run_test(test)
            results.append(r)
    finally:
        runner.stop()

    # 保存结果
    run_time = _save_results(args.env, "health", results)

    # 输出
    output = _format_output(run_time, args.env, "health", results)
    print(json.dumps(output, ensure_ascii=False, indent=2))


# ── 命令: smoke ───────────────────────────────
def cmd_smoke(args):
    """冒烟测试"""
    runner = WebTestRunner(env=args.env, headless=True)
    has_cookies = runner._cookie_path().exists()
    runner.start(use_cookies=True)
    results = []

    try:
        for test in SMOKE_TESTS:
            if test.get("needs_login") and not has_cookies:
                results.append({
                    "test_id": test["id"],
                    "test_name": test["name"],
                    "status": "skipped",
                    "duration_ms": 0,
                    "url": "",
                    "http_status": 0,
                    "console_errors": [],
                    "screenshot": "",
                    "error_message": "需要登录态，请先运行: web_tester.py login",
                    "steps": [],
                })
                continue
            r = runner.run_test(test)
            results.append(r)
    finally:
        runner.stop()

    run_time = _save_results(args.env, "smoke", results)
    alerts = _check_regression(args.env, "smoke", results)

    # 生成 HTML 报告
    _generate_html(run_time, args.env, "smoke", results, alerts)

    output = _format_output(run_time, args.env, "smoke", results)
    output["alerts"] = alerts
    print(json.dumps(output, ensure_ascii=False, indent=2))

    # 退化告警
    if alerts and args.chatid:
        msg = f"⚠️ Web 冒烟测试退化告警 ({ENVS[args.env]['name']})\n" + "\n".join(alerts)
        _notify_wecom(args.chatid, msg)

    # 失败通知
    failed_tests = [r for r in results if r["status"] == "failed"]
    if failed_tests and args.chatid:
        lines = [f"❌ Web 冒烟测试失败 ({ENVS[args.env]['name']}) — {len(failed_tests)}/{len(results)} 失败"]
        for r in failed_tests:
            lines.append(f"  • {r['test_name']}: {r.get('error_message', 'unknown')}")
        _notify_wecom(args.chatid, "\n".join(lines))

    # 清理 3 天前的报告、截图和 DB 记录
    cleanup_old_reports(keep_days=3)


# ── 命令: run ─────────────────────────────────
def cmd_run(args):
    """自由操作模式"""
    steps = json.loads(args.steps)
    runner = WebTestRunner(env=args.env, headless=True)
    runner.start(use_cookies=True)

    try:
        test = {
            "id": "custom_run",
            "name": args.name or "Custom Test",
            "steps": steps,
        }
        result = runner.run_test(test)
    finally:
        runner.stop()

    run_time = _save_results(args.env, "run", [result])
    output = _format_output(run_time, args.env, "run", [result])
    print(json.dumps(output, ensure_ascii=False, indent=2))


# ── 命令: login ───────────────────────────────
def cmd_login(args):
    """交互式登录，保存 cookie"""
    runner = WebTestRunner(env=args.env, headless=False)
    runner.start(use_cookies=False)

    login_url = runner._resolve_url("/zh/login")
    print(f"🔐 正在打开登录页: {login_url}", file=sys.stderr)
    print("请在浏览器中完成登录，登录成功后按 Enter 继续...", file=sys.stderr)

    try:
        runner.page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
        input(">>> 登录完成后按 Enter 保存 cookie...")
        runner.save_cookies()
        print(json.dumps({"status": "ok", "message": f"Cookie 已保存到 {runner._cookie_path()}", "env": args.env}, ensure_ascii=False))
    finally:
        runner.stop()


# ── 命令: report ──────────────────────────────
def cmd_report(args):
    """查看最近测试结果"""
    conn = _db()
    runs = conn.execute(
        "SELECT * FROM test_runs ORDER BY run_time DESC LIMIT ?",
        (args.limit,)
    ).fetchall()

    output = []
    for run in runs:
        results = conn.execute(
            "SELECT * FROM test_results WHERE run_time = ? AND env = ?",
            (run["run_time"], run["env"])
        ).fetchall()
        output.append({
            "run_time": run["run_time"],
            "env": run["env"],
            "test_type": run["test_type"],
            "total": run["total_tests"],
            "passed": run["passed"],
            "failed": run["failed"],
            "tests": [dict(r) for r in results],
        })
    conn.close()
    print(json.dumps(output, ensure_ascii=False, indent=2))


# ── 存储 & 报告 ───────────────────────────────
def _save_results(env: str, test_type: str, results: list) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _db()
    passed = sum(1 for r in results if r["status"] == "passed")
    failed = sum(1 for r in results if r["status"] == "failed")
    errors = sum(1 for r in results if r["status"] == "error")

    conn.execute(
        "INSERT INTO test_runs (run_time,env,test_type,total_tests,passed,failed,errors) VALUES (?,?,?,?,?,?,?)",
        (now, env, test_type, len(results), passed, failed, errors)
    )
    for r in results:
        conn.execute(
            "INSERT INTO test_results (run_time,env,test_id,test_name,status,duration_ms,url,http_status,console_errors,screenshot,error_message) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (now, env, r["test_id"], r["test_name"], r["status"], r.get("duration_ms", 0),
             r.get("url", ""), r.get("http_status", 0),
             json.dumps(r.get("console_errors", []), ensure_ascii=False),
             r.get("screenshot", ""), r.get("error_message", ""))
        )
    conn.commit()
    conn.close()
    return now


def _check_regression(env: str, test_type: str, results: list) -> list:
    alerts = []
    conn = _db()
    times = conn.execute(
        "SELECT DISTINCT run_time FROM test_runs WHERE env=? AND test_type=? ORDER BY run_time DESC LIMIT 2",
        (env, test_type)
    ).fetchall()
    if len(times) < 2:
        conn.close()
        return alerts

    prev_time = times[1]["run_time"]
    prev_results = conn.execute(
        "SELECT test_id, status FROM test_results WHERE run_time=? AND env=?",
        (prev_time, env)
    ).fetchall()
    prev_map = {r["test_id"]: r["status"] for r in prev_results}

    for r in results:
        prev_status = prev_map.get(r["test_id"])
        if prev_status == "passed" and r["status"] == "failed":
            alerts.append(f"❌ {r['test_name']}: passed → failed ({r.get('error_message', '')})")

    conn.close()
    return alerts


def _format_output(run_time: str, env: str, test_type: str, results: list) -> dict:
    passed = sum(1 for r in results if r["status"] == "passed")
    failed = sum(1 for r in results if r["status"] == "failed")
    skipped = sum(1 for r in results if r["status"] == "skipped")

    return {
        "run_time": run_time,
        "env": env,
        "env_name": ENVS.get(env, {}).get("name", env),
        "test_type": test_type,
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
        },
        "tests": [
            {
                "id": r["test_id"],
                "name": r["test_name"],
                "status": r["status"],
                "duration_ms": r.get("duration_ms", 0),
                "url": r.get("url", ""),
                "http_status": r.get("http_status", 0),
                "console_errors_count": len(r.get("console_errors", [])),
                "screenshot": r.get("screenshot", ""),
                "error": r.get("error_message", ""),
            }
            for r in results
        ],
    }


def _generate_html(run_time: str, env: str, test_type: str, results: list, alerts: list) -> str:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    env_name = ENVS.get(env, {}).get("name", env)
    passed = sum(1 for r in results if r["status"] == "passed")
    failed = sum(1 for r in results if r["status"] == "failed")
    skipped = sum(1 for r in results if r["status"] == "skipped")

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Web Test — {env_name} — {run_time}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 20px; background: #f5f5f5; color: #333; }}
  .header {{ background: #1a1a2e; color: #fff; padding: 20px 30px; border-radius: 8px; margin-bottom: 20px; }}
  .header h1 {{ margin: 0 0 8px; font-size: 22px; }}
  .header .meta {{ color: #aaa; font-size: 14px; }}
  .summary {{ display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }}
  .card {{ background: #fff; border-radius: 8px; padding: 16px 24px; box-shadow: 0 1px 3px rgba(0,0,0,.1); text-align: center; min-width: 120px; }}
  .card .num {{ font-size: 32px; font-weight: 700; }}
  .card .label {{ color: #666; font-size: 13px; margin-top: 4px; }}
  .alert {{ background: #fff3cd; border: 1px solid #ffc107; border-radius: 8px; padding: 12px 16px; margin-bottom: 20px; }}
  .test-card {{ background: #fff; border-radius: 8px; padding: 16px; margin-bottom: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.1); }}
  .test-card.passed {{ border-left: 4px solid #0cce6b; }}
  .test-card.failed {{ border-left: 4px solid #ff4e42; }}
  .test-card.skipped {{ border-left: 4px solid #ffa400; }}
  .test-title {{ font-weight: 600; font-size: 15px; margin-bottom: 8px; }}
  .test-meta {{ color: #666; font-size: 13px; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; color: #fff; }}
  .badge.passed {{ background: #0cce6b; }}
  .badge.failed {{ background: #ff4e42; }}
  .badge.skipped {{ background: #ffa400; }}
  .err {{ color: #ff4e42; }}
  .ok {{ color: #0cce6b; }}
  .screenshot {{ max-width: 100%; border-radius: 6px; margin-top: 8px; box-shadow: 0 2px 8px rgba(0,0,0,.15); }}
  details {{ margin-top: 8px; }}
  summary {{ cursor: pointer; color: #666; font-size: 13px; }}
  pre {{ background: #f4f4f4; padding: 8px; border-radius: 4px; font-size: 12px; overflow-x: auto; }}
</style></head><body>
<div class="header">
  <h1>🧪 Web E2E Test Report</h1>
  <div class="meta">{run_time} · {env_name} · {test_type}</div>
</div>"""

    if alerts:
        html += '<div class="alert">⚠️ <b>退化告警</b><br>' + '<br>'.join(alerts) + '</div>'

    html += f"""<div class="summary">
  <div class="card"><div class="num">{len(results)}</div><div class="label">Total</div></div>
  <div class="card"><div class="num ok">{passed}</div><div class="label">Passed</div></div>
  <div class="card"><div class="num err">{failed}</div><div class="label">Failed</div></div>
  <div class="card"><div class="num" style="color:#ffa400">{skipped}</div><div class="label">Skipped</div></div>
</div>"""

    for r in results:
        status = r["status"]
        html += f'<div class="test-card {status}">'
        html += f'<div class="test-title"><span class="badge {status}">{status.upper()}</span> {r["test_name"]}</div>'
        html += f'<div class="test-meta">'
        if r.get("duration_ms"):
            html += f'⏱ {r["duration_ms"]}ms · '
        if r.get("url"):
            html += f'🔗 {r["url"]} · '
        if r.get("http_status"):
            html += f'HTTP {r["http_status"]} · '
        ce = r.get("console_errors", [])
        if ce:
            html += f'<span class="err">⚠ {len(ce)} console error(s)</span>'
        html += '</div>'

        if r.get("error_message"):
            html += f'<div class="err" style="margin-top:8px;font-size:13px;">❌ {r["error_message"]}</div>'

        if ce:
            html += '<details><summary>Console Errors</summary><pre>'
            for e in ce:
                html += f'{e["type"]}: {e["text"]}\n'
            html += '</pre></details>'

        if r.get("screenshot") and Path(r["screenshot"]).exists():
            try:
                img_data = base64.b64encode(Path(r["screenshot"]).read_bytes()).decode()
                html += f'<img class="screenshot" src="data:image/png;base64,{img_data}" alt="screenshot">'
            except Exception:
                html += f'<div class="test-meta">📸 {r["screenshot"]}</div>'

        html += '</div>'

    html += '</body></html>'

    fname = f"web_test_{run_time.replace(' ', '_').replace(':', '-')}.html"
    path = REPORT_DIR / fname
    path.write_text(html, encoding="utf-8")
    (REPORT_DIR / "latest.html").write_text(html, encoding="utf-8")
    return str(path)


def _notify_wecom(chatid: str, message: str):
    import urllib.request as ur
    payload = json.dumps({"chatid": chatid, "content": message, "chat_type": 1}, ensure_ascii=False)
    req = ur.Request(f"http://127.0.0.1:{BRIDGE_PORT}/send", data=payload.encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with ur.urlopen(req, timeout=10):
            pass
    except Exception as e:
        print(f"通知失败: {e}", file=sys.stderr)


def cleanup_old_reports(keep_days=3):
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M:%S")
    for f in REPORT_DIR.glob("web_test_*.html"):
        stem = f.stem
        try:
            ts = stem[9:19] + " " + stem[20:].replace("-", ":")
            if ts < cutoff:
                f.unlink()
        except (ValueError, IndexError):
            continue
    for f in SCREENSHOT_DIR.glob("*.png"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if mtime < datetime.now() - timedelta(days=keep_days):
                f.unlink()
        except Exception:
            continue
    try:
        conn = _db()
        conn.execute("DELETE FROM test_results WHERE run_time < ?", (cutoff,))
        conn.execute("DELETE FROM test_runs WHERE run_time < ?", (cutoff,))
        conn.execute("VACUUM")
        conn.commit()
        conn.close()
    except Exception:
        pass


# ── CLI 入口 ──────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Web E2E 测试器")
    sub = parser.add_subparsers(dest="command", required=True)

    # health
    p_health = sub.add_parser("health", help="页面健康检查")
    p_health.add_argument("--url", help="指定 URL（不指定则检查默认页面）")
    p_health.add_argument("--env", default="uat", choices=["uat", "prod"])

    # smoke
    p_smoke = sub.add_parser("smoke", help="冒烟测试")
    p_smoke.add_argument("--env", default="uat", choices=["uat", "prod"])
    p_smoke.add_argument("--chatid", default="dm_Alan.Li", help="告警推送 chatid")

    # run
    p_run = sub.add_parser("run", help="自由操作模式")
    p_run.add_argument("--steps", required=True, help="JSON 步骤数组")
    p_run.add_argument("--env", default="uat", choices=["uat", "prod"])
    p_run.add_argument("--name", default="Custom Test", help="测试名称")

    # login
    p_login = sub.add_parser("login", help="交互式登录保存 cookie")
    p_login.add_argument("--env", default="uat", choices=["uat", "prod"])

    # report
    p_report = sub.add_parser("report", help="查看历史测试结果")
    p_report.add_argument("--limit", type=int, default=5)

    args = parser.parse_args()

    if args.command == "health":
        cmd_health(args)
    elif args.command == "smoke":
        cmd_smoke(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "login":
        cmd_login(args)
    elif args.command == "report":
        cmd_report(args)


if __name__ == "__main__":
    main()
