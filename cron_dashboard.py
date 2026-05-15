#!/usr/bin/env python3
"""
定时任务仪表盘 — 汇总 crontab + scheduler 所有任务及最新执行结果。

功能：
  1. 收集系统 crontab 中的所有定时任务
  2. 收集 scheduler（bridge）中的所有定时任务
  3. 读取各扫描/测试的最新结果（SQLite）
  4. 生成汇总消息推送到企微

用法：
  python3 cron_dashboard.py              # 生成报告并推送企微
  python3 cron_dashboard.py --json       # JSON 格式输出（供 API 调用）
  python3 cron_dashboard.py --no-push    # 只输出不推送

cron 示例（每天 9:00）：
  0 9 * * * /mnt/i/workspace/kiro-wecom-bridge/.venv/bin/python3 /mnt/i/workspace/kiro-wecom-bridge/cron_dashboard.py 2>&1
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime

# 配置
BRIDGE_PORT = int(os.getenv("PORT", "8900"))
WECOM_SEND_URL = f"http://localhost:{BRIDGE_PORT}/send"
WECOM_CHATID = "dm_Alan.Li"
SCHEDULER_DB = "/mnt/i/workspace/alan_bot/wecom-sessions/scheduler.db"
REPORTS_DIR = "/mnt/i/workspace/kiro-wecom-bridge/reports"

# crontab 任务描述映射（通过脚本名或注释标签识别）
CRON_TASK_LABELS = {
    "ada_scanner.py": "ADA 无障碍扫描",
    "cwv_monitor.py": "CWV 性能扫描",
    "web_tester.py": "Web E2E 测试",
    "git_pull_all.py": "代码自动同步",
    "tracking_scanner.py": "埋点扫描",
    "cron_dashboard.py": "定时任务日报",
}


def _parse_cron_schedule(cron_expr: str) -> str:
    """将 cron 表达式转为可读描述"""
    parts = cron_expr.split()
    if len(parts) < 5:
        return cron_expr
    minute, hour, dom, month, dow = parts[:5]
    time_str = f"{hour.zfill(2)}:{minute.zfill(2)}"
    if dom == "*" and month == "*" and dow == "*":
        return f"每天 {time_str}"
    if dow != "*":
        days = {"0": "日", "1": "一", "2": "二", "3": "三", "4": "四", "5": "五", "6": "六"}
        day_names = ",".join(days.get(d, d) for d in dow.split(","))
        return f"每周{day_names} {time_str}"
    return f"{cron_expr[:20]} ({time_str})"


def get_crontab_tasks() -> list[dict]:
    """解析系统 crontab 中的所有任务"""
    tasks = []
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        if result.returncode != 0 or not result.stdout.strip():
            return tasks
    except Exception:
        return tasks

    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # 提取 cron 表达式（前5个字段）
        match = re.match(r"^([\d\*\/\-,]+\s+[\d\*\/\-,]+\s+[\d\*\/\-,]+\s+[\d\*\/\-,]+\s+[\d\*\/\-,]+)\s+(.+)$", line)
        if not match:
            continue

        cron_expr = match.group(1)
        command = match.group(2)

        # 提取注释标签
        tag = ""
        tag_match = re.search(r"#\s*(.+)$", command)
        if tag_match:
            tag = tag_match.group(1).strip()

        # 识别任务名称
        label = tag
        if not label:
            for script_name, desc in CRON_TASK_LABELS.items():
                if script_name in command:
                    label = desc
                    break
        if not label:
            label = command[:50]

        # 判断是否是 kiro-scheduler 管理的（跳过，避免重复）
        if "kiro-scheduler:" in command:
            continue

        tasks.append({
            "type": "crontab",
            "schedule": _parse_cron_schedule(cron_expr),
            "cron": cron_expr,
            "label": label,
            "command": command.split("#")[0].strip()[:80],
        })

    return tasks


def get_scheduler_tasks() -> list[dict]:
    """从 scheduler SQLite 读取所有任务"""
    tasks = []
    if not os.path.exists(SCHEDULER_DB):
        return tasks

    try:
        conn = sqlite3.connect(SCHEDULER_DB)
        conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at").fetchall()
        conn.close()

        for row in rows:
            tasks.append({
                "type": "scheduler",
                "schedule": _parse_cron_schedule(row["cron"]),
                "cron": row["cron"],
                "label": row["description"] or row["prompt"][:40],
                "enabled": bool(row["enabled"]),
                "chatid": row["chatid"],
            })
    except Exception:
        pass

    return tasks


def get_ada_latest() -> dict | None:
    """获取 ADA 扫描最新结果"""
    db_path = os.path.join(REPORTS_DIR, "ada.db")
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT scan_time, SUM(files_scanned) as files, SUM(total_issues) as issues, "
            "SUM(errors) as errors, SUM(warnings) as warnings "
            "FROM scans WHERE scan_time = (SELECT MAX(scan_time) FROM scans)"
        ).fetchone()
        conn.close()
        if row and row["scan_time"]:
            return {
                "name": "ADA 无障碍扫描",
                "time": row["scan_time"],
                "files": row["files"] or 0,
                "issues": row["issues"] or 0,
                "errors": row["errors"] or 0,
                "warnings": row["warnings"] or 0,
            }
    except Exception:
        pass
    return None


def get_cwv_latest() -> dict | None:
    """获取 CWV 性能扫描最新结果"""
    db_path = os.path.join(REPORTS_DIR, "cwv.db")
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT scan_time, SUM(files_scanned) as files, SUM(total_issues) as issues, "
            "SUM(errors) as errors, SUM(warnings) as warnings "
            "FROM scans WHERE scan_time = (SELECT MAX(scan_time) FROM scans)"
        ).fetchone()
        conn.close()
        if row and row["scan_time"]:
            return {
                "name": "CWV 性能扫描",
                "time": row["scan_time"],
                "files": row["files"] or 0,
                "issues": row["issues"] or 0,
                "errors": row["errors"] or 0,
                "warnings": row["warnings"] or 0,
            }
    except Exception:
        pass
    return None


def get_web_test_latest() -> dict | None:
    """获取 Web E2E 测试最新结果"""
    db_path = os.path.join(REPORTS_DIR, "web_test.db")
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT run_time, env, test_type, total_tests, passed, failed "
            "FROM test_runs ORDER BY run_time DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            return {
                "name": f"Web E2E ({row['test_type']}@{row['env']})",
                "time": row["run_time"],
                "total": row["total_tests"] or 0,
                "passed": row["passed"] or 0,
                "failed": row["failed"] or 0,
            }
    except Exception:
        pass
    return None


def get_git_pull_latest() -> dict | None:
    """获取代码同步最新日志"""
    log_dir = "/mnt/i/workspace/kiro-wecom-bridge/logs"
    if not os.path.exists(log_dir):
        return None
    try:
        # 找最新的日志文件
        import glob
        logs = sorted(glob.glob(os.path.join(log_dir, "git_pull_*.log")), reverse=True)
        if not logs:
            return None
        # 读最后一行（总计行）
        with open(logs[0], "r") as f:
            lines = f.readlines()
        for line in reversed(lines):
            if "总计:" in line:
                return {
                    "name": "代码自动同步",
                    "time": os.path.basename(logs[0]).replace("git_pull_", "").replace(".log", ""),
                    "summary": line.strip(),
                }
    except Exception:
        pass
    return None


def build_report() -> dict:
    """构建完整报告数据"""
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "crontab_tasks": get_crontab_tasks(),
        "scheduler_tasks": get_scheduler_tasks(),
        "results": {
            "ada": get_ada_latest(),
            "cwv": get_cwv_latest(),
            "web_test": get_web_test_latest(),
            "git_pull": get_git_pull_latest(),
        },
    }


def format_markdown(report: dict) -> str:
    """将报告格式化为 Markdown 消息"""
    lines = [f"📋 **定时任务日报** {report['generated_at']}"]
    lines.append("")

    # 任务列表
    crontab_tasks = report["crontab_tasks"]
    scheduler_tasks = report["scheduler_tasks"]
    total = len(crontab_tasks) + len(scheduler_tasks)
    lines.append(f"**🕐 定时任务（{total} 个）**")

    if crontab_tasks:
        for t in crontab_tasks:
            lines.append(f"  · {t['schedule']} — {t['label']}")

    if scheduler_tasks:
        for t in scheduler_tasks:
            status = "✅" if t["enabled"] else "⏸️"
            lines.append(f"  · {status} {t['schedule']} — {t['label']}")

    # 最新结果
    lines.append("")
    lines.append("**📊 最新执行结果**")

    results = report["results"]

    # ADA
    ada = results.get("ada")
    if ada:
        emoji = "✅" if ada["errors"] == 0 else "⚠️"
        lines.append(f"  {emoji} {ada['name']}: {ada['files']}文件, {ada['issues']}问题 ({ada['errors']}错误/{ada['warnings']}警告) — {ada['time']}")
    else:
        lines.append("  ⏳ ADA 无障碍扫描: 暂无数据")

    # CWV
    cwv = results.get("cwv")
    if cwv:
        emoji = "✅" if cwv["errors"] == 0 else "⚠️"
        lines.append(f"  {emoji} {cwv['name']}: {cwv['files']}文件, {cwv['issues']}问题 ({cwv['errors']}错误/{cwv['warnings']}警告) — {cwv['time']}")
    else:
        lines.append("  ⏳ CWV 性能扫描: 暂无数据")

    # Web Test
    web_test = results.get("web_test")
    if web_test:
        emoji = "✅" if web_test["failed"] == 0 else "❌"
        lines.append(f"  {emoji} {web_test['name']}: {web_test['passed']}/{web_test['total']}通过, {web_test['failed']}失败 — {web_test['time']}")
    else:
        lines.append("  ⏳ Web E2E 测试: 暂无数据")

    # Git Pull
    git_pull = results.get("git_pull")
    if git_pull:
        lines.append(f"  📦 {git_pull['name']}: {git_pull['summary']} — {git_pull['time']}")
    else:
        lines.append("  ⏳ 代码同步: 暂无数据")

    return "\n".join(lines)


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


def main():
    json_output = "--json" in sys.argv
    no_push = "--no-push" in sys.argv

    report = build_report()

    if json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    msg = format_markdown(report)
    print(msg)

    if not no_push:
        if send_wecom(msg):
            print("\n✅ 已推送到企微")
        else:
            print("\n❌ 推送失败")


if __name__ == "__main__":
    main()
