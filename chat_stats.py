"""对话统计 — 记录每条消息的元数据，用于统计对话频率和用户活跃度"""
import logging
import os
import sqlite3
import time
import threading

log = logging.getLogger(__name__)

WORK_DIR = os.getenv("KIRO_WORK_DIR", "/mnt/i/workspace/alan_bot")
DB_PATH = os.path.join(WORK_DIR, "wecom-sessions", "chat_stats.db")

_lock = threading.Lock()
_conn: sqlite3.Connection = None


def _get_conn() -> sqlite3.Connection:
    """进程级单例连接，线程安全（SQLite 在 WSL /mnt/ 上无法可靠创建多连接）"""
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, timeout=5, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _init_tables(_conn)
    return _conn


def _init_tables(conn: sqlite3.Connection):
    """建表（幂等）"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chatid TEXT NOT NULL,
            userid TEXT NOT NULL,
            msg_type TEXT NOT NULL DEFAULT 'text',
            msg_len INTEGER NOT NULL DEFAULT 0,
            ts INTEGER NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_logs_chatid ON chat_logs(chatid)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_logs_ts ON chat_logs(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_logs_userid ON chat_logs(userid)")
    conn.commit()


def record(chatid: str, userid: str, msg_type: str = "text", msg_len: int = 0):
    """记录一条消息元数据，失败不影响主流程"""
    try:
        with _lock:
            conn = _get_conn()
            conn.execute(
                "INSERT INTO chat_logs (chatid, userid, msg_type, msg_len, ts) VALUES (?, ?, ?, ?, ?)",
                (chatid, userid, msg_type, msg_len, int(time.time())),
            )
            conn.commit()
    except Exception as e:
        log.warning("chat_stats 写入失败: %s", e)


def query(sql: str, params: tuple = ()) -> list:
    """通用查询接口，供 dashboard 等模块复用同一连接"""
    with _lock:
        conn = _get_conn()
        return conn.execute(sql, params).fetchall()
