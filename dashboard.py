"""Agent 实时看板 — 提供 HTML 页面 + JSON 数据 API"""
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta

WORK_DIR = os.getenv("KIRO_WORK_DIR", "/mnt/i/workspace/alan_bot")
SESSIONS_DIR = os.path.join(WORK_DIR, "wecom-sessions")
REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
SCHEDULER_DB = os.path.join(SESSIONS_DIR, "scheduler.db")
CHAT_STATS_DB = os.path.join(SESSIONS_DIR, "chat_stats.db")


def get_process_pool_status(cm) -> dict:
    """获取进程池状态"""
    pools = []
    for i, ch in enumerate(cm.channels):
        pool = ch.pool
        active_procs = []
        for chatid, proc in pool._pool.items():
            active_procs.append({
                "chatid": chatid,
                "alive": proc.alive,
                "idle_seconds": round(proc.idle_seconds, 1),
                "pid": proc._proc.pid if proc._proc else None,
                "mode": getattr(proc, "_mode", "unknown"),
            })
        pools.append({
            "bot_index": i,
            "bot_id": ch.bot_id,
            "active_count": len(pool._pool),
            "warm_count": len(pool._warm),
            "max_procs": pool.MAX_PROCS,
            "processes": active_procs,
        })
    return {"pools": pools}


def get_zentao_status() -> dict:
    """获取禅道轮询状态"""
    import zentao_poller
    return zentao_poller.get_status()


def get_chat_stats() -> dict:
    """获取对话统计"""
    if not os.path.exists(CHAT_STATS_DB):
        return {"today": 0, "week": 0, "total": 0, "by_user": [], "by_hour": []}

    try:
        conn = sqlite3.connect(CHAT_STATS_DB)
        conn.row_factory = sqlite3.Row

        now = int(time.time())
        today_start = now - (now % 86400) - 8 * 3600  # UTC+8 今天 0 点
        week_start = today_start - 6 * 86400

        # 今日消息数
        today = conn.execute("SELECT COUNT(*) as c FROM chat_logs WHERE ts >= ?", (today_start,)).fetchone()["c"]
        # 本周消息数
        week = conn.execute("SELECT COUNT(*) as c FROM chat_logs WHERE ts >= ?", (week_start,)).fetchone()["c"]
        # 总消息数
        total = conn.execute("SELECT COUNT(*) as c FROM chat_logs").fetchone()["c"]

        # 按用户统计（本周）
        by_user = conn.execute(
            "SELECT userid, COUNT(*) as cnt FROM chat_logs WHERE ts >= ? GROUP BY userid ORDER BY cnt DESC LIMIT 10",
            (week_start,)
        ).fetchall()

        # 按用户统计（全部时间）
        by_user_all = conn.execute(
            "SELECT userid, COUNT(*) as cnt FROM chat_logs GROUP BY userid ORDER BY cnt DESC LIMIT 10"
        ).fetchall()

        # 按小时统计（今天）
        by_hour = conn.execute(
            "SELECT CAST((ts - ?) / 3600 AS INTEGER) as hour, COUNT(*) as cnt "
            "FROM chat_logs WHERE ts >= ? GROUP BY hour ORDER BY hour",
            (today_start, today_start)
        ).fetchall()

        conn.close()
        return {
            "today": today,
            "week": week,
            "total": total,
            "by_user": [{"userid": r["userid"], "count": r["cnt"]} for r in by_user],
            "by_user_all": [{"userid": r["userid"], "count": r["cnt"]} for r in by_user_all],
            "by_hour": [{"hour": r["hour"], "count": r["cnt"]} for r in by_hour],
        }
    except Exception as e:
        return {"today": 0, "week": 0, "total": 0, "by_user": [], "by_user_all": [], "by_hour": [], "error": str(e)}


def get_cron_tasks() -> dict:
    """获取所有定时任务"""
    from cron_dashboard import get_crontab_tasks, get_scheduler_tasks
    return {
        "crontab": get_crontab_tasks(),
        "scheduler": get_scheduler_tasks(),
    }


def get_scan_results() -> dict:
    """获取扫描结果"""
    from cron_dashboard import get_ada_latest, get_cwv_latest, get_web_test_latest, get_git_pull_latest
    return {
        "ada": get_ada_latest(),
        "cwv": get_cwv_latest(),
        "web_test": get_web_test_latest(),
        "git_pull": get_git_pull_latest(),
    }


def get_memory_stats() -> dict:
    """获取记忆系统统计"""
    stats = []
    if not os.path.exists(SESSIONS_DIR):
        return {"databases": stats}

    for entry in os.listdir(SESSIONS_DIR):
        mem_db = os.path.join(SESSIONS_DIR, entry, "memory.db")
        if os.path.exists(mem_db):
            try:
                conn = sqlite3.connect(mem_db)
                entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
                relations = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
                conn.close()
                stats.append({"chatid": entry, "entities": entities, "relations": relations})
            except Exception:
                pass

    return {"databases": stats}


def get_system_info() -> dict:
    """获取系统基本信息"""
    import platform
    uptime = None
    try:
        with open("/proc/uptime", "r") as f:
            uptime = round(float(f.read().split()[0]) / 3600, 1)
    except Exception:
        pass

    return {
        "platform": platform.system(),
        "python": platform.python_version(),
        "hostname": platform.node(),
        "uptime_hours": uptime,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def get_full_dashboard(cm) -> dict:
    """汇总所有看板数据"""
    return {
        "system": get_system_info(),
        "process_pool": get_process_pool_status(cm),
        "zentao": get_zentao_status(),
        "chat_stats": get_chat_stats(),
        "cron_tasks": get_cron_tasks(),
        "scan_results": get_scan_results(),
        "memory": get_memory_stats(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
