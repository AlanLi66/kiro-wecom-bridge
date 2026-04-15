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


def create_pr(owner: str, repo: str, title: str, head: str, base: str = "master",
              body: str = "", draft: bool = False) -> dict:
    """创建 Pull Request"""
    payload = {"title": title, "head": head, "base": base, "body": body, "draft": draft}
    pr = _gh_api(f"/repos/{owner}/{repo}/pulls", method="POST", body=payload)
    return {
        "number": pr["number"],
        "title": pr["title"],
        "url": pr["html_url"],
        "head": pr["head"]["ref"],
        "base": pr["base"]["ref"],
        "draft": pr.get("draft", False),
        "state": pr["state"],
    }


def list_tags(owner: str, repo: str, per_page: int = 20) -> list:
    """列出仓库 tags"""
    data = _gh_api(f"/repos/{owner}/{repo}/tags?per_page={per_page}")
    return [{"name": t["name"], "sha": t["commit"]["sha"][:8]} for t in data]
def list_releases(owner: str, repo: str, per_page: int = 10) -> list:
    """列出仓库 releases（按发布时间倒序，用于查找回滚 tag）"""
    data = _gh_api(f"/repos/{owner}/{repo}/releases?per_page={per_page}")
    return [{"tag": r["tag_name"], "name": r["name"], "published_at": r.get("published_at", ""),
             "draft": r.get("draft", False), "prerelease": r.get("prerelease", False),
             "url": r["html_url"]} for r in data]


def create_tag(owner: str, repo: str, tag: str, sha: str = "", message: str = "") -> dict:
    """创建 tag（annotated tag via Git API）。sha 默认为默认分支最新 commit。"""
    if not sha:
        repo_info = _gh_api(f"/repos/{owner}/{repo}")
        default_branch = repo_info.get("default_branch", "master")
        branch = _gh_api(f"/repos/{owner}/{repo}/branches/{default_branch}")
        sha = branch["commit"]["sha"]
    # 创建 tag object
    tag_obj = _gh_api(f"/repos/{owner}/{repo}/git/tags", method="POST", body={
        "tag": tag, "message": message or tag,
        "object": sha, "type": "commit",
    })
    # 创建 ref 指向 tag object
    _gh_api(f"/repos/{owner}/{repo}/git/refs", method="POST", body={
        "ref": f"refs/tags/{tag}", "sha": tag_obj["sha"],
    })
    return {"tag": tag, "sha": sha[:8], "url": f"https://github.com/{owner}/{repo}/releases/tag/{tag}"}


def create_release(owner: str, repo: str, tag: str, name: str = "",
                   body: str = "", draft: bool = False, prerelease: bool = False) -> dict:
    """基于 tag 创建 GitHub Release"""
    payload = {
        "tag_name": tag, "name": name or tag, "body": body,
        "draft": draft, "prerelease": prerelease,
    }
    rel = _gh_api(f"/repos/{owner}/{repo}/releases", method="POST", body=payload)
    return {"id": rel["id"], "tag": rel["tag_name"], "name": rel["name"],
            "url": rel["html_url"], "draft": rel.get("draft", False)}


def delete_tag(owner: str, repo: str, tag: str) -> dict:
    """删除 tag（同时删除关联的 release）"""
    # 先尝试删除关联的 release
    try:
        rel = _gh_api(f"/repos/{owner}/{repo}/releases/tags/{tag}")
        _gh_api(f"/repos/{owner}/{repo}/releases/{rel['id']}", method="DELETE")
    except RuntimeError:
        pass  # 没有关联 release，继续删 tag
    # 删除 git ref
    _gh_api(f"/repos/{owner}/{repo}/git/refs/tags/{tag}", method="DELETE")
    return {"deleted": tag}


def update_release(owner: str, repo: str, tag: str, name: str = "",
                   body: str = "", draft: bool = False, prerelease: bool = False) -> dict:
    """更新已有 release 的信息"""
    rel = _gh_api(f"/repos/{owner}/{repo}/releases/tags/{tag}")
    payload = {}
    if name:
        payload["name"] = name
    if body:
        payload["body"] = body
    if "draft" in {draft}:
        payload["draft"] = draft
    if "prerelease" in {prerelease}:
        payload["prerelease"] = prerelease
    if not payload:
        return {"error": "nothing to update"}
    updated = _gh_api(f"/repos/{owner}/{repo}/releases/{rel['id']}", method="PATCH", body=payload)
    return {"id": updated["id"], "tag": updated["tag_name"], "name": updated["name"],
            "url": updated["html_url"]}


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
    "create_pr": lambda a: create_pr(
        a["owner"], a["repo"], a["title"], a["head"],
        a.get("base", "master"), a.get("body", ""), a.get("draft", False)
    ),
    "list_tags": lambda a: list_tags(a["owner"], a["repo"], a.get("per_page", 20)),
    "list_releases": lambda a: list_releases(a["owner"], a["repo"], a.get("per_page", 10)),
    "create_tag": lambda a: create_tag(
        a["owner"], a["repo"], a["tag"], a.get("sha", ""), a.get("message", "")
    ),
    "create_release": lambda a: create_release(
        a["owner"], a["repo"], a["tag"], a.get("name", ""), a.get("body", ""),
        a.get("draft", False), a.get("prerelease", False)
    ),
    "delete_tag": lambda a: delete_tag(a["owner"], a["repo"], a["tag"]),
    "update_release": lambda a: update_release(
        a["owner"], a["repo"], a["tag"], a.get("name", ""), a.get("body", ""),
        a.get("draft", False), a.get("prerelease", False)
    ),
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
