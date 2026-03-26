#!/usr/bin/env python3
"""Google Sheets CLI — ACP agent 通过 execute_bash 调用，零外部依赖"""
import json, os, sys, traceback, urllib.request, urllib.error, urllib.parse

TOKEN_PATH = os.getenv("GSHEET_TOKEN_PATH", "/mnt/i/AI/mcp-config/google-token.json")
API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE_API = "https://www.googleapis.com/drive/v3/files"


def _load_token() -> dict:
    with open(TOKEN_PATH) as f:
        return json.load(f)


def _save_token(data: dict):
    with open(TOKEN_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _refresh_access_token(token_data: dict) -> str:
    """用 refresh_token 刷新 access_token"""
    params = urllib.parse.urlencode({
        "client_id": token_data["client_id"],
        "client_secret": token_data["client_secret"],
        "refresh_token": token_data["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(token_data["token_uri"], data=params, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode())
    token_data["token"] = result["access_token"]
    _save_token(token_data)
    return result["access_token"]


def _api(url: str, method: str = "GET", body: dict | None = None, retry: bool = True) -> dict | list:
    """调用 Google API，自动刷新 token"""
    token_data = _load_token()
    access_token = token_data["token"]

    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req) as resp:
            text = resp.read().decode()
            return json.loads(text) if text.strip() else {}
    except urllib.error.HTTPError as e:
        if e.code == 401 and retry:
            # token 过期，刷新后重试
            _refresh_access_token(token_data)
            return _api(url, method, body, retry=False)
        err_body = e.read().decode() if e.fp else ""
        return {"error": f"HTTP {e.code}", "message": err_body[:500]}


# ── 操作函数 ──────────────────────────────────────────────

def get_sheet(spreadsheet_id: str, range_: str = "") -> dict:
    """读取 Sheet 数据"""
    if range_:
        url = f"{API_BASE}/{spreadsheet_id}/values/{urllib.parse.quote(range_)}"
    else:
        url = f"{API_BASE}/{spreadsheet_id}?fields=spreadsheetId,properties.title,sheets.properties"
    return _api(url)


def get_values(spreadsheet_id: str, range_: str) -> list:
    """读取指定范围的值"""
    url = f"{API_BASE}/{spreadsheet_id}/values/{urllib.parse.quote(range_)}"
    data = _api(url)
    return data.get("values", [])


def update_values(spreadsheet_id: str, range_: str, values: list) -> dict:
    """更新指定范围的值"""
    url = f"{API_BASE}/{spreadsheet_id}/values/{urllib.parse.quote(range_)}?valueInputOption=USER_ENTERED"
    return _api(url, method="PUT", body={"values": values})


def append_values(spreadsheet_id: str, range_: str, values: list) -> dict:
    """追加行"""
    url = f"{API_BASE}/{spreadsheet_id}/values/{urllib.parse.quote(range_)}:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
    return _api(url, method="POST", body={"values": values})


def list_sheets(spreadsheet_id: str) -> list:
    """列出所有 sheet tab"""
    data = get_sheet(spreadsheet_id)
    if "error" in data:
        return data
    return [
        {"title": s["properties"]["title"], "sheetId": s["properties"]["sheetId"],
         "rows": s["properties"].get("gridProperties", {}).get("rowCount", 0),
         "cols": s["properties"].get("gridProperties", {}).get("columnCount", 0)}
        for s in data.get("sheets", [])
    ]


def search_spreadsheets(query: str) -> list:
    """在 Google Drive 中搜索 Spreadsheet"""
    q = urllib.parse.quote(f"mimeType='application/vnd.google-apps.spreadsheet' and name contains '{query}'")
    url = f"{DRIVE_API}?q={q}&fields=files(id,name,modifiedTime)&pageSize=20"
    data = _api(url)
    return data.get("files", [])


def find_in_sheet(spreadsheet_id: str, sheet: str, query: str) -> list:
    """在 sheet 中搜索包含关键词的单元格"""
    values = get_values(spreadsheet_id, sheet)
    results = []
    for r, row in enumerate(values):
        for c, cell in enumerate(row):
            if query.lower() in str(cell).lower():
                col_letter = chr(65 + c) if c < 26 else f"{chr(64 + c // 26)}{chr(65 + c % 26)}"
                results.append({"cell": f"{col_letter}{r + 1}", "value": str(cell)})
                if len(results) >= 50:
                    return results
    return results


# ── CLI 入口 ──────────────────────────────────────────────

ACTIONS = {
    "get_sheet": lambda a: get_sheet(a["spreadsheet_id"], a.get("range", "")),
    "get_values": lambda a: get_values(a["spreadsheet_id"], a["range"]),
    "update_values": lambda a: update_values(a["spreadsheet_id"], a["range"], a["values"]),
    "append_values": lambda a: append_values(a["spreadsheet_id"], a["range"], a["values"]),
    "list_sheets": lambda a: list_sheets(a["spreadsheet_id"]),
    "search": lambda a: search_spreadsheets(a["query"]),
    "find": lambda a: find_in_sheet(a["spreadsheet_id"], a["sheet"], a["query"]),
}


def main():
    if not os.path.exists(TOKEN_PATH):
        print(json.dumps({"error": f"Token file not found: {TOKEN_PATH}"}))
        sys.exit(1)

    if len(sys.argv) < 3:
        print(json.dumps({"error": "usage: gsheet_cli.py <action> '<json_args>'"}))
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
