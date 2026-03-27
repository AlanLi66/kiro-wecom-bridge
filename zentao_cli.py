"""禅道 Bug 管理 CLI — 供 agent 通过 execute_bash 调用"""
import json, os, sys, requests, hashlib, re
from pathlib import Path

# 自动加载 .env（CLI 直接调用时需要）
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and k not in os.environ:  # 不覆盖已有环境变量
                os.environ[k] = v

ZENTAO_URL = os.getenv("ZENTAO_URL", "https://bugs.yamibuy.tech")
ZENTAO_USER = os.getenv("ZENTAO_USER", "Alan_Li")
ZENTAO_PASS = os.getenv("ZENTAO_PASS", "")

_session = requests.Session()
_session.verify = False
requests.packages.urllib3.disable_warnings()


def _get_session_id() -> tuple[str, str]:
    """获取禅道 session"""
    r = _session.get(f"{ZENTAO_URL}/api-getsessionid.json", timeout=10)
    data = r.json()
    if data.get("status") == "success":
        inner = json.loads(data["data"])
        return inner["sessionName"], inner["sessionID"]
    raise RuntimeError(f"获取 session 失败: {data}")


def _login():
    """登录禅道"""
    if not ZENTAO_PASS:
        raise RuntimeError("ZENTAO_PASS 未设置，请检查 .env 或环境变量")
    sname, sid = _get_session_id()
    _session.params = {sname: sid}
    r = _session.post(
        f"{ZENTAO_URL}/user-login.json",
        data={"account": ZENTAO_USER, "password": ZENTAO_PASS},
        timeout=10,
    )
    result = r.json()
    if result.get("status") != "success":
        raise RuntimeError(f"登录失败: {result}")
    # 验证登录身份
    inner = json.loads(result.get("data", "{}"))
    user = inner.get("user", {})
    account = user.get("account", "")
    if account:
        _session._zentao_account = account  # 记录实际登录账号
    return True


_logged_in = False


def _ensure_login():
    """确保已登录，未登录则自动登录"""
    global _logged_in
    if _logged_in:
        return
    _login()
    _logged_in = True


def list_my_bugs(product_id: int = 11, browse_type: str = "assigntome",
                 limit: int = 20, status: str = "", assigned_to: str = "") -> list[dict]:
    """获取 bug 列表
    
    Args:
        product_id: 产品 ID，默认 11
        browse_type: 浏览类型 assigntome/unclosed/unresolved/all 等
        limit: 返回数量
        status: 按状态过滤（active/resolved/closed），空=不过滤
        assigned_to: 按指派人过滤，空=不过滤
    """
    _ensure_login()
    # 禅道 URL 有两种格式：
    #   短格式: bug-browse-{pid}-all-{browseType}.json  （用于 assigntome 等视图）
    #   长格式: bug-browse-{pid}-{browseType}-{param}-{orderBy}-{limit}-{page}.json
    # assigntome/openedbyme/resolvedbyme 等用短格式才能正确返回
    view_types = {"assigntome", "openedbyme", "resolvedbyme", "assigntonull"}
    if browse_type in view_types:
        url = f"{ZENTAO_URL}/bug-browse-{product_id}-all-{browse_type}.json"
    else:
        url = f"{ZENTAO_URL}/bug-browse-{product_id}-{browse_type}-0-id_desc-{limit}-1.json"
    r = _session.get(url, timeout=15)
    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(f"获取 bug 列表失败: {data}")
    inner = json.loads(data["data"])
    bugs_raw = inner.get("bugs", [])
    # bugs 可能是 dict（id→bug）或 list
    if isinstance(bugs_raw, dict):
        bugs_raw = list(bugs_raw.values())
    result = []
    for b in bugs_raw:
        # 按状态过滤
        if status and b.get("status", "") != status:
            continue
        # 按指派人过滤
        if assigned_to and b.get("assignedTo", "") != assigned_to:
            continue
        result.append({
            "id": b.get("id"),
            "title": b.get("title", ""),
            "severity": b.get("severity"),
            "pri": b.get("pri"),
            "status": b.get("status"),
            "type": b.get("type", ""),
            "openedBy": b.get("openedBy", ""),
            "openedDate": b.get("openedDate", ""),
            "assignedTo": b.get("assignedTo", ""),
            "module": b.get("module", ""),
            "resolvedBy": b.get("resolvedBy", ""),
            "resolution": b.get("resolution", ""),
        })
    return result


def get_bug(bug_id: int) -> dict:
    """获取 bug 详情（含复现步骤）"""
    _ensure_login()
    url = f"{ZENTAO_URL}/bug-view-{bug_id}.json"
    r = _session.get(url, timeout=15)
    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(f"获取 bug 详情失败: {data}")
    inner = json.loads(data["data"])
    bug = inner.get("bug", {})
    # 清理 HTML 标签
    steps = bug.get("steps", "")
    steps_clean = re.sub(r"<[^>]+>", "", steps).strip()
    steps_clean = re.sub(r"\n{3,}", "\n\n", steps_clean)
    return {
        "id": bug.get("id"),
        "title": bug.get("title", ""),
        "severity": bug.get("severity"),
        "pri": bug.get("pri"),
        "status": bug.get("status"),
        "type": bug.get("type", ""),
        "steps": steps_clean,
        "openedBy": bug.get("openedBy", ""),
        "openedDate": bug.get("openedDate", ""),
        "assignedTo": bug.get("assignedTo", ""),
        "module": bug.get("module", ""),
        "modulePath": inner.get("modulePath", ""),
        "product": bug.get("product", ""),
        "project": bug.get("project", ""),
        "openedBuild": bug.get("openedBuild", ""),
        "resolvedBy": bug.get("resolvedBy", ""),
        "resolution": bug.get("resolution", ""),
        "keywords": bug.get("keywords", ""),
        "os": bug.get("os", ""),
        "browser": bug.get("browser", ""),
    }


def resolve_bug(bug_id: int, resolution: str = "fixed") -> bool:
    """解决 bug"""
    _ensure_login()
    url = f"{ZENTAO_URL}/bug-resolve-{bug_id}.json"
    r = _session.post(url, data={"resolution": resolution}, timeout=10)
    data = r.json()
    return data.get("status") == "success"


# ---- CLI 入口 ----

USAGE = """用法: zentao_cli.py <action> [json_args]

操作:
  list_bugs     获取 bug 列表（默认 assigntome）
                可选参数: product_id, browse_type, limit, status, assigned_to
                示例: {"status": "active", "assigned_to": "Alan_Li"}
  get_bug       获取 bug 详情  {"id": 6774}
  resolve_bug   解决 bug       {"id": 6774, "resolution": "fixed"}
"""


def main():
    if len(sys.argv) < 2:
        print(USAGE)
        sys.exit(1)

    action = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}

    try:
        if action == "list_bugs":
            bugs = list_my_bugs(
                product_id=args.get("product_id", 11),
                browse_type=args.get("browse_type", "assigntome"),
                limit=args.get("limit", 20),
                status=args.get("status", ""),
                assigned_to=args.get("assigned_to", ""),
            )
            print(json.dumps(bugs, ensure_ascii=False, indent=2))

        elif action == "get_bug":
            bug = get_bug(args["id"])
            print(json.dumps(bug, ensure_ascii=False, indent=2))

        elif action == "resolve_bug":
            ok = resolve_bug(args["id"], args.get("resolution", "fixed"))
            print(json.dumps({"ok": ok}, ensure_ascii=False))

        else:
            print(f"未知操作: {action}")
            print(USAGE)
            sys.exit(1)

    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
