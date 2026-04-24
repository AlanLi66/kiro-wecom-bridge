#!/usr/bin/env python3
"""
定时批量 git pull 工作区项目。
仅在当前分支为 master 或 testing 时执行 pull，其他分支跳过。

用法：
  python3 git_pull_all.py           # pull 所有项目
  python3 git_pull_all.py --dry-run # 只检查不执行
  python3 git_pull_all.py --json    # JSON 格式输出（供 agent 解析）

cron 示例（每天凌晨 2:30）：
  30 2 * * * /mnt/i/workspace/kiro-wecom-bridge/.venv/bin/python3 /mnt/i/workspace/kiro-wecom-bridge/git_pull_all.py >> /mnt/i/workspace/kiro-wecom-bridge/logs/git_pull.log 2>&1
"""

import subprocess
import sys
import json
import os
from datetime import datetime
from pathlib import Path

# 需要保持最新的项目列表（路径 + 说明）
PROJECTS = [
    # 后端 Java 服务
    ("/mnt/i/workspace/ec-so-service", "订单服务"),
    ("/mnt/i/workspace/ec-payment-service", "支付服务"),
    ("/mnt/i/workspace/ec-customer-service", "用户服务"),
    ("/mnt/i/workspace/ec-inventory-service", "库存服务"),
    ("/mnt/i/workspace/ec-rma-service", "售后服务"),
    ("/mnt/i/workspace/ec-activity-service", "活动服务"),
    ("/mnt/i/workspace/ec-distributor-service", "校园大使服务"),
    ("/mnt/i/workspace/ec-tax-service", "税费服务"),
    ("/mnt/i/workspace/central-so-service", "中台订单服务"),
    ("/mnt/i/workspace/central-payment-service", "中台支付服务"),
    ("/mnt/i/workspace/central-customer-service", "中台用户服务"),
    ("/mnt/i/workspace/central-activity-service", "中台活动服务"),
    ("/mnt/i/workspace/central-distributor-service", "中台校园大使服务"),
    ("/mnt/i/workspace/central-fp-service", "中台风控服务"),
    ("/mnt/i/workspace/central-mkt-service", "中台跟买看板服务"),
    ("/mnt/i/workspace/central-rma-service", "中台售后服务"),
    # 前端 Web 项目
    ("/mnt/c/Alan/workspace/ec-website-nb", "主站PC"),
    ("/mnt/c/Alan/workspace/ec-website-next", "主站PC迁移版"),
    ("/mnt/c/Alan/workspace/ec-website-customer-nb", "个人中心PC"),
    ("/mnt/c/Alan/workspace/ec-website-customer-next", "个人中心PC迁移版"),
    ("/mnt/c/Alan/workspace/ec-website-trade-nb", "交易PC"),
    ("/mnt/c/Alan/workspace/ec-mobilesite-nb", "移动站H5"),
    ("/mnt/c/Alan/workspace/ec-mobilesite-ssr", "个人中心H5"),
    ("/mnt/c/Alan/workspace/ec-mobilesite-rma", "售后H5"),
    # APP 项目
    ("/mnt/c/Alan/workspace/mobile_flutter", "APP Flutter"),
    ("/mnt/i/workspace/mobile_android", "APP Android原生"),
    ("/mnt/i/workspace/mobile_ios", "APP iOS原生"),
]

# 允许 pull 的分支白名单
PULL_BRANCHES = {"master", "testing", "main"}


def get_current_branch(project_path: str) -> str | None:
    """获取项目当前分支名"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def git_pull(project_path: str) -> tuple[bool, str]:
    """执行 git pull，返回 (成功, 输出信息)"""
    try:
        result = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = result.stdout.strip() or result.stderr.strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "超时（60s）"
    except Exception as e:
        return False, str(e)


def main():
    """主函数"""
    dry_run = "--dry-run" in sys.argv
    json_output = "--json" in sys.argv

    results = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for project_path, desc in PROJECTS:
        # 检查目录是否存在
        if not Path(project_path).exists():
            results.append({
                "project": os.path.basename(project_path),
                "desc": desc,
                "path": project_path,
                "status": "skip",
                "reason": "目录不存在",
                "branch": None,
            })
            continue

        # 检查是否是 git 仓库
        if not Path(project_path, ".git").exists():
            results.append({
                "project": os.path.basename(project_path),
                "desc": desc,
                "path": project_path,
                "status": "skip",
                "reason": "非 git 仓库",
                "branch": None,
            })
            continue

        # 获取当前分支
        branch = get_current_branch(project_path)
        if branch is None:
            results.append({
                "project": os.path.basename(project_path),
                "desc": desc,
                "path": project_path,
                "status": "error",
                "reason": "无法获取分支",
                "branch": None,
            })
            continue

        # 判断是否在允许 pull 的分支上
        if branch not in PULL_BRANCHES:
            results.append({
                "project": os.path.basename(project_path),
                "desc": desc,
                "path": project_path,
                "status": "skip",
                "reason": f"当前分支 {branch}，非 master/testing",
                "branch": branch,
            })
            continue

        # 执行 pull
        if dry_run:
            results.append({
                "project": os.path.basename(project_path),
                "desc": desc,
                "path": project_path,
                "status": "dry_run",
                "reason": f"将 pull 分支 {branch}",
                "branch": branch,
            })
        else:
            success, output = git_pull(project_path)
            results.append({
                "project": os.path.basename(project_path),
                "desc": desc,
                "path": project_path,
                "status": "ok" if success else "error",
                "reason": output,
                "branch": branch,
            })

    # 输出结果
    if json_output:
        print(json.dumps({"time": now, "results": results}, ensure_ascii=False, indent=2))
    else:
        print(f"📦 Git Pull All — {now}")
        print(f"{'='*60}")

        pulled = [r for r in results if r["status"] == "ok"]
        skipped = [r for r in results if r["status"] == "skip"]
        errors = [r for r in results if r["status"] == "error"]
        dry_runs = [r for r in results if r["status"] == "dry_run"]

        if dry_runs:
            print(f"\n🔍 Dry Run（{len(dry_runs)} 个项目将被 pull）:")
            for r in dry_runs:
                print(f"  ✅ {r['project']} ({r['desc']}) — {r['reason']}")

        if pulled:
            print(f"\n✅ 已更新（{len(pulled)} 个）:")
            for r in pulled:
                status = "已是最新" if "Already up to date" in r["reason"] else "有更新"
                print(f"  · {r['project']} ({r['desc']}) [{r['branch']}] — {status}")

        if skipped:
            print(f"\n⏭️ 跳过（{len(skipped)} 个）:")
            for r in skipped:
                print(f"  · {r['project']} ({r['desc']}) — {r['reason']}")

        if errors:
            print(f"\n❌ 失败（{len(errors)} 个）:")
            for r in errors:
                print(f"  · {r['project']} ({r['desc']}) — {r['reason']}")

        print(f"\n{'='*60}")
        print(f"总计: {len(results)} 个项目 | ✅ {len(pulled)} 更新 | ⏭️ {len(skipped)} 跳过 | ❌ {len(errors)} 失败")


if __name__ == "__main__":
    main()
