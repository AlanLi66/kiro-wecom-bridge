"""禅道 Bug 轮询器 — 定期检查新 bug 并触发 agent 分析"""
import asyncio, json, logging, os, time
from datetime import datetime

log = logging.getLogger(__name__)

_poll_interval = int(os.getenv("ZENTAO_POLL_INTERVAL", "900"))  # 默认 15 分钟
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
        "interval_seconds": _poll_interval,
        "last_poll": _state["last_poll"],
        "poll_count": _state["poll_count"],
        "new_bugs_found": _state["new_bugs_found"],
        "seen_count": len(_state["seen_ids"]),
    }


def set_interval(seconds: int):
    global _poll_interval
    _poll_interval = max(seconds, 10)
    log.info("轮询间隔已更新为 %ds", _poll_interval)


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
    get_bug_cmd = '{"id": ' + str(bug_id) + '}'
    prompt = (
        f"[cron]: 禅道新 Bug #{bug_id} 需要分析\n"
        f"标题: {title}\n\n"
        "请按以下流程处理:\n\n"
        "## 第 1 步：获取 Bug 详情\n"
        f"调用 zentao_cli.py get_bug '{get_bug_cmd}'，获取完整的复现步骤、严重程度、关联项目等信息。\n\n"
        "## 第 2 步：分析定位\n"
        "- 从 bug 标题/描述/复现步骤中提取关键词（接口名、错误信息、页面名等）\n"
        "- 根据 page-feature-map 和 project-index 确定涉及的项目和文件\n"
        "- 在代码中搜索相关文件（grep/find）\n"
        "- 如果涉及线上异常，用 kibana_search.py 查最近的 error 日志\n\n"
        "## 第 3 步：输出分析结果\n"
        "输出格式：\n"
        f"🐛 Bug #{bug_id}: {{标题}}\n"
        "严重程度: {severity} | 优先级: {pri}\n\n"
        "📍 问题定位:\n"
        "- 涉及项目: {项目名}\n"
        "- 相关文件: {文件路径}\n"
        "- 问题原因: {分析结论}\n\n"
        "🔧 修复建议:\n"
        "- {具体修复方案}\n\n"
        "## 第 4 步：引导修复\n"
        '分析完成后，主动询问用户："需要我直接帮你修改代码吗？"\n\n'
        "如果用户确认要修改，执行以下检查：\n"
        "1. 从 bug 详情的标题或描述中提取 OP 号（格式 OP-XXXXX）\n"
        "2. 确认涉及的项目目录，用 git branch --show-current 查看当前分支\n"
        "3. 检查当前分支名是否包含对应的 OP 号\n"
        "   - 如果一致 → 直接修改代码\n"
        '   - 如果不一致 → 提醒用户："当前分支是 {branch}，但 bug 关联的是 OP-XXXXX，分支不匹配。需要切换分支吗？"\n'
        "4. 分支确认后，按最小改动原则修复 bug，只改必要的代码\n"
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
    log.info("禅道轮询器已启动，间隔 %ds", _poll_interval)
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
            await asyncio.sleep(_poll_interval)
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

        await asyncio.sleep(_poll_interval)
