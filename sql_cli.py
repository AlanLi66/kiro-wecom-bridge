#!/usr/bin/env python3
"""MySQL 只读查询 CLI — agent 通过 execute_bash 调用

安全设计：
- 只允许 SELECT 语句
- 参数化查询防 SQL 注入
- 结果集限制最大行数
- 连接信息从 db_config.json 读取
"""
import json, os, sys, traceback, re

try:
    import pymysql
except ImportError:
    print(json.dumps({"error": "pymysql not installed. Run: pip install pymysql"}))
    sys.exit(1)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db_config.json")
MAX_ROWS = 100

# ── 安全校验 ──────────────────────────────────────────────

FORBIDDEN_PATTERNS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|RENAME|GRANT|REVOKE|LOCK|UNLOCK|CALL|EXEC|EXECUTE|LOAD|INTO\s+OUTFILE|INTO\s+DUMPFILE)\b",
    re.IGNORECASE,
)


def validate_sql(sql: str) -> str | None:
    """校验 SQL 安全性，返回错误信息或 None"""
    stripped = sql.strip().rstrip(";").strip()
    if not stripped.upper().startswith("SELECT") and not stripped.upper().startswith("SHOW") and not stripped.upper().startswith("DESC"):
        return "只允许 SELECT / SHOW / DESC 语句"
    if FORBIDDEN_PATTERNS.search(stripped):
        return f"检测到禁止的关键词，只允许只读查询"
    if stripped.count(";") > 0:
        return "不允许多条语句（分号）"
    return None


# ── 数据库操作 ──────────────────────────────────────────────

def load_config(env: str) -> dict:
    """加载数据库配置"""
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"配置文件不存在: {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    if env not in config:
        raise ValueError(f"未知环境: {env}，可选: {list(config.keys())}")
    return config[env]


def execute_query(env: str, sql: str, params: list = None, database: str = None) -> dict:
    """执行只读查询"""
    err = validate_sql(sql)
    if err:
        return {"error": err}

    cfg = load_config(env)
    connect_args = dict(
        host=cfg["host"],
        port=int(cfg["port"]),
        user=cfg["user"],
        password=cfg["password"],
        charset="utf8mb4",
        connect_timeout=10,
        read_timeout=30,
        cursorclass=pymysql.cursors.DictCursor,
    )
    # 优先用参数指定的 database，其次用配置文件的
    db_name = database or cfg.get("database")
    if db_name:
        connect_args["database"] = db_name

    conn = pymysql.connect(**connect_args)
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params or [])
            rows = cursor.fetchmany(MAX_ROWS)
            total = cursor.rowcount
            # 将不可序列化的类型转为字符串
            for row in rows:
                for k, v in row.items():
                    if not isinstance(v, (str, int, float, bool, type(None))):
                        row[k] = str(v)
            return {
                "env": env,
                "total": total,
                "count": len(rows),
                "truncated": total > MAX_ROWS,
                "columns": list(rows[0].keys()) if rows else [],
                "rows": rows,
            }
    finally:
        conn.close()



def list_tables(env: str, database: str = None, pattern: str = None) -> dict:
    """列出数据库表"""
    sql = "SHOW TABLES"
    if pattern:
        sql += f" LIKE %s"
        return execute_query(env, sql, [pattern], database=database)
    return execute_query(env, sql, database=database)


def desc_table(env: str, table: str, database: str = None) -> dict:
    """查看表结构"""
    # 校验表名防注入（只允许字母数字下划线和点号）
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_.]*$", table):
        return {"error": f"非法表名: {table}"}
    return execute_query(env, f"DESC `{table}`", database=database)


# ── CLI 入口 ──────────────────────────────────────────────

def list_databases(env: str) -> dict:
    """列出所有数据库（不指定 database 连接）"""
    cfg = load_config(env)
    conn = pymysql.connect(
        host=cfg["host"],
        port=int(cfg["port"]),
        user=cfg["user"],
        password=cfg["password"],
        charset="utf8mb4",
        connect_timeout=10,
        read_timeout=30,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute("SHOW DATABASES")
            rows = cursor.fetchall()
            return {"env": env, "databases": [list(r.values())[0] for r in rows]}
    finally:
        conn.close()


ACTIONS = {
    "query": lambda a: execute_query(a["env"], a["sql"], a.get("params"), a.get("database")),
    "tables": lambda a: list_tables(a["env"], a.get("database"), a.get("pattern")),
    "desc": lambda a: desc_table(a["env"], a["table"], a.get("database")),
    "databases": lambda a: list_databases(a["env"]),
}


def main():
    if len(sys.argv) < 3:
        print(json.dumps({
            "error": "usage: sql_cli.py <action> '<json_args>'",
            "actions": {
                "query": {"env": "uat|prd", "sql": "SELECT ...", "params": ["optional"]},
                "tables": {"env": "uat|prd", "pattern": "%user%（可选）"},
                "desc": {"env": "uat|prd", "table": "table_name"},
            },
        }, ensure_ascii=False, indent=2))
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
