#!/usr/bin/env python3
"""OpenProject CLI — ACP agent 通过 execute_bash 调用"""
import json, os, sys, traceback, urllib.request, urllib.error, urllib.parse, base64

BASE_URL = os.getenv("OP_BASE_URL", "https://openproject.yamibuy.net")
API_KEY = os.getenv("OP_API_KEY", "ff9b87f1a98013b6491220253f651d7ecbef7075fd5d2f061842f12aedabf485")
AUTH_HEADER = "Basic " + base64.b64encode(f"apikey:{API_KEY}".encode()).decode()


def _api(path: str, method: str = "GET", body: dict | None = None) -> dict | list:
    url = f"{BASE_URL}/api/v3{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", AUTH_HEADER)
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if e.fp else ""
        return {"error": f"HTTP {e.code}", "message": err_body[:500]}


def my_tasks(status: str = "open") -> list:
    """查询我的任务"""
    filters = [
        {"assignee": {"operator": "=", "values": ["me"]}},
    ]
    if status == "open":
        filters.append({"status": {"operator": "o", "values": []}})
    elif status == "closed":
        filters.append({"status": {"operator": "c", "values": []}})
    f_str = urllib.parse.quote(json.dumps(filters))
    sort = urllib.parse.quote(json.dumps([["priority", "desc"]]))
    data = _api(f"/work_packages?filters={f_str}&sortBy={sort}&pageSize=20")
    if "error" in data:
        return data
    return [
        {
            "id": wp["id"],
            "subject": wp["subject"],
            "status": wp["_links"]["status"]["title"],
            "priority": wp["_links"]["priority"]["title"],
            "project": wp["_links"]["project"]["title"],
            "type": wp["_links"]["type"]["title"],
            "updated_at": wp.get("updatedAt", ""),
        }
        for wp in data.get("_embedded", {}).get("elements", [])
    ]


def get_task(task_id: int) -> dict:
    """获取任务详情"""
    wp = _api(f"/work_packages/{task_id}")
    if "error" in wp:
        return wp
    return {
        "id": wp["id"],
        "subject": wp["subject"],
        "description": (wp.get("description", {}) or {}).get("raw", "")[:2000],
        "status": wp["_links"]["status"]["title"],
        "priority": wp["_links"]["priority"]["title"],
        "project": wp["_links"]["project"]["title"],
        "type": wp["_links"]["type"]["title"],
        "assignee": wp["_links"].get("assignee", {}).get("title", "未分配"),
        "created_at": wp.get("createdAt", ""),
        "updated_at": wp.get("updatedAt", ""),
        "done_ratio": wp.get("percentageDone", 0),
    }


def update_status(task_id: int, status_id: int) -> dict:
    """更新任务状态（1=New, 7=In progress, 15=Done, 16=Launched）"""
    return _api(f"/work_packages/{task_id}", method="PATCH",
                body={"_links": {"status": {"href": f"/api/v3/statuses/{status_id}"}}})


def add_comment(task_id: int, comment: str) -> dict:
    """给任务添加评论"""
    return _api(f"/work_packages/{task_id}/activities", method="POST",
                body={"comment": {"raw": comment}})


def create_task(project_id: int, subject: str, type_id: int = 1,
                assignee_id: int = 0, description: str = "") -> dict:
    """创建工单。type_id: 1=Task, 2=Milestone, 3=Phase, 4=Feature, 5=Epic, 6=User Story, 7=Bug"""
    body = {
        "subject": subject,
        "_links": {
            "type": {"href": f"/api/v3/types/{type_id}"},
        },
    }
    if description:
        body["description"] = {"format": "markdown", "raw": description}
    if assignee_id:
        body["_links"]["assignee"] = {"href": f"/api/v3/users/{assignee_id}"}
    return _api(f"/projects/{project_id}/work_packages", method="POST", body=body)


def search_tasks(query: str, project_id: int = 0) -> list:
    """搜索任务"""
    filters = [{"subjectOrId": {"operator": "**", "values": [query]}}]
    if project_id:
        filters.append({"project": {"operator": "=", "values": [str(project_id)]}})
    f_str = urllib.parse.quote(json.dumps(filters))
    data = _api(f"/work_packages?filters={f_str}&pageSize=20")
    if "error" in data:
        return data
    return [
        {
            "id": wp["id"],
            "subject": wp["subject"],
            "status": wp["_links"]["status"]["title"],
            "project": wp["_links"]["project"]["title"],
        }
        for wp in data.get("_embedded", {}).get("elements", [])
    ]


# ── CLI 入口 ──────────────────────────────────────────────

ACTIONS = {
    "my_tasks": lambda a: my_tasks(a.get("status", "open")),
    "get_task": lambda a: get_task(a["id"]),
    "update_status": lambda a: update_status(a["id"], a["status_id"]),
    "add_comment": lambda a: add_comment(a["id"], a["comment"]),
    "search": lambda a: search_tasks(a["query"], a.get("project_id", 0)),
    "create_task": lambda a: create_task(a["project_id"], a["subject"], a.get("type_id", 1), a.get("assignee_id", 0), a.get("description", "")),
}


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"error": "usage: op_cli.py <action> '<json_args>'"}))
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
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except KeyError as e:
        print(json.dumps({"error": f"missing required field: {e}"}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e), "trace": traceback.format_exc()}))
        sys.exit(1)


if __name__ == "__main__":
    main()
