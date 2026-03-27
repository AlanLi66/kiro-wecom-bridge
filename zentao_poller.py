"""禅道 Bug 轮询器 — 定期检查新 bug 并触发 agent 分析"""
import asyncio, json, logging, os, time
from datetime import datetime

log = logging.getLogger(__name__)

POLL_INTERVAL = int(os.getenv("ZENTAO_POLL_INTERVAL", "900"))  # 默认 15 分钟
BRIDGE_PORT = int(os.getenv("PORT", "8900"))
ZENTAO_CLI = os.path.join(os.path.dirname(__file__), "zentao_cli.py")
VENV_PYTHON = os.path.join(os.path.dirname(__file__), ".venv", "bin", "python3")

# 轮询状态
_state = {
    "enabled": False,
    "chatid": "dm_Alan.Li",
    "product_id": 11,
    "seen_ids": set(),       # 已推送过的 bug id
    "last_poll": None,
    "poll_count": 0,
    "new_bugs_found": 0,
}


def get_status() -> dict:
    return {
        "enabled": _state["enabled"],
        "chatid": _state["chatid"],
        "product_id": _state["product_id"],
        "interval_seconds": POLL_INTERVAL,
        "last_poll": _state["last_poll"],
        "poll_count": _state["poll_count"],
        "new_bugs_found": _state["new_bugs_found"],
        "seen_count": len(_state["seen_ids"]),
    }


def enable(chatid: str = "dm_Alan.Li", product_id: int = 11):
    _state["enabled"] = True
    _state["chatid"] = chatid
    _state["product_id"] = product_id
    log.info("禅道轮询已开启 chatid=%s product=%d", chatid, product_id)


def disable():
    _state["enabled"] = False
    log.info("禅道轮询已关闭")


def reset_seen():
    """重置已见 bug 列表（下次轮询会重新推送所有 active bug）"""
    _state["seen_ids"].clear()
    log.info("已重置 seen_ids")


async def _fetch_bugs() -> list[dict]:
    """调用 zentao_cli.py 获取 bug 列表"""
    import subprocess
    cmd = [
        VENV_PYTHON, ZENTAO_CLI, "list_bugs",
        json.dumps({"product_id": _state["product_id"], "browse_type": "assigntome"})
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            log.error("zentao_cli list_bugs 失败: %s", stderr.decode())
            return []
        return json.loads(stdout.decode())
    except Exception as e:
        log.error("获取禅道 bug 列表异常: %s", e)
        return []


async def _trigger_analysis(bug_id: int, title: str):
    """通过 cron/trigger 接口让 agent 分析 bug"""
    import aiohttp
    prompt = (
        f"[cron]: 禅道新 Bug #{bug_id} 需要分析\n"
        f"标题: {title}\n\n"
        f"请执行以下步骤:\n"
        f"1. 调用 zentao_cli.py get_bug 获取 bug 详情（含复现步骤）\n"
        f"2. 根据 bug 描述中的关键词，在代码中搜索可能的问题点\n"
        f"3. 如果 bug 涉及线上异常，用 kibana_search.py 查最近的 error 日志\n"
        f"4. 输出分析结果：bug 摘要 + 可能的代码位置 + 修复建议\n\n"
        f"调用示例:\n"
        f"zentao_cli.py get_bug '{{\"id\": {bug_id}}}'"
    )
    payload = {
        "chatid": _state["chatid"],
        "prompt": prompt,
        "bot_index": 0,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{BRIDGE_PORT}/cron/trigger",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=360),
            ) as resp:
                result = await resp.json()
                if result.get("ok"):
                    log.info("Bug #%d 分析已触发", bug_id)
                else:
                    log.warning("Bug #%d 分析触发失败: %s", bug_id, result)
    except Exception as e:
        log.error("触发 bug 分析异常 #%d: %s", bug_id, e)


async def poll_loop():
    """主轮询循环，在 lifespan 中启动"""
    # 启动时先初始化 seen_ids（避免首次开启时推送所有历史 bug）
    log.info("禅道轮询器已启动，间隔 %ds", POLL_INTERVAL)
    _initialized = False

    while True:
        await asyncio.sleep(30)  # 每 30 秒检查一次是否 enabled

        if not _state["enabled"]:
            _initialized = False
            continue

        # 首次开启时，先拉一次列表记录已有 bug，不触发分析
        if not _initialized:
            bugs = await _fetch_bugs()
            for b in bugs:
                if b.get("status") == "active":
                    _state["seen_ids"].add(int(b["id"]))
            _initialized = True
            _state["last_poll"] = datetime.now().isoformat(timespec="seconds")
            log.info("禅道轮询初始化完成，已记录 %d 个现有 bug", len(_state["seen_ids"]))
            await asyncio.sleep(POLL_INTERVAL)
            continue

        # 正常轮询
        bugs = await _fetch_bugs()
        _state["last_poll"] = datetime.now().isoformat(timespec="seconds")
        _state["poll_count"] += 1

        new_bugs = []
        for b in bugs:
            bid = int(b["id"])
            if b.get("status") == "active" and bid not in _state["seen_ids"]:
                new_bugs.append(b)
                _state["seen_ids"].add(bid)

        if new_bugs:
            _state["new_bugs_found"] += len(new_bugs)
            log.info("发现 %d 个新 bug", len(new_bugs))
            for b in new_bugs:
                await _trigger_analysis(int(b["id"]), b.get("title", ""))
                await asyncio.sleep(5)  # 间隔 5 秒避免并发过高
        else:
            log.debug("本轮无新 bug")

        await asyncio.sleep(POLL_INTERVAL)
