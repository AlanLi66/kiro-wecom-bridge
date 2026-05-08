#!/usr/bin/env python3
"""Core Web Vitals 代码扫描器 — 扫描本地前端代码中影响 CWV 的模式
检查 img 缺 width/height(CLS)、缺 lazy loading(LCP)、render-blocking 资源、
大型内联脚本、缺 font-display 等。结果存 SQLite + 生成 HTML 报告。

用法: python3 cwv_monitor.py [--chatid CHATID]
"""
import json, os, re, sys, sqlite3, glob
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

BRIDGE_DIR = Path(__file__).resolve().parent
REPORT_DIR = BRIDGE_DIR / "reports" / "cwv"
DB_PATH = BRIDGE_DIR / "reports" / "cwv.db"
BRIDGE_PORT = int(os.getenv("PORT", "8900"))

PROJECTS = [
    ("ec-website-nb", "blade", "resources/views", "*.blade.php"),
    ("ec-website-next", "react", "src", "*.tsx"),
    ("ec-website-customer-nb", "blade", "resources/views", "*.blade.php"),
    ("ec-website-customer-next", "react", "src", "*.tsx"),
    ("ec-website-trade-nb", "blade", "resources/views", "*.blade.php"),
    ("ec-mobilesite-nb", "vue", "pages", "*.vue"),
    ("ec-mobilesite-ssr", "vue", "pages", "*.vue"),
    ("ec-mobilesite-rma", "vue", "src", "*.vue"),
]
BASE_DIR = "/mnt/c/Alan/workspace"
EXCLUDE_DIRS = {"node_modules", ".next", ".nuxt", ".output", "vendor", "dist",
                "build", ".git", "__pycache__", "storage", "bootstrap"}


class Rule:
    def __init__(self, rid, name, cwv_metric, severity, desc):
        self.id = rid
        self.name = name
        self.cwv_metric = cwv_metric  # LCP / CLS / INP / FID / TTFB / general
        self.severity = severity      # error / warning
        self.description = desc

RULES = [
    Rule("img-no-dimensions", "img 缺 width/height", "CLS", "error",
         "img 没有 width/height 属性会导致布局偏移(CLS)"),
    Rule("img-no-lazy", "首屏外 img 缺 loading=lazy", "LCP", "warning",
         "非首屏图片应加 loading=\"lazy\" 延迟加载，减少首屏资源竞争"),
    Rule("img-eager-no-priority", "首屏 img 缺 fetchpriority=high", "LCP", "warning",
         "首屏关键图片(hero/banner)应加 fetchpriority=\"high\" 提升 LCP"),
    Rule("render-blocking-css", "同步加载外部 CSS", "LCP", "warning",
         "外部 CSS 默认 render-blocking，考虑 critical CSS 内联或 preload"),
    Rule("sync-script", "同步加载外部 JS（无 async/defer）", "LCP", "error",
         "外部 script 缺少 async/defer 会阻塞渲染"),
    Rule("large-inline-script", "大型内联脚本(>5KB)", "INP", "warning",
         "大型内联脚本会阻塞主线程，影响 INP/TBT"),
    Rule("no-font-display", "@font-face 缺 font-display", "CLS", "warning",
         "@font-face 应设置 font-display: swap/optional 避免 FOIT 导致 CLS"),
    Rule("document-write", "使用 document.write()", "LCP", "error",
         "document.write() 会阻塞解析，严重影响性能"),
    Rule("unoptimized-image-format", "使用非优化图片格式", "LCP", "warning",
         "建议使用 WebP/AVIF 替代 PNG/JPG/GIF 减少传输体积"),
    Rule("layout-thrashing", "可能的强制同步布局", "INP", "warning",
         "在循环中读写 DOM 布局属性(offsetHeight/getBoundingClientRect)可能导致布局抖动"),
]


# ── 扫描逻辑 ──────────────────────────────────
def _find_files(project_dir, template_dir, pattern):
    search_path = os.path.join(project_dir, template_dir)
    if not os.path.isdir(search_path):
        return []
    results = []
    for root, dirs, files in os.walk(search_path):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            if glob.fnmatch.fnmatch(f, pattern):
                results.append(os.path.join(root, f))
    return results


def _read_file(path):
    for enc in ("utf-8", "latin-1"):
        try:
            return open(path, encoding=enc).read()
        except UnicodeDecodeError:
            continue
    return ""


def _scan_file(filepath, tech):
    content = _read_file(filepath)
    if not content:
        return []
    lines = content.split("\n")
    issues = []

    def _add(rule_id, line_num, context=""):
        issues.append({"rule": rule_id, "line": line_num, "context": context[:120]})

    for i, line in enumerate(lines, 1):
        # img-no-dimensions: <img> 缺 width 或 height
        for m in re.finditer(r'<img\b[^>]*?(?:/\s*>|>)', line, re.IGNORECASE):
            tag = m.group()
            has_w = bool(re.search(r'\bwidth\s*=', tag, re.IGNORECASE))
            has_h = bool(re.search(r'\bheight\s*=', tag, re.IGNORECASE))
            if not (has_w and has_h):
                # Next.js Image 组件用 fill 模式不需要 w/h
                if tech == "react" and 'fill' in tag:
                    continue
                _add("img-no-dimensions", i, tag)
            # img-no-lazy
            if 'loading' not in tag.lower():
                _add("img-no-lazy", i, tag)

        # Next.js <Image 组件
        if tech == "react":
            for m in re.finditer(r'<Image\b[^>]*?(?:/\s*>|>)', line):
                tag = m.group()
                has_w = 'width' in tag or 'fill' in tag
                has_h = 'height' in tag or 'fill' in tag
                if not (has_w and has_h):
                    _add("img-no-dimensions", i, tag)

        # sync-script: <script src="..." 无 async/defer
        for m in re.finditer(r'<script\b[^>]*src\s*=[^>]*>', line, re.IGNORECASE):
            tag = m.group()
            if 'async' not in tag.lower() and 'defer' not in tag.lower():
                # 排除 type=module（自带 defer 语义）
                if 'type="module"' not in tag.lower() and "type='module'" not in tag.lower():
                    _add("sync-script", i, tag)

        # render-blocking-css: <link rel="stylesheet" 无 media/preload
        for m in re.finditer(r'<link\b[^>]*rel\s*=\s*["\']stylesheet["\'][^>]*>', line, re.IGNORECASE):
            tag = m.group()
            if 'media=' not in tag.lower() and 'preload' not in tag.lower():
                _add("render-blocking-css", i, tag)

        # document.write
        if 'document.write(' in line or 'document.write (' in line:
            _add("document-write", i, line.strip())

        # no-font-display: @font-face 块
        if '@font-face' in line.lower():
            # 检查后续几行有没有 font-display
            block = "\n".join(lines[i-1:min(i+10, len(lines))])
            if 'font-display' not in block.lower():
                _add("no-font-display", i, "@font-face without font-display")

        # unoptimized-image-format: 硬编码的 .png/.jpg/.gif URL
        for m in re.finditer(r'["\']https?://[^"\']*\.(png|jpg|jpeg|gif)["\']', line, re.IGNORECASE):
            url = m.group()
            # 排除 CDN 已经做了格式转换的（如 ?format=webp）
            if 'format=' not in url.lower() and 'webp' not in url.lower():
                _add("unoptimized-image-format", i, url[:120])

        # layout-thrashing: 循环中的 offsetHeight 等
        if re.search(r'\b(offsetHeight|offsetWidth|clientHeight|clientWidth|getBoundingClientRect)\b', line):
            # 检查是否在循环内（简单启发式：上下文有 for/while/forEach）
            ctx = "\n".join(lines[max(0, i-5):i])
            if re.search(r'\b(for|while|forEach|map)\b', ctx):
                _add("layout-thrashing", i, line.strip())

    # large-inline-script: 大型内联 <script> 块
    for m in re.finditer(r'<script\b[^>]*>(.*?)</script>', content, re.DOTALL | re.IGNORECASE):
        tag_attrs = re.search(r'<script\b([^>]*)>', m.group(), re.IGNORECASE).group(1)
        if 'src' in tag_attrs.lower():
            continue  # 外部脚本，跳过
        body = m.group(1)
        if len(body) > 5000:
            line_num = content[:m.start()].count("\n") + 1
            _add("large-inline-script", line_num, f"inline script {len(body)} bytes")

    return issues


# ── 主扫描 + DB ───────────────────────────────
def scan_all():
    results = {}
    for proj_name, tech, tpl_dir, pattern in PROJECTS:
        proj_dir = os.path.join(BASE_DIR, proj_name)
        if not os.path.isdir(proj_dir):
            results[proj_name] = {"tech": tech, "files": 0, "issues": [], "error": "目录不存在"}
            continue
        files = _find_files(proj_dir, tpl_dir, pattern)
        if tech == "vue" and tpl_dir == "pages":
            files += _find_files(proj_dir, "components", pattern)
        # Blade 项目也扫 public 下的 JS/CSS
        if tech == "blade":
            files += _find_files(proj_dir, "resources/js", "*.js")
            files += _find_files(proj_dir, "resources/css", "*.css")
        all_issues = []
        for f in files:
            fi = _scan_file(f, tech)
            rel = os.path.relpath(f, proj_dir)
            for issue in fi:
                issue["file"] = rel
            all_issues.extend(fi)
        results[proj_name] = {"tech": tech, "files": len(files), "issues": all_issues}
    return results


def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_time TEXT NOT NULL, project TEXT NOT NULL,
            files_scanned INTEGER DEFAULT 0, total_issues INTEGER DEFAULT 0,
            errors INTEGER DEFAULT 0, warnings INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_time TEXT NOT NULL, project TEXT NOT NULL,
            file TEXT NOT NULL, line INTEGER, rule TEXT NOT NULL, context TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_cwv_time ON scans(scan_time);
    """)
    return conn


def save_to_db(results):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rule_map = {r.id: r for r in RULES}
    conn = _db()
    for proj, data in results.items():
        issues = data.get("issues", [])
        errs = sum(1 for i in issues if rule_map.get(i["rule"], RULES[0]).severity == "error")
        conn.execute("INSERT INTO scans VALUES (NULL,?,?,?,?,?,?)",
                     (now, proj, data.get("files", 0), len(issues), errs, len(issues) - errs))
        for i in issues:
            conn.execute("INSERT INTO issues VALUES (NULL,?,?,?,?,?,?)",
                         (now, proj, i["file"], i["line"], i["rule"], i.get("context", "")))
    conn.commit()
    conn.close()
    return now


def check_regression(results):
    alerts = []
    conn = _db()
    times = conn.execute("SELECT DISTINCT scan_time FROM scans ORDER BY scan_time DESC LIMIT 2").fetchall()
    if len(times) < 2:
        conn.close()
        return alerts
    prev_time = times[1]["scan_time"]
    for proj, data in results.items():
        prev = conn.execute("SELECT total_issues FROM scans WHERE scan_time=? AND project=?",
                            (prev_time, proj)).fetchone()
        if prev:
            diff = len(data.get("issues", [])) - prev["total_issues"]
            if diff > 5:
                alerts.append(f"⚠️ {proj}: {prev['total_issues']} → {len(data['issues'])} (+{diff})")
    conn.close()
    return alerts


# ── HTML 报告 ─────────────────────────────────
def _metric_color(metric):
    colors = {"LCP": "#e74c3c", "CLS": "#e67e22", "INP": "#9b59b6", "FID": "#9b59b6",
              "TTFB": "#3498db", "general": "#95a5a6"}
    return colors.get(metric, "#666")

def generate_html(results, scan_time, alerts):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    rule_map = {r.id: r for r in RULES}
    total_files = sum(d.get("files", 0) for d in results.values())
    total_issues = sum(len(d.get("issues", [])) for d in results.values())
    total_errors = sum(1 for d in results.values() for i in d.get("issues", [])
                       if rule_map.get(i["rule"], RULES[0]).severity == "error")

    # 按 CWV 指标分组统计
    metric_counts = defaultdict(int)
    for d in results.values():
        for i in d.get("issues", []):
            r = rule_map.get(i["rule"])
            if r:
                metric_counts[r.cwv_metric] += 1

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>CWV Scan — {scan_time}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 20px; background: #f5f5f5; }}
  .header {{ background: #0f3460; color: #fff; padding: 20px 30px; border-radius: 8px; margin-bottom: 20px; }}
  .header h1 {{ margin: 0 0 8px; font-size: 22px; }}
  .header .meta {{ color: #aaa; font-size: 14px; }}
  .summary {{ display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }}
  .card {{ background: #fff; border-radius: 8px; padding: 16px 24px; box-shadow: 0 1px 3px rgba(0,0,0,.1); text-align: center; min-width: 100px; }}
  .card .num {{ font-size: 28px; font-weight: 700; }}
  .card .label {{ color: #666; font-size: 12px; margin-top: 4px; }}
  .alert {{ background: #fff3cd; border: 1px solid #ffc107; border-radius: 8px; padding: 12px 16px; margin-bottom: 20px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.1); margin-bottom: 20px; }}
  th {{ background: #16213e; color: #fff; padding: 10px 12px; text-align: left; font-size: 13px; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid #eee; font-size: 13px; }}
  tr:hover {{ background: #f8f9fa; }}
  .err {{ color: #ff4e42; font-weight: 600; }}
  .warn {{ color: #ffa400; font-weight: 600; }}
  .metric-tag {{ display: inline-block; padding: 2px 8px; border-radius: 10px; color: #fff; font-size: 11px; font-weight: 600; }}
  .code {{ font-family: monospace; font-size: 12px; background: #f4f4f4; padding: 2px 6px; border-radius: 3px; word-break: break-all; }}
  details {{ margin-bottom: 8px; }}
  summary {{ cursor: pointer; font-weight: 600; padding: 8px; background: #fff; border-radius: 6px; box-shadow: 0 1px 2px rgba(0,0,0,.08); }}
</style></head><body>
<div class="header">
  <h1>📊 Core Web Vitals Code Scan</h1>
  <div class="meta">{scan_time} · {len(results)} projects · {total_files} files</div>
</div>"""

    if alerts:
        html += '<div class="alert">⚠️ <b>退化告警</b><br>' + '<br>'.join(alerts) + '</div>'

    html += '<div class="summary">'
    html += f'<div class="card"><div class="num">{total_issues}</div><div class="label">总问题</div></div>'
    html += f'<div class="card"><div class="num err">{total_errors}</div><div class="label">Errors</div></div>'
    for metric in ["CLS", "LCP", "INP"]:
        c = metric_counts.get(metric, 0)
        html += f'<div class="card"><div class="num" style="color:{_metric_color(metric)}">{c}</div><div class="label">{metric} 相关</div></div>'
    html += '</div>'

    # 项目概览
    html += '<table><tr><th>项目</th><th>技术栈</th><th>文件</th><th>Errors</th><th>Warnings</th><th>总计</th></tr>'
    for proj, data in sorted(results.items()):
        issues = data.get("issues", [])
        errs = sum(1 for i in issues if rule_map.get(i["rule"], RULES[0]).severity == "error")
        ec = 'err' if errs > 0 else ''
        html += f'<tr><td>{proj}</td><td>{data["tech"]}</td><td>{data.get("files", 0)}</td>'
        html += f'<td class="{ec}">{errs}</td><td class="warn">{len(issues) - errs}</td><td>{len(issues)}</td></tr>'
    html += '</table>'

    # 按规则汇总
    html += '<h2 style="font-size:16px;margin:20px 0 10px;">📊 按规则汇总</h2>'
    rule_counts = defaultdict(int)
    for d in results.values():
        for i in d.get("issues", []):
            rule_counts[i["rule"]] += 1
    html += '<table><tr><th>规则</th><th>CWV 指标</th><th>级别</th><th>数量</th><th>说明</th></tr>'
    for rule in RULES:
        cnt = rule_counts.get(rule.id, 0)
        if cnt == 0:
            continue
        sc = 'err' if rule.severity == 'error' else 'warn'
        html += f'<tr><td>{rule.id}</td>'
        html += f'<td><span class="metric-tag" style="background:{_metric_color(rule.cwv_metric)}">{rule.cwv_metric}</span></td>'
        html += f'<td class="{sc}">{rule.severity}</td><td>{cnt}</td><td>{rule.description}</td></tr>'
    html += '</table>'

    # 各项目详情
    html += '<h2 style="font-size:16px;margin:20px 0 10px;">📋 详细问题</h2>'
    for proj, data in sorted(results.items()):
        issues = data.get("issues", [])
        if not issues:
            html += f'<details><summary>{proj} — ✅ 无问题</summary></details>'
            continue
        html += f'<details><summary>{proj} — {len(issues)} 个问题</summary>'
        html += '<table><tr><th>文件</th><th>行</th><th>规则</th><th>CWV</th><th>级别</th><th>代码</th></tr>'
        for i in sorted(issues, key=lambda x: (x["file"], x["line"])):
            rule = rule_map.get(i["rule"])
            sc = 'err' if rule and rule.severity == 'error' else 'warn'
            ctx = i.get("context", "").replace("<", "&lt;").replace(">", "&gt;")
            cwv = rule.cwv_metric if rule else "?"
            html += f'<tr><td>{i["file"]}</td><td>{i["line"]}</td><td>{i["rule"]}</td>'
            html += f'<td><span class="metric-tag" style="background:{_metric_color(cwv)}">{cwv}</span></td>'
            html += f'<td class="{sc}">{rule.severity if rule else "?"}</td>'
            html += f'<td><span class="code">{ctx}</span></td></tr>'
        html += '</table></details>'

    html += '</body></html>'
    fname = f"cwv_{scan_time.replace(' ', '_').replace(':', '-')}.html"
    path = REPORT_DIR / fname
    path.write_text(html, encoding="utf-8")
    (REPORT_DIR / "latest.html").write_text(html, encoding="utf-8")
    return str(path)

def cleanup_old_reports(keep_days=3):
    """删除超过 keep_days 天的 HTML 报告文件和 SQLite 记录"""
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M:%S")
    # 清理 HTML 文件（保留 latest.html）
    for f in REPORT_DIR.glob("cwv_*.html"):
        stem = f.stem  # cwv_2026-04-08_09-13-12
        try:
            ts = stem[4:14] + " " + stem[15:].replace("-", ":")
            if ts < cutoff:
                f.unlink()
                print(f"🗑️ 删除旧报告: {f.name}", file=sys.stderr)
        except (ValueError, IndexError):
            continue
    # 清理 SQLite 记录
    try:
        conn = _db()
        conn.execute("DELETE FROM issues WHERE scan_time < ?", (cutoff,))
        conn.execute("DELETE FROM scans WHERE scan_time < ?", (cutoff,))
        deleted = conn.total_changes
        conn.execute("VACUUM")
        conn.commit()
        conn.close()
        if deleted:
            print(f"🗑️ 清理 {deleted} 条过期 DB 记录 (>{keep_days}天)", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ 清理 DB 失败: {e}", file=sys.stderr)



# ── 通知 + main ───────────────────────────────
def notify_wecom(chatid, message):
    import urllib.request as ur
    payload = json.dumps({"chatid": chatid, "content": message, "chat_type": 1}, ensure_ascii=False)
    req = ur.Request(f"http://127.0.0.1:{BRIDGE_PORT}/send", data=payload.encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with ur.urlopen(req, timeout=10):
            pass
    except Exception as e:
        print(f"通知失败: {e}", file=sys.stderr)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="CWV 代码扫描")
    parser.add_argument("--chatid", default="dm_Alan.Li")
    args = parser.parse_args()

    print("📊 CWV 代码扫描开始...", file=sys.stderr)
    results = scan_all()
    scan_time = save_to_db(results)
    alerts = check_regression(results)
    report = generate_html(results, scan_time, alerts)

    total = sum(len(d.get("issues", [])) for d in results.values())
    output = {
        "scan_time": scan_time,
        "projects": len(results),
        "total_files": sum(d.get("files", 0) for d in results.values()),
        "total_issues": total,
        "report": report,
        "alerts": alerts,
        "per_project": {p: {"files": d.get("files", 0), "issues": len(d.get("issues", []))}
                        for p, d in results.items()},
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))

    if alerts:
        msg = "🚨 CWV 代码扫描退化告警\n\n" + "\n".join(alerts) + f"\n\n📄 报告: {report}"
        notify_wecom(args.chatid, msg)
    else:
        # 每次扫描完推送结果摘要
        total_files = sum(d.get("files", 0) for d in results.values())
        rule_map = {r.id: r for r in RULES}
        total_errors = sum(1 for d in results.values() for i in d.get("issues", []) if rule_map.get(i["rule"], Rule("", "", "", "error", "")).severity == "error")
        total_warnings = total - total_errors
        msg = (f"📊 **CWV 扫描完成** {scan_time}\n"
               f"{len(results)} 项目 | {total_files} 文件 | "
               f"{total_errors} 错误 / {total_warnings} 警告")
        notify_wecom(args.chatid, msg)

    cleanup_old_reports(keep_days=3)


if __name__ == "__main__":
    main()
