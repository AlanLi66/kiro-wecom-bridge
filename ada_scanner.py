#!/usr/bin/env python3
"""ADA 无障碍静态扫描器 — 扫描本地前端代码中的 WCAG 违规
支持 Blade/PHP、Vue、JSX/TSX 模板，检查 img alt、表单 label、aria 属性、语义标签等。
结果存 SQLite + 生成 HTML 报告，退化时推企微告警。

用法: python3 ada_scanner.py [--chatid CHATID]
"""
import json, os, re, sys, sqlite3, glob, time
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

BRIDGE_DIR = Path(__file__).resolve().parent
REPORT_DIR = BRIDGE_DIR / "reports" / "ada"
DB_PATH = BRIDGE_DIR / "reports" / "ada.db"
BRIDGE_PORT = int(os.getenv("PORT", "8900"))

# 项目配置: (路径, 技术栈, 模板目录, 文件后缀)
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

# 排除目录
EXCLUDE_DIRS = {"node_modules", ".next", ".nuxt", ".output", "vendor", "dist", "build", ".git", "__pycache__", "storage", "bootstrap"}


# ── 规则定义 ──────────────────────────────────
class Rule:
    def __init__(self, rule_id, name, wcag, severity, description):
        self.id = rule_id
        self.name = name
        self.wcag = wcag          # e.g. "1.1.1"
        self.severity = severity  # "error" | "warning"
        self.description = description


RULES = [
    Rule("img-alt", "img 缺少 alt 属性", "1.1.1", "error",
         "<img> 必须有 alt 属性，为屏幕阅读器提供替代文本"),
    Rule("img-alt-empty", "img alt 为空字符串（装饰性图片需确认）", "1.1.1", "warning",
         'alt="" 仅适用于纯装饰性图片，功能性图片必须有描述'),
    Rule("input-label", "表单控件缺少关联 label", "1.3.1", "error",
         "input/select/textarea 需要关联的 label 或 aria-label"),
    Rule("button-text", "按钮缺少可访问文本", "4.1.2", "error",
         "button 需要文本内容、aria-label 或 aria-labelledby"),
    Rule("a-text", "链接缺少可访问文本", "2.4.4", "warning",
         "a 标签需要文本内容或 aria-label，不能只有图片无 alt"),
    Rule("html-lang", "缺少 html lang 属性", "3.1.1", "warning",
         "<html> 标签应有 lang 属性声明页面语言"),
    Rule("heading-order", "标题层级跳跃", "1.3.1", "warning",
         "标题应按顺序使用（h1→h2→h3），不应跳级"),
    Rule("img-width-height", "img 缺少 width/height（影响 CLS）", "N/A", "warning",
         "img 应设置 width 和 height 防止布局偏移"),
    Rule("tabindex-positive", "tabindex 为正数", "2.4.3", "warning",
         "tabindex 应为 0 或 -1，正数会破坏自然 tab 顺序"),
    Rule("aria-role-valid", "无效的 ARIA role", "4.1.2", "error",
         "role 属性值必须是有效的 WAI-ARIA role"),
]

VALID_ROLES = {
    "alert", "alertdialog", "application", "article", "banner", "button", "cell",
    "checkbox", "columnheader", "combobox", "complementary", "contentinfo", "definition",
    "dialog", "directory", "document", "feed", "figure", "form", "grid", "gridcell",
    "group", "heading", "img", "link", "list", "listbox", "listitem", "log", "main",
    "marquee", "math", "menu", "menubar", "menuitem", "menuitemcheckbox", "menuitemradio",
    "navigation", "none", "note", "option", "presentation", "progressbar", "radio",
    "radiogroup", "region", "row", "rowgroup", "rowheader", "scrollbar", "search",
    "searchbox", "separator", "slider", "spinbutton", "status", "switch", "tab",
    "table", "tablist", "tabpanel", "term", "textbox", "timer", "toolbar", "tooltip",
    "tree", "treegrid", "treeitem",
}


# ── 扫描逻辑 ──────────────────────────────────
def _find_files(project_dir: str, template_dir: str, pattern: str) -> list[str]:
    """递归查找模板文件，排除依赖目录"""
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


def _read_file(path: str) -> str:
    for enc in ("utf-8", "latin-1"):
        try:
            return open(path, encoding=enc).read()
        except UnicodeDecodeError:
            continue
    return ""


def _relative_path(filepath: str, project_dir: str) -> str:
    return os.path.relpath(filepath, project_dir)


def _scan_file(filepath: str, tech: str) -> list[dict]:
    """扫描单个文件，返回问题列表"""
    content = _read_file(filepath)
    if not content:
        return []
    lines = content.split("\n")
    issues = []

    def _add(rule_id, line_num, context=""):
        issues.append({"rule": rule_id, "line": line_num, "context": context[:120]})

    # 逐行 + 正则扫描
    for i, line in enumerate(lines, 1):
        stripped = line.strip()

        # img-alt: <img 没有 alt
        for m in re.finditer(r'<img\b[^>]*?(?:/\s*>|>)', line, re.IGNORECASE):
            tag = m.group()
            if 'alt' not in tag.lower():
                _add("img-alt", i, tag)
            elif re.search(r'alt\s*=\s*["\'][\s]*["\']', tag):
                _add("img-alt-empty", i, tag)
            # img-width-height
            has_w = re.search(r'\b(width|w)\s*=', tag, re.IGNORECASE) or 'width:' in tag
            has_h = re.search(r'\b(height|h)\s*=', tag, re.IGNORECASE) or 'height:' in tag
            if not (has_w and has_h):
                _add("img-width-height", i, tag)

        # Next.js <Image 组件也检查 alt
        if tech == "react":
            for m in re.finditer(r'<Image\b[^>]*?(?:/\s*>|>)', line):
                tag = m.group()
                if 'alt' not in tag:
                    _add("img-alt", i, tag)

        # Vue :src 动态图片也检查
        if tech == "vue":
            for m in re.finditer(r'<img\b[^>]*:src[^>]*?(?:/\s*>|>)', line, re.IGNORECASE):
                tag = m.group()
                if 'alt' not in tag.lower():
                    _add("img-alt", i, tag)

        # input-label: input/select/textarea 没有 aria-label 且不在 label 内
        for m in re.finditer(r'<(input|select|textarea)\b[^>]*?(?:/\s*>|>)', line, re.IGNORECASE):
            tag = m.group()
            tag_type = re.search(r'type\s*=\s*["\'](\w+)["\']', tag, re.IGNORECASE)
            if tag_type and tag_type.group(1) in ("hidden", "submit", "button", "reset"):
                continue
            has_label = any(x in tag.lower() for x in ('aria-label', 'aria-labelledby', 'id='))
            if not has_label:
                _add("input-label", i, tag)

        # button-text: 空 button
        for m in re.finditer(r'<button\b[^>]*>(.*?)</button>', line, re.IGNORECASE | re.DOTALL):
            tag_attrs = re.search(r'<button\b([^>]*)>', m.group(), re.IGNORECASE).group(1)
            inner = m.group(1).strip()
            inner_text = re.sub(r'<[^>]+>', '', inner).strip()
            has_aria = any(x in tag_attrs.lower() for x in ('aria-label', 'aria-labelledby'))
            if not inner_text and not has_aria:
                _add("button-text", i, m.group()[:120])

        # tabindex positive
        for m in re.finditer(r'tabindex\s*=\s*["\']?(\d+)', line, re.IGNORECASE):
            val = int(m.group(1))
            if val > 0:
                _add("tabindex-positive", i, m.group())

        # aria-role-valid
        for m in re.finditer(r'role\s*=\s*["\']([^"\']+)["\']', line, re.IGNORECASE):
            role = m.group(1).strip().lower()
            if role and role not in VALID_ROLES:
                _add("aria-role-valid", i, m.group())

    # html-lang: 检查布局文件
    if any(x in filepath.lower() for x in ('layout', 'app.', 'document', '_app', 'root')):
        if '<html' in content and not re.search(r'<html[^>]*\blang\s*=', content, re.IGNORECASE):
            _add("html-lang", 1, "<html> missing lang")

    return issues


# ── 主扫描 ────────────────────────────────────
def scan_all() -> dict:
    """扫描所有项目，返回 {project: {files_scanned, issues: [...]}}"""
    results = {}
    for proj_name, tech, tpl_dir, pattern in PROJECTS:
        proj_dir = os.path.join(BASE_DIR, proj_name)
        if not os.path.isdir(proj_dir):
            results[proj_name] = {"tech": tech, "files": 0, "issues": [], "error": "目录不存在"}
            continue
        files = _find_files(proj_dir, tpl_dir, pattern)
        # Vue 项目也扫 components 目录
        if tech == "vue" and tpl_dir == "pages":
            files += _find_files(proj_dir, "components", pattern)
        all_issues = []
        for f in files:
            file_issues = _scan_file(f, tech)
            rel = _relative_path(f, proj_dir)
            for issue in file_issues:
                issue["file"] = rel
            all_issues.extend(file_issues)
        results[proj_name] = {"tech": tech, "files": len(files), "issues": all_issues}
    return results


# ── DB ────────────────────────────────────────
def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_time TEXT NOT NULL,
            project TEXT NOT NULL,
            files_scanned INTEGER DEFAULT 0,
            total_issues INTEGER DEFAULT 0,
            errors INTEGER DEFAULT 0,
            warnings INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_time TEXT NOT NULL,
            project TEXT NOT NULL,
            file TEXT NOT NULL,
            line INTEGER,
            rule TEXT NOT NULL,
            context TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_scans_time ON scans(scan_time);
    """)
    return conn


def save_to_db(results: dict) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _db()
    for proj, data in results.items():
        issues = data.get("issues", [])
        rule_map = {r.id: r for r in RULES}
        errs = sum(1 for i in issues if rule_map.get(i["rule"], Rule("", "", "", "error", "")).severity == "error")
        warns = len(issues) - errs
        conn.execute("INSERT INTO scans (scan_time,project,files_scanned,total_issues,errors,warnings) VALUES (?,?,?,?,?,?)",
                     (now, proj, data.get("files", 0), len(issues), errs, warns))
        for i in issues:
            conn.execute("INSERT INTO issues (scan_time,project,file,line,rule,context) VALUES (?,?,?,?,?,?)",
                         (now, proj, i["file"], i["line"], i["rule"], i.get("context", "")))
    conn.commit()
    conn.close()
    return now


def check_regression(results: dict) -> list[str]:
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
                alerts.append(f"⚠️ {proj}: {prev['total_issues']} → {len(data['issues'])} (+{diff} 问题)")
    conn.close()
    return alerts


# ── HTML 报告 ─────────────────────────────────
def _sev_color(sev):
    return "#ff4e42" if sev == "error" else "#ffa400"

def generate_html(results: dict, scan_time: str, alerts: list[str]) -> str:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    rule_map = {r.id: r for r in RULES}

    total_files = sum(d.get("files", 0) for d in results.values())
    total_issues = sum(len(d.get("issues", [])) for d in results.values())
    total_errors = sum(1 for d in results.values() for i in d.get("issues", []) if rule_map.get(i["rule"], RULES[0]).severity == "error")

    # 趋势数据
    conn = _db()
    trend_rows = conn.execute(
        "SELECT scan_time, project, total_issues FROM scans ORDER BY scan_time DESC LIMIT 80").fetchall()
    conn.close()
    trend = defaultdict(list)
    for r in trend_rows:
        trend[r["project"]].append({"time": r["scan_time"], "count": r["total_issues"]})

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>ADA Scan — {scan_time}</title>
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
  table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.1); margin-bottom: 20px; }}
  th {{ background: #16213e; color: #fff; padding: 10px 12px; text-align: left; font-size: 13px; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid #eee; font-size: 13px; }}
  tr:hover {{ background: #f8f9fa; }}
  .err {{ color: #ff4e42; font-weight: 600; }}
  .warn {{ color: #ffa400; font-weight: 600; }}
  .ok {{ color: #0cce6b; font-weight: 600; }}
  .code {{ font-family: 'Fira Code', monospace; font-size: 12px; background: #f4f4f4; padding: 2px 6px; border-radius: 3px; word-break: break-all; }}
  details {{ margin-bottom: 8px; }}
  summary {{ cursor: pointer; font-weight: 600; padding: 8px; background: #fff; border-radius: 6px; box-shadow: 0 1px 2px rgba(0,0,0,.08); }}
  .rule-ref {{ background: #fff; border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.1); margin-bottom: 20px; }}
  .rule-ref td {{ padding: 4px 12px; font-size: 13px; }}
</style></head><body>
<div class="header">
  <h1>♿ ADA Accessibility Code Scan</h1>
  <div class="meta">{scan_time} · {len(results)} projects · {total_files} files</div>
</div>"""

    if alerts:
        html += '<div class="alert">⚠️ <b>退化告警</b><br>' + '<br>'.join(alerts) + '</div>'

    html += f"""<div class="summary">
  <div class="card"><div class="num">{len(results)}</div><div class="label">项目</div></div>
  <div class="card"><div class="num">{total_files}</div><div class="label">文件</div></div>
  <div class="card"><div class="num err">{total_errors}</div><div class="label">Errors</div></div>
  <div class="card"><div class="num warn">{total_issues - total_errors}</div><div class="label">Warnings</div></div>
</div>"""

    # 项目概览
    html += '<table><tr><th>项目</th><th>技术栈</th><th>文件数</th><th>Errors</th><th>Warnings</th><th>总计</th></tr>'
    for proj, data in sorted(results.items()):
        issues = data.get("issues", [])
        errs = sum(1 for i in issues if rule_map.get(i["rule"], RULES[0]).severity == "error")
        warns = len(issues) - errs
        ec = 'err' if errs > 0 else 'ok'
        html += f'<tr><td>{proj}</td><td>{data["tech"]}</td><td>{data.get("files", 0)}</td>'
        html += f'<td class="{ec}">{errs}</td><td class="warn">{warns}</td><td>{len(issues)}</td></tr>'
    html += '</table>'

    # 按规则汇总
    html += '<h2 style="font-size:16px;margin:20px 0 10px;">📊 按规则汇总</h2>'
    rule_counts = defaultdict(int)
    for d in results.values():
        for i in d.get("issues", []):
            rule_counts[i["rule"]] += 1
    html += '<table><tr><th>规则</th><th>WCAG</th><th>级别</th><th>数量</th><th>说明</th></tr>'
    for rule in RULES:
        cnt = rule_counts.get(rule.id, 0)
        if cnt == 0:
            continue
        sc = 'err' if rule.severity == 'error' else 'warn'
        html += f'<tr><td>{rule.id}</td><td>{rule.wcag}</td><td class="{sc}">{rule.severity}</td><td>{cnt}</td><td>{rule.description}</td></tr>'
    html += '</table>'

    # 各项目详情（折叠）
    html += '<h2 style="font-size:16px;margin:20px 0 10px;">📋 详细问题</h2>'
    for proj, data in sorted(results.items()):
        issues = data.get("issues", [])
        if not issues:
            html += f'<details><summary>{proj} — ✅ 无问题</summary></details>'
            continue
        html += f'<details><summary>{proj} — {len(issues)} 个问题</summary>'
        html += '<table><tr><th>文件</th><th>行</th><th>规则</th><th>级别</th><th>代码片段</th></tr>'
        for i in sorted(issues, key=lambda x: (x["file"], x["line"])):
            rule = rule_map.get(i["rule"])
            sc = 'err' if rule and rule.severity == 'error' else 'warn'
            ctx = i.get("context", "").replace("<", "&lt;").replace(">", "&gt;")
            html += f'<tr><td>{i["file"]}</td><td>{i["line"]}</td><td>{i["rule"]}</td>'
            html += f'<td class="{sc}">{rule.severity if rule else "?"}</td>'
            html += f'<td><span class="code">{ctx}</span></td></tr>'
        html += '</table></details>'

    html += '</body></html>'

    fname = f"ada_{scan_time.replace(' ', '_').replace(':', '-')}.html"
    path = REPORT_DIR / fname
    path.write_text(html, encoding="utf-8")
    (REPORT_DIR / "latest.html").write_text(html, encoding="utf-8")
    return str(path)

def cleanup_old_reports(keep_days=3):
    """删除超过 keep_days 天的 HTML 报告文件和 SQLite 记录"""
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M:%S")
    # 清理 HTML 文件（保留 latest.html）
    for f in REPORT_DIR.glob("ada_*.html"):
        stem = f.stem  # ada_2026-04-07_17-07-52
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
def notify_wecom(chatid: str, message: str):
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
    parser = argparse.ArgumentParser(description="ADA 无障碍代码扫描")
    parser.add_argument("--chatid", default="dm_Alan.Li")
    args = parser.parse_args()

    print("♿ ADA 代码扫描开始...", file=sys.stderr)
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
        msg = "🚨 ADA 代码扫描退化告警\n\n" + "\n".join(alerts) + f"\n\n📄 报告: {report}"
        notify_wecom(args.chatid, msg)

    cleanup_old_reports(keep_days=3)


if __name__ == "__main__":
    main()
