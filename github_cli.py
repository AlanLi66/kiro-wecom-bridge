#!/usr/bin/env python3
"""GitHub CLI — 基于 gh CLI 的封装，ACP agent 通过 execute_bash 调用"""
import json, subprocess, sys, traceback


def _gh(args: list[str], accept: str = "") -> str:
    """调用 gh CLI，返回 stdout"""
    cmd = ["gh"] + args
    if accept:
        cmd += ["-H", f"Accept: {accept}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"gh exit code {r.returncode}")
    return r.stdout


def _gh_json(args: list[str]) -> dict | list:
    """调用 gh CLI，解析 JSON 输出"""
    return json.loads(_gh(args))


def _gh_api(path: str, method: str = "GET", body: dict | None = None,
            accept: str = "application/vnd.github+json") -> dict | list | str:
    """调用 gh api"""
    cmd = ["api", path, "--method", method, "-H", f"Accept: {accept}"]
    if body:
        cmd += ["--input", "-"]
        r = subprocess.run(["gh"] + cmd, capture_output=True, text=True,
                           input=json.dumps(body), timeout=60)
    else:
        r = subprocess.run(["gh"] + cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"gh api exit code {r.returncode}")
    if accept == "application/vnd.github.v3.diff":
        return r.stdout
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return r.stdout


def list_prs(owner: str, repo: str, state: str = "open") -> list:
    """列出 PR"""
    data = _gh_api(f"/repos/{owner}/{repo}/pulls?state={state}&per_page=20")
    return [
        {
            "number": pr["number"],
            "title": pr["title"],
            "author": pr["user"]["login"],
            "state": pr["state"],
            "draft": pr.get("draft", False),
            "created_at": pr["created_at"],
            "updated_at": pr["updated_at"],
            "url": pr["html_url"],
            "base": pr["base"]["ref"],
            "head": pr["head"]["ref"],
        }
        for pr in data
    ]


def get_pr(owner: str, repo: str, number: int) -> dict:
    """获取 PR 详情"""
    pr = _gh_api(f"/repos/{owner}/{repo}/pulls/{number}")
    return {
        "number": pr["number"],
        "title": pr["title"],
        "body": (pr.get("body") or "")[:2000],
        "author": pr["user"]["login"],
        "state": pr["state"],
        "draft": pr.get("draft", False),
        "mergeable": pr.get("mergeable"),
        "additions": pr.get("additions", 0),
        "deletions": pr.get("deletions", 0),
        "changed_files": pr.get("changed_files", 0),
        "base": pr["base"]["ref"],
        "head": pr["head"]["ref"],
        "url": pr["html_url"],
        "created_at": pr["created_at"],
        "updated_at": pr["updated_at"],
    }


def get_pr_files(owner: str, repo: str, number: int) -> list:
    """获取 PR 变更文件列表"""
    data = _gh_api(f"/repos/{owner}/{repo}/pulls/{number}/files?per_page=100")
    return [
        {
            "filename": f["filename"],
            "status": f["status"],
            "additions": f["additions"],
            "deletions": f["deletions"],
            "patch": (f.get("patch") or "")[:3000],
        }
        for f in data
    ]


def get_pr_diff(owner: str, repo: str, number: int) -> str:
    """获取 PR 完整 diff"""
    diff = _gh_api(f"/repos/{owner}/{repo}/pulls/{number}",
                   accept="application/vnd.github.v3.diff")
    if len(diff) > 30000:
        return diff[:30000] + f"\n\n... [diff truncated, total length: {len(diff)}]"
    return diff


def get_pr_comments(owner: str, repo: str, number: int) -> list:
    """获取 PR review comments"""
    data = _gh_api(f"/repos/{owner}/{repo}/pulls/{number}/comments?per_page=50")
    return [
        {
            "user": c["user"]["login"],
            "body": c["body"][:500],
            "path": c.get("path"),
            "line": c.get("line"),
            "created_at": c["created_at"],
        }
        for c in data
    ]


def create_review_comment(owner: str, repo: str, number: int,
                          body: str, path: str, line: int,
                          side: str = "RIGHT", commit_id: str = "") -> dict:
    """在 PR 的指定文件行上添加 review comment"""
    if not commit_id:
        pr = _gh_api(f"/repos/{owner}/{repo}/pulls/{number}")
        commit_id = pr.get("head", {}).get("sha", "")
    payload = {
        "body": body, "commit_id": commit_id,
        "path": path, "line": line, "side": side,
    }
    return _gh_api(f"/repos/{owner}/{repo}/pulls/{number}/comments",
                   method="POST", body=payload)


def submit_review(owner: str, repo: str, number: int,
                  body: str, event: str = "COMMENT") -> dict:
    """提交 PR review（COMMENT / APPROVE / REQUEST_CHANGES）"""
    return _gh_api(f"/repos/{owner}/{repo}/pulls/{number}/reviews",
                   method="POST", body={"body": body, "event": event})


def list_repos(owner: str) -> list:
    """列出用户/组织的仓库"""
    try:
        data = _gh_api(f"/orgs/{owner}/repos?per_page=30&sort=updated&type=all")
    except RuntimeError:
        data = _gh_api(f"/users/{owner}/repos?per_page=30&sort=updated")
    return [{"name": r["name"], "full_name": r["full_name"], "private": r["private"],
             "url": r["html_url"], "updated_at": r["updated_at"]} for r in data]


# ── CLI 入口 ──────────────────────────────────────────────

ACTIONS = {
    "list_prs": lambda a: list_prs(a["owner"], a["repo"], a.get("state", "open")),
    "get_pr": lambda a: get_pr(a["owner"], a["repo"], a["number"]),
    "get_pr_files": lambda a: get_pr_files(a["owner"], a["repo"], a["number"]),
    "get_pr_diff": lambda a: get_pr_diff(a["owner"], a["repo"], a["number"]),
    "get_pr_comments": lambda a: get_pr_comments(a["owner"], a["repo"], a["number"]),
    "create_review_comment": lambda a: create_review_comment(
        a["owner"], a["repo"], a["number"], a["body"], a["path"], a["line"],
        a.get("side", "RIGHT"), a.get("commit_id", "")
    ),
    "submit_review": lambda a: submit_review(
        a["owner"], a["repo"], a["number"], a["body"], a.get("event", "COMMENT")
    ),
    "list_repos": lambda a: list_repos(a["owner"]),
}


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"error": "usage: github_cli.py <action> '<json_args>'"}))
        sys.exit(1)

    action = sys.argv[1]
    try:
        args = json.loads(sys.argv[2])
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid JSON: {e}"}))
        sys.exit(1)

    if action not in ACTIONS:
        print(json.dumps({"error": f"unknown action: {action}", "available": list(ACTIONS.keys())}))
        sys.exit(1)

    try:
        result = ACTIONS[action](args)
        if isinstance(result, str):
            print(result)
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except KeyError as e:
        print(json.dumps({"error": f"missing required field: {e}"}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e), "trace": traceback.format_exc()}))
        sys.exit(1)


if __name__ == "__main__":
    main()
