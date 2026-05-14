"""分支代码逐个 Review — 将单次大 prompt 拆分为 N 次独立 review

原来：1 个 prompt 让 agent 扫描 8 个项目所有活跃分支（1 条消息）
现在：先获取分支列表 → 逐个分支独立 review → 汇总（N+2 条消息）

每个分支的 review 是独立的 prompt，agent 会：
  读 diff → 分析代码质量 → 输出重构建议

预计消息量：8 项目 × 2-3 分支 × 每个 10-20 次工具调用 = 200-500 次/天
"""
import asyncio
import json
import logging
import os
import subprocess
import time

log = logging.getLogger(__name__)

WORK_DIR = os.getenv("KIRO_WORK_DIR", "/mnt/i/workspace/alan_bot")

# 要扫描的前端项目
PROJECTS = [
    {"name": "ec-website-nb", "path": "/mnt/c/Alan/workspace/ec-website-nb"},
    {"name": "ec-website-next", "path": "/mnt/c/Alan/workspace/ec-website-next"},
    {"name": "ec-website-customer-nb", "path": "/mnt/c/Alan/workspace/ec-website-customer-nb"},
    {"name": "ec-website-customer-next", "path": "/mnt/c/Alan/workspace/ec-website-customer-next"},
    {"name": "ec-website-trade-nb", "path": "/mnt/c/Alan/workspace/ec-website-trade-nb"},
    {"name": "ec-mobilesite-nb", "path": "/mnt/c/Alan/workspace/ec-mobilesite-nb"},
    {"name": "ec-mobilesite-ssr", "path": "/mnt/c/Alan/workspace/ec-mobilesite-ssr"},
    {"name": "ec-mobilesite-rma", "path": "/mnt/c/Alan/workspace/ec-mobilesite-rma"},
]

REVIEW_PROMPT = """请 review 以下分支的代码变更，给出重构建议。

项目: {project}
分支: {branch}
变更文件数: {file_count}

## 变更文件列表
{file_list}

## Diff 内容（核心变更）
```
{diff}
```

请从以下角度分析：
1. 🐛 潜在 Bug（NPE、边界条件、逻辑错误）
2. 📐 代码规范（命名、重复代码、过长函数/文件）
3. ⚡ 性能问题（N+1、不必要的循环、大对象拷贝）
4. 🔒 安全问题（注入、硬编码、敏感信息）
5. 💡 重构建议（更好的设计、可复用的抽象）

输出格式：
- 如果没有明显问题：✅ LGTM + 简短说明
- 如果有问题：按严重程度列出（🚨严重 / ⚠️建议 / 💡优化）

保持简洁，不超过 500 字。
"""


def get_active_branches(project_path: str, days: int = 14) -> list[str]:
    """获取最近 N 天有提交的远程分支（排除 master/main/HEAD）"""
    try:
        result = subprocess.run(
            ["git", "branch", "-r", "--sort=-committerdate",
             f"--format=%(refname:short) %(committerdate:unix)"],
            capture_output=True, text=True, timeout=10, cwd=project_path)
        if result.returncode != 0:
            return []

        cutoff = int(time.time()) - days * 86400
        branches = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.strip().split()
            if len(parts) < 2:
                continue
            branch_name = parts[0]
            try:
                commit_ts = int(parts[1])
            except ValueError:
                continue
            # 排除 master/main/HEAD
            if any(skip in branch_name for skip in ["master", "main", "HEAD"]):
                continue
            # 去掉 origin/ 前缀
            if branch_name.startswith("origin/"):
                branch_name = branch_name[7:]
            if commit_ts >= cutoff:
                branches.append(branch_name)

        return branches[:10]  # 最多 10 个分支
    except Exception as e:
        log.error("获取分支失败 %s: %s", project_path, e)
        return []


def get_branch_diff(project_path: str, branch: str, max_chars: int = 10000) -> tuple[str, str, int]:
    """获取分支相对于 master 的 diff

    Returns: (file_list, diff_content, file_count)
    """
    try:
        # 文件列表
        stat_result = subprocess.run(
            ["git", "diff", f"master...origin/{branch}", "--stat"],
            capture_output=True, text=True, timeout=15, cwd=project_path)
        file_list = stat_result.stdout.strip()[-2000:] if stat_result.stdout else "无变更"

        # 文件数
        numstat = subprocess.run(
            ["git", "diff", f"master...origin/{branch}", "--numstat"],
            capture_output=True, text=True, timeout=15, cwd=project_path)
        file_count = len([l for l in numstat.stdout.strip().split("\n") if l.strip()])

        # diff 内容（截断）
        diff_result = subprocess.run(
            ["git", "diff", f"master...origin/{branch}", "--no-color"],
            capture_output=True, text=True, timeout=30, cwd=project_path)
        diff = diff_result.stdout.strip()
        if len(diff) > max_chars:
            diff = diff[:max_chars] + "\n\n... (diff 过长已截断)"

        return file_list, diff, file_count
    except Exception as e:
        log.error("获取 diff 失败 %s:%s: %s", project_path, branch, e)
        return "", "", 0


async def run_branch_review(chatid: str, pool, ws):
    """逐分支 review 主流程

    Args:
        chatid: 推送结果的 chatid
        pool: ProcessPool 实例
        ws: WsClient 实例
    """
    chat_type = 1 if chatid.startswith("dm_") else 2
    start_time = time.time()

    # 第一步：收集所有活跃分支
    all_branches = []
    for project in PROJECTS:
        branches = get_active_branches(project["path"])
        for b in branches:
            all_branches.append({"project": project["name"], "path": project["path"], "branch": b})

    if not all_branches:
        await ws.send_msg(chatid, chat_type, "📊 分支 Review：未发现活跃分支（最近14天无提交）")
        return

    await ws.send_msg(chatid, chat_type,
        f"📊 **分支代码 Review 启动**\n"
        f"- 项目数: {len(PROJECTS)}\n"
        f"- 活跃分支: {len(all_branches)}\n"
        f"- 模式: 逐分支独立 Review\n---")

    # 获取 agent 进程
    proc = await pool.get_or_create(
        f"{chatid}/_branch_review", agent=None,
        cwd=WORK_DIR, mode="readonly")

    results = []
    reviewed = 0
    prompt_count = 0  # agent prompt 发送计数

    for i, item in enumerate(all_branches, 1):
        project = item["project"]
        branch = item["branch"]
        path = item["path"]

        # 获取 diff
        file_list, diff, file_count = get_branch_diff(path, branch)
        if not diff or file_count == 0:
            results.append({
                "project": project,
                "branch": branch,
                "status": "skipped",
                "detail": "无变更",
            })
            continue

        # 构建 review prompt
        prompt = REVIEW_PROMPT.format(
            project=project,
            branch=branch,
            file_count=file_count,
            file_list=file_list,
            diff=diff,
        )

        try:
            reply = await proc.send(prompt, timeout=120)
            reviewed += 1
            prompt_count += 1

            # 判断结果
            has_issues = any(kw in reply for kw in ["🚨", "⚠️", "严重", "问题"])
            status = "issues" if has_issues else "clean"

            results.append({
                "project": project,
                "branch": branch,
                "status": status,
                "detail": reply[:500],
            })

            # 实时推送（只推送有问题的）
            if has_issues:
                await ws.send_msg(chatid, chat_type,
                    f"⚠️ [{i}/{len(all_branches)}] **{project}** `{branch}`\n{reply[:1000]}")
            else:
                await ws.send_msg(chatid, chat_type,
                    f"✅ [{i}/{len(all_branches)}] **{project}** `{branch}` — LGTM")

        except Exception as e:
            results.append({
                "project": project,
                "branch": branch,
                "status": "error",
                "detail": str(e),
            })
            log.error("Review 异常 %s:%s: %s", project, branch, e)

        # 分支间间隔
        await asyncio.sleep(1)

    # 汇总
    duration = int(time.time() - start_time)
    issues_count = sum(1 for r in results if r["status"] == "issues")
    clean_count = sum(1 for r in results if r["status"] == "clean")
    skipped_count = sum(1 for r in results if r["status"] == "skipped")

    summary = (
        f"📊 **分支 Review 完成**\n"
        f"- 耗时: {duration}s\n"
        f"- 已审查: {reviewed}/{len(all_branches)} 分支\n"
        f"- 有问题: {issues_count} | 无问题: {clean_count} | 跳过: {skipped_count}\n"
        f"- Agent prompt 次数: {prompt_count}\n"
    )

    if issues_count > 0:
        summary += "\n⚠️ 需关注的分支:\n"
        for r in results:
            if r["status"] == "issues":
                summary += f"  • {r['project']} / {r['branch']}\n"

    await ws.send_msg(chatid, chat_type, summary)

    log.info("Branch review 完成 chatid=%s branches=%d reviewed=%d issues=%d prompts=%d duration=%ds",
             chatid, len(all_branches), reviewed, issues_count, prompt_count, duration)

    return results
