#!/usr/bin/env python3
"""
埋点扫描器：定时扫描前端项目代码，提取埋点调用存入 SQLite。
为查询埋点技能提供预建索引，后续可直接查库而非实时 grep。

用法：
  python3 tracking_scanner.py           # 扫描所有项目
  python3 tracking_scanner.py --project ec-website-nb  # 只扫描指定项目
  python3 tracking_scanner.py --stats   # 查看统计信息

cron 示例（每天凌晨 3:00，在 git pull 之后）：
  0 3 * * * /mnt/i/workspace/kiro-wecom-bridge/.venv/bin/python3 /mnt/i/workspace/kiro-wecom-bridge/tracking_scanner.py 2>&1
"""

import subprocess
import sys
import os
import re
import json
import sqlite3
import glob
from datetime import datetime
from pathlib import Path

# ============================================================
# 项目配置：与查询埋点技能的平台搜索策略表保持一致
# ============================================================

PROJECTS = [
    # PC 端
    {
        "name": "ec-website-nb",
        "platform": "PC",
        "tech": "PHP/Laravel",
        "path": "/mnt/c/Alan/workspace/ec-website-nb",
        "keywords": ["pageSceneHandler", "sensors", "yamidata", "dataLayer"],
        "includes": ["*.php", "*.blade.php", "*.js"],
        "excludes": ["vendor", "node_modules", ".git", "storage", "bootstrap"],
    },
    {
        "name": "ec-website-next",
        "platform": "PC",
        "tech": "Next.js",
        "path": "/mnt/c/Alan/workspace/ec-website-next",
        "keywords": ["track", "analytics", "gtag", "dataLayer"],
        "includes": ["*.tsx", "*.ts", "*.js", "*.jsx"],
        "excludes": ["node_modules", ".next", ".git", "dist"],
    },
    {
        "name": "ec-website-customer-nb",
        "platform": "PC",
        "tech": "PHP/Laravel",
        "path": "/mnt/c/Alan/workspace/ec-website-customer-nb",
        "keywords": ["pageSceneHandler", "sensors", "yamidata", "dataLayer"],
        "includes": ["*.php", "*.blade.php", "*.js"],
        "excludes": ["vendor", "node_modules", ".git", "storage", "bootstrap"],
    },
    {
        "name": "ec-website-customer-next",
        "platform": "PC",
        "tech": "Next.js",
        "path": "/mnt/c/Alan/workspace/ec-website-customer-next",
        "keywords": ["track", "analytics", "gtag", "dataLayer"],
        "includes": ["*.tsx", "*.ts", "*.js", "*.jsx"],
        "excludes": ["node_modules", ".next", ".git", "dist"],
    },
    {
        "name": "ec-website-trade-nb",
        "platform": "PC",
        "tech": "PHP/Laravel",
        "path": "/mnt/c/Alan/workspace/ec-website-trade-nb",
        "keywords": ["pageSceneHandler", "sensors", "yamidata", "dataLayer"],
        "includes": ["*.php", "*.blade.php", "*.js"],
        "excludes": ["vendor", "node_modules", ".git", "storage", "bootstrap"],
    },
    # H5 端
    {
        "name": "ec-mobilesite-nb",
        "platform": "H5",
        "tech": "Nuxt.js",
        "path": "/mnt/c/Alan/workspace/ec-mobilesite-nb",
        "keywords": ["\\$track", "sensors", "yamidata"],
        "includes": ["*.vue", "*.js", "*.ts"],
        "excludes": ["node_modules", ".nuxt", ".output", ".git", "dist"],
    },
    {
        "name": "ec-mobilesite-ssr",
        "platform": "H5",
        "tech": "Nuxt.js",
        "path": "/mnt/c/Alan/workspace/ec-mobilesite-ssr",
        "keywords": ["\\$track", "sensors", "yamidata"],
        "includes": ["*.vue", "*.js", "*.ts"],
        "excludes": ["node_modules", ".nuxt", ".output", ".git", "dist"],
    },
    {
        "name": "ec-mobilesite-rma",
        "platform": "H5",
        "tech": "Vue",
        "path": "/mnt/c/Alan/workspace/ec-mobilesite-rma",
        "keywords": ["sensors", "yamidata"],
        "includes": ["*.vue", "*.js"],
        "excludes": ["node_modules", ".git", "dist"],
    },
    # APP 端
    {
        "name": "mobile_flutter",
        "platform": "APP",
        "tech": "Flutter",
        "path": "/mnt/c/Alan/workspace/mobile_flutter",
        "search_path": "/mnt/c/Alan/workspace/mobile_flutter/lib",
        "keywords": ["track", "tracker", "TrackEvent"],
        "includes": ["*.dart"],
        "excludes": ["build", ".dart_tool", ".git", ".ios", ".android"],
    },
    {
        "name": "mobile_android",
        "platform": "APP(原生)",
        "tech": "Java/Android",
        "path": "/mnt/i/workspace/mobile_android",
        "search_path": "/mnt/i/workspace/mobile_android/linden/src/main/java",
        "keywords": ["Analyst", "sensors", "track", "AnalyticsEventNameConst"],
        "includes": ["*.java"],
        "excludes": ["build", ".idea", ".git", ".gradle", ".scannerwork", "gpu_lib"],
    },
    {
        "name": "mobile_ios",
        "platform": "APP(原生)",
        "tech": "Swift/iOS",
        "path": "/mnt/i/workspace/mobile_ios",
        "search_path": "/mnt/i/workspace/mobile_ios/Yamibuy",
        "keywords": ["track", "sensors", "analytics", "YMBAnalytics"],
        "includes": ["*.swift", "*.m", "*.h"],
        "excludes": ["Pods", ".git", "build", "sonar-reports", "fastlane",
                     "YamibuyTests", "YamibuyUITests"],
    },
]

# 埋点平台识别规则
TRACKING_PLATFORMS = {
    "sensors": "神策",
    "$track": "神策",
    "yamidata": "亚米",
    "dataLayer": "星辰",
    "gtag": "星辰",
    "analytics": "星辰",
}

# 事件名提取正则（覆盖各平台调用模式）
EVENT_PATTERNS = [
    # sensors.track('event_name', {...})
    re.compile(r"""(?:sensors|sa)\.track\s*\(\s*['"]([^'"]+)['"]"""),
    # this.$track('event_name', {...})
    re.compile(r"""\$track\s*\(\s*['"]([^'"]+)['"]"""),
    # track('event_name', {...}) — 但排除 import/require 等
    re.compile(r"""(?<!\w)track\s*\(\s*['"]([^'"]+)['"]"""),
    # yamidata.push({event: 'event_name'})
    re.compile(r"""yamidata\.push\s*\(\s*\{[^}]*event\s*:\s*['"]([^'"]+)['"]"""),
    # dataLayer.push({event: 'event_name'})
    re.compile(r"""dataLayer\.push\s*\(\s*\{[^}]*event\s*:\s*['"]([^'"]+)['"]"""),
    # gtag('event', 'event_name')
    re.compile(r"""gtag\s*\(\s*['"]event['"]\s*,\s*['"]([^'"]+)['"]"""),
    # TrackEvent('event_name', {...})
    re.compile(r"""TrackEvent\s*\(\s*['"]([^'"]+)['"]"""),
    # Analyst.track('event_name')
    re.compile(r"""Analyst\w*\.track\s*\(\s*['"]([^'"]+)['"]"""),
    # Flutter 常量定义: static const String event_xxx = 'event_xxx';
    re.compile(r"""static\s+const\s+String\s+(\w+)\s*=\s*['"]([^'"]+)['"]"""),
    # Flutter 常量引用: eventName: SensorTrackEvent.event_xxx
    re.compile(r"""eventName\s*:\s*\w+TrackEvent\.(\w+)"""),
    # pageSceneHandler:场景名（PHP 路由中间件）
    re.compile(r"""pageSceneHandler[:\s]+['"]?(\w+)['"]?"""),
]

# 属性提取正则（从传参对象中提取 key）
ATTR_PATTERNS = [
    # JS/TS: key: value 或 'key': value
    re.compile(r"""['"]?(\w+)['"]?\s*:\s*"""),
]

# DB 配置
DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
DB_PATH = os.path.join(DB_DIR, "tracking.db")
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


def init_db():
    """初始化 SQLite 数据库"""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 扫描记录表
    c.execute("""CREATE TABLE IF NOT EXISTS scans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_time TEXT NOT NULL,
        project TEXT NOT NULL,
        platform TEXT NOT NULL,
        tech TEXT NOT NULL,
        total_events INTEGER DEFAULT 0,
        total_files INTEGER DEFAULT 0
    )""")

    # 埋点事件表（核心表，对应查询技能的输出字段）
    c.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_time TEXT NOT NULL,
        project TEXT NOT NULL,
        platform TEXT NOT NULL,
        file_path TEXT NOT NULL,
        line_number INTEGER NOT NULL,
        event_name TEXT NOT NULL,
        tracking_platform TEXT,
        context_before TEXT,
        context_after TEXT,
        raw_code TEXT NOT NULL,
        function_name TEXT,
        component_name TEXT
    )""")

    # 事件属性表
    c.execute("""CREATE TABLE IF NOT EXISTS event_attrs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER NOT NULL,
        attr_name TEXT NOT NULL,
        attr_value TEXT,
        attr_type TEXT,
        FOREIGN KEY (event_id) REFERENCES events(id)
    )""")

    # 索引：加速按项目、平台、事件名查询
    c.execute("CREATE INDEX IF NOT EXISTS idx_events_project ON events(project)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_events_platform ON events(platform)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_events_event_name ON events(event_name)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_events_scan_time ON events(scan_time)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_events_file_path ON events(file_path)")

    conn.commit()
    return conn


def grep_project(project: dict) -> list[dict]:
    """用 grep 扫描项目，返回匹配结果列表"""
    search_path = project.get("search_path", project["path"])
    if not os.path.exists(search_path):
        return []

    keywords = "\\|".join(project["keywords"])
    includes = " ".join(f'--include="{i}"' for i in project["includes"])
    excludes = " ".join(f"--exclude-dir={e}" for e in project["excludes"])

    cmd = f'grep -rn "{keywords}" {search_path}/ {includes} {excludes} -A 5 -B 2 2>/dev/null'

    try:
        result = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode not in (0, 1):  # 1 = no match
            return []
        return parse_grep_output(result.stdout, project)
    except subprocess.TimeoutExpired:
        print(f"  ⚠️ {project['name']} grep 超时")
        return []
    except Exception as e:
        print(f"  ❌ {project['name']} grep 异常: {e}")
        return []


def parse_grep_output(output: str, project: dict) -> list[dict]:
    """解析 grep -A 5 -B 2 输出，提取埋点事件"""
    if not output.strip():
        return []

    events = []
    current_block = []
    current_file = None
    current_line = None

    for line in output.split("\n"):
        # 分隔符 --
        if line == "--":
            if current_block and current_file:
                parsed = parse_block(current_block, current_file, current_line, project)
                if parsed:
                    events.extend(parsed)
            current_block = []
            current_file = None
            current_line = None
            continue

        # 匹配行: file:line:content 或上下文行: file-line-content
        m = re.match(r'^(.+?)[:\-](\d+)[:\-](.*)$', line)
        if m:
            filepath, lineno, content = m.group(1), int(m.group(2)), m.group(3)
            if current_file is None:
                current_file = filepath
                current_line = lineno
            current_block.append({
                "file": filepath,
                "line": lineno,
                "content": content,
                "is_match": ":" in line[:len(filepath)+10],
            })

    # 处理最后一个 block
    if current_block and current_file:
        parsed = parse_block(current_block, current_file, current_line, project)
        if parsed:
            events.extend(parsed)

    return events


def parse_block(block: list[dict], filepath: str, base_line: int, project: dict) -> list[dict]:
    """从一个 grep block 中提取埋点事件"""
    events = []
    full_code = "\n".join(b["content"] for b in block)

    # 提取事件名
    for pattern in EVENT_PATTERNS:
        for m in pattern.finditer(full_code):
            # Flutter 常量定义有两个 group：变量名和字符串值，取字符串值
            if m.lastindex and m.lastindex >= 2:
                event_name = m.group(2)
            else:
                event_name = m.group(1)
            # 过滤掉明显不是事件名的匹配（如 'true', 'false', 'function' 等）
            if event_name in ("true", "false", "null", "undefined", "function",
                              "return", "const", "let", "var", "if", "else"):
                continue
            if len(event_name) < 3 or len(event_name) > 80:
                continue

            # 识别埋点平台
            platforms = set()
            for keyword, platform in TRACKING_PLATFORMS.items():
                if keyword in full_code:
                    platforms.add(platform)
            tracking_platform = "&".join(sorted(platforms)) if platforms else "待确认"

            # 提取属性
            attrs = extract_attrs(full_code, m.end())

            # 提取函数名/组件名
            func_name = extract_function_name(block)
            comp_name = extract_component_name(filepath)

            # 上下文
            match_lines = [b for b in block if b.get("is_match")]
            context_before = "\n".join(b["content"] for b in block[:2])
            context_after = "\n".join(b["content"] for b in block[-3:])

            # 相对路径
            rel_path = filepath
            base = project.get("search_path", project["path"])
            if filepath.startswith(base):
                rel_path = filepath[len(base):].lstrip("/")

            events.append({
                "project": project["name"],
                "platform": project["platform"],
                "file_path": rel_path,
                "line_number": match_lines[0]["line"] if match_lines else base_line,
                "event_name": event_name,
                "tracking_platform": tracking_platform,
                "context_before": context_before[:500],
                "context_after": context_after[:500],
                "raw_code": full_code[:1000],
                "function_name": func_name,
                "component_name": comp_name,
                "attrs": attrs,
            })
            break  # 每个 block 只取第一个事件名

    return events


def extract_attrs(code: str, start_pos: int) -> list[dict]:
    """从事件调用后的代码中提取属性"""
    attrs = []
    # 找到传参对象的开始 {
    brace_start = code.find("{", start_pos)
    if brace_start == -1:
        return attrs

    # 简单提取 key: value 对
    remaining = code[brace_start:]
    for m in re.finditer(r"""['"]?(\w+)['"]?\s*:\s*(['"][^'"]*['"]|\d+(?:\.\d+)?|true|false|\[.*?\]|\w+)""",
                         remaining):
        attr_name = m.group(1)
        attr_value = m.group(2).strip("'\"")

        # 过滤掉 JS 关键字和非属性名
        if attr_name in ("event", "type", "msgtype", "method", "url", "headers",
                         "data", "params", "config", "options", "callback"):
            continue

        # 推断类型
        raw_value = m.group(2)
        if raw_value in ("true", "false"):
            attr_type = "BOOLEAN"
        elif re.match(r'^\d+(\.\d+)?$', raw_value):
            attr_type = "NUMBER"
        elif raw_value.startswith("["):
            attr_type = "ARRAY"
        elif raw_value.startswith("'") or raw_value.startswith('"'):
            attr_type = "STRING"
        else:
            attr_type = None  # 变量引用，类型待确认

        attrs.append({
            "attr_name": attr_name,
            "attr_value": attr_value[:200],
            "attr_type": attr_type,
        })

    return attrs


def extract_function_name(block: list[dict]) -> str | None:
    """从代码块上下文中提取函数名"""
    for b in block:
        content = b["content"]
        # JS/TS: function xxx() / const xxx = / xxx() { / async xxx()
        m = re.search(r'(?:function|const|let|var|async)\s+(\w+)', content)
        if m:
            return m.group(1)
        # Dart: void xxx() / Future<void> xxx()
        m = re.search(r'(?:void|Future|static)\s+(\w+)\s*\(', content)
        if m:
            return m.group(1)
        # Swift: func xxx()
        m = re.search(r'func\s+(\w+)', content)
        if m:
            return m.group(1)
    return None


def extract_component_name(filepath: str) -> str | None:
    """从文件路径中提取组件名"""
    basename = os.path.basename(filepath)
    name = os.path.splitext(basename)[0]
    if name in ("index", "main", "app"):
        # 用父目录名
        parent = os.path.basename(os.path.dirname(filepath))
        return parent if parent else name
    return name


def save_events(conn: sqlite3.Connection, scan_time: str, project: dict, events: list[dict]):
    """保存扫描结果到数据库"""
    c = conn.cursor()

    # 删除该项目的旧数据（只保留最新一次扫描）
    c.execute("DELETE FROM event_attrs WHERE event_id IN (SELECT id FROM events WHERE project = ?)",
              (project["name"],))
    c.execute("DELETE FROM events WHERE project = ?", (project["name"],))
    c.execute("DELETE FROM scans WHERE project = ?", (project["name"],))

    # 统计
    files = set(e["file_path"] for e in events)

    # 写入扫描记录
    c.execute("""INSERT INTO scans (scan_time, project, platform, tech, total_events, total_files)
                 VALUES (?, ?, ?, ?, ?, ?)""",
              (scan_time, project["name"], project["platform"], project["tech"],
               len(events), len(files)))

    # 写入事件和属性
    for event in events:
        c.execute("""INSERT INTO events
                     (scan_time, project, platform, file_path, line_number, event_name,
                      tracking_platform, context_before, context_after, raw_code,
                      function_name, component_name)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (scan_time, event["project"], event["platform"], event["file_path"],
                   event["line_number"], event["event_name"], event["tracking_platform"],
                   event["context_before"], event["context_after"], event["raw_code"],
                   event["function_name"], event["component_name"]))
        event_id = c.lastrowid

        for attr in event.get("attrs", []):
            c.execute("""INSERT INTO event_attrs (event_id, attr_name, attr_value, attr_type)
                         VALUES (?, ?, ?, ?)""",
                      (event_id, attr["attr_name"], attr["attr_value"], attr["attr_type"]))

    conn.commit()


def print_stats(conn: sqlite3.Connection):
    """打印统计信息"""
    c = conn.cursor()

    print("📊 埋点库统计")
    print(f"{'='*60}")

    # 按项目统计
    c.execute("""SELECT project, platform, total_events, total_files, scan_time
                 FROM scans ORDER BY platform, project""")
    rows = c.fetchall()
    if not rows:
        print("  暂无数据")
        return

    print(f"\n{'项目':<35} {'平台':<10} {'事件数':<8} {'文件数':<8} {'扫描时间'}")
    print(f"{'-'*35} {'-'*10} {'-'*8} {'-'*8} {'-'*20}")
    total_events = 0
    total_files = 0
    for row in rows:
        print(f"{row[0]:<35} {row[1]:<10} {row[2]:<8} {row[3]:<8} {row[4]}")
        total_events += row[2]
        total_files += row[3]

    print(f"\n总计: {len(rows)} 个项目 | {total_events} 个事件 | {total_files} 个文件")

    # 按事件名统计 Top 20
    c.execute("""SELECT event_name, COUNT(*) as cnt, GROUP_CONCAT(DISTINCT platform) as platforms
                 FROM events GROUP BY event_name ORDER BY cnt DESC LIMIT 20""")
    top_events = c.fetchall()
    if top_events:
        print(f"\n🔥 Top 20 事件名:")
        for row in top_events:
            print(f"  · {row[0]} ({row[1]}次) [{row[2]}]")


def main():
    """主函数"""
    stats_only = "--stats" in sys.argv
    target_project = None
    if "--project" in sys.argv:
        idx = sys.argv.index("--project")
        if idx + 1 < len(sys.argv):
            target_project = sys.argv[idx + 1]

    conn = init_db()

    if stats_only:
        print_stats(conn)
        conn.close()
        return

    scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"📦 埋点扫描 — {scan_time}")
    print(f"{'='*60}")

    projects_to_scan = PROJECTS
    if target_project:
        projects_to_scan = [p for p in PROJECTS if p["name"] == target_project]
        if not projects_to_scan:
            print(f"❌ 未找到项目: {target_project}")
            conn.close()
            return

    total_events = 0
    for project in projects_to_scan:
        if not os.path.exists(project["path"]):
            print(f"  ⏭️ {project['name']} — 目录不存在")
            continue

        print(f"  🔍 扫描 {project['name']} ({project['platform']})...", end=" ", flush=True)
        events = grep_project(project)

        # 去重：同一文件同一行同一事件名只保留一条
        seen = set()
        unique_events = []
        for e in events:
            key = (e["file_path"], e["line_number"], e["event_name"])
            if key not in seen:
                seen.add(key)
                unique_events.append(e)

        save_events(conn, scan_time, project, unique_events)
        print(f"✅ {len(unique_events)} 个事件")
        total_events += len(unique_events)

    print(f"\n{'='*60}")
    print(f"总计: {total_events} 个事件")

    # 打印统计
    print()
    print_stats(conn)
    conn.close()


if __name__ == "__main__":
    main()
