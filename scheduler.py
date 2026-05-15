"""定时任务调度器 — 基于系统 crontab + SQLite 持久化"""
import json, logging, os, sqlite3, subprocess, uuid
from datetime import datetime

log = logging.getLogger(__name__)

SESSIONS_DIR = os.path.join(os.getenv("KIRO_WORK_DIR", "/mnt/i/workspace/alan_bot"), "wecom-sessions")
DB_PATH = os.path.join(SESSIONS_DIR, "scheduler.db")
BRIDGE_PORT = int(os.getenv("PORT", "8900"))
CRONTAB_TAG = "# kiro-scheduler:"


def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        cron TEXT NOT NULL,
        chatid TEXT NOT NULL,
        prompt TEXT NOT NULL,
        bot_index INTEGER DEFAULT 0,
        enabled INTEGER DEFAULT 1,
        description TEXT DEFAULT '',
        timeout INTEGER DEFAULT 300,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    # 兼容旧表：如果 timeout 列不存在则添加
    try:
        conn.execute("SELECT timeout FROM jobs LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE jobs ADD COLUMN timeout INTEGER DEFAULT 300")
    conn.commit()
    return conn


JOBS_DIR = os.path.join(SESSIONS_DIR, "scheduler-jobs")


def _build_curl(job_id: str, chatid: str, prompt: str, bot_index: int, timeout: int = 300) -> str:
    # 将 payload 写入文件，crontab 中用 -d @file 引用，避免 command too long
    os.makedirs(JOBS_DIR, exist_ok=True)
    payload_path = os.path.join(JOBS_DIR, f"{job_id}.json")
    # 如果文件已存在且包含 steps（多轮模式），保留原文件不覆盖
    if os.path.exists(payload_path):
        try:
            with open(payload_path, "r", encoding="utf-8") as f:
                existing = json.loads(f.read())
            if "steps" in existing and existing["steps"]:
                # 只更新 chatid/bot_index/timeout，保留 steps
                existing["chatid"] = chatid
                existing["bot_index"] = bot_index
                existing["timeout"] = timeout
                with open(payload_path, "w", encoding="utf-8") as f:
                    json.dump(existing, f, ensure_ascii=False, indent=2)
                return (
                    f"curl -s -X POST http://127.0.0.1:{BRIDGE_PORT}/cron/trigger "
                    f"-H 'Content-Type: application/json' "
                    f"-d @{payload_path} "
                    f">> /tmp/kiro-scheduler.log 2>&1 {CRONTAB_TAG}{job_id}"
                )
        except (json.JSONDecodeError, IOError):
            pass
    # 单轮模式：正常写入
    payload = json.dumps({"chatid": chatid, "prompt": prompt, "bot_index": bot_index, "timeout": timeout}, ensure_ascii=False)
    with open(payload_path, "w", encoding="utf-8") as f:
        f.write(payload)
    return (
        f"curl -s -X POST http://127.0.0.1:{BRIDGE_PORT}/cron/trigger "
        f"-H 'Content-Type: application/json' "
        f"-d @{payload_path} "
        f">> /tmp/kiro-scheduler.log 2>&1 {CRONTAB_TAG}{job_id}"
    )


def _read_crontab() -> list[str]:
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        return r.stdout.strip().split("\n") if r.returncode == 0 and r.stdout.strip() else []
    except Exception:
        return []


def _write_crontab(lines: list[str]):
    content = "\n".join(lines) + "\n" if lines else ""
    subprocess.run(["crontab", "-"], input=content, text=True, check=True)


def _sync_one(job_id: str, cron: str, chatid: str, prompt: str, bot_index: int, enabled: bool, timeout: int = 300):
    """确保 crontab 中有/无这条任务"""
    lines = _read_crontab()
    tag = f"{CRONTAB_TAG}{job_id}"
    lines = [l for l in lines if tag not in l]
    if enabled:
        lines.append(f"{cron} {_build_curl(job_id, chatid, prompt, bot_index, timeout)}")
    _write_crontab(lines)


def sync_all():
    """启动时从 db 同步全部任务到 crontab"""
    conn = _db()
    jobs = conn.execute("SELECT * FROM jobs").fetchall()
    conn.close()
    # 先清除所有 kiro-scheduler 条目
    lines = [l for l in _read_crontab() if CRONTAB_TAG not in l]
    for j in jobs:
        if j["enabled"]:
            timeout = j["timeout"] if "timeout" in j.keys() else 300
            lines.append(f"{j['cron']} {_build_curl(j['id'], j['chatid'], j['prompt'], j['bot_index'], timeout)}")
    _write_crontab(lines)
    log.info("同步 %d 个定时任务到 crontab（%d 启用）", len(jobs), sum(1 for j in jobs if j["enabled"]))


def create_job(cron: str, chatid: str, prompt: str, bot_index: int = 0, description: str = "", timeout: int = 300) -> dict:
    job_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat(timespec="seconds")
    conn = _db()
    conn.execute(
        "INSERT INTO jobs (id, cron, chatid, prompt, bot_index, enabled, description, timeout, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
        (job_id, cron, chatid, prompt, bot_index, description, timeout, now, now),
    )
    conn.commit()
    conn.close()
    _sync_one(job_id, cron, chatid, prompt, bot_index, True, timeout)
    log.info("创建定时任务 %s: %s → %s", job_id, cron, chatid)
    return {"id": job_id, "cron": cron, "chatid": chatid, "prompt": prompt,
            "bot_index": bot_index, "enabled": True, "description": description, "timeout": timeout}


def list_jobs() -> list[dict]:
    conn = _db()
    rows = conn.execute("SELECT * FROM jobs ORDER BY created_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_job(job_id: str) -> dict | None:
    conn = _db()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_job(job_id: str, **kwargs) -> dict | None:
    conn = _db()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        conn.close()
        return None
    job = dict(row)
    for k in ("cron", "chatid", "prompt", "bot_index", "enabled", "description", "timeout"):
        if k in kwargs and kwargs[k] is not None:
            job[k] = kwargs[k]
    job["updated_at"] = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "UPDATE jobs SET cron=?, chatid=?, prompt=?, bot_index=?, enabled=?, description=?, timeout=?, updated_at=? WHERE id=?",
        (job["cron"], job["chatid"], job["prompt"], job["bot_index"], job["enabled"], job["description"], job.get("timeout", 300), job["updated_at"], job_id),
    )
    conn.commit()
    conn.close()
    _sync_one(job_id, job["cron"], job["chatid"], job["prompt"], job["bot_index"], bool(job["enabled"]), job.get("timeout", 300))
    return job


def delete_job(job_id: str) -> bool:
    conn = _db()
    row = conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        conn.close()
        return False
    conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()
    _sync_one(job_id, "", "", "", 0, False)
    log.info("删除定时任务 %s", job_id)
    return True
