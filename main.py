"""kiro-wecom-bridge: 企微智能机器人长连接"""
import asyncio, logging, os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from pydantic import BaseModel, Field

from typing import Optional
from channel import ChannelManager
from agents.teams.task_list import TaskList
from agents.teams.mailbox import Mailbox
import scheduler
import zentao_poller
import lab_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CHANNELS_PATH = os.getenv("CHANNELS_PATH", "channels.json")
cm = ChannelManager()


async def _cleanup_loop():
    while True:
        await asyncio.sleep(60)
        for ch in cm.channels:
            try:
                await ch.pool.cleanup_idle()
            except Exception as e:
                log.error("cleanup_idle 异常: %s", e)
            # 清理空闲的 GroupChat 会话
            for cid in list(ch._groupchats):
                session = ch._groupchats[cid]
                if session.can_recycle():
                    await session.stop()
                    del ch._groupchats[cid]
                    log.info("回收空闲 GroupChat chatid=%s", cid)
            # 清理空闲的 Teams 会话
            for cid in list(ch._teams):
                session = ch._teams[cid]
                if session.can_recycle():
                    await session.stop()
                    del ch._teams[cid]
                    log.info("回收空闲 Teams chatid=%s", cid)


async def _daily_memory_loop():
    """每天 0 点把昨天的 history 整理到长期记忆"""
    import glob, time as _time
    while True:
        # 计算到明天 0:00:05 的秒数
        now = _time.time()
        tomorrow = now - (now % 86400) + 86400 + 5  # UTC 明天 00:00:05
        # 调整为本地时区（UTC+8）
        local_midnight = tomorrow - 8 * 3600
        wait = max(local_midnight - now, 60)
        log.info("下次记忆整理: %.0f 秒后", wait)
        await asyncio.sleep(wait)
        # 扫描所有 chatid 的 history.jsonl
        sessions_dir = os.path.join(os.getenv("KIRO_WORK_DIR", "/mnt/i/workspace/alan_bot"), "wecom-sessions")
        for hist_file in glob.glob(os.path.join(sessions_dir, "*/history.jsonl")):
            chatid = os.path.basename(os.path.dirname(hist_file))
            if chatid.startswith("_warm"):
                continue
            try:
                with open(hist_file, "r") as f:
                    lines = f.readlines()
                if not lines:
                    continue
                # 检查是否是昨天的（第一行的 ts）
                import json as _json
                first_ts = _json.loads(lines[0].strip()).get("ts", 0)
                if _time.strftime("%Y-%m-%d", _time.localtime(first_ts)) == _time.strftime("%Y-%m-%d"):
                    continue  # 今天的，不处理
                log.info("整理昨日记忆 chatid=%s turns=%d", chatid, len(lines))
                from agents.process import _recycle_memory, _load_history, _clear_history
                session_dir = os.path.dirname(hist_file)
                history = _load_history(session_dir, max_turns=50)
                cwd = os.getenv("KIRO_WORK_DIR", "/mnt/i/workspace/alan_bot")
                await _recycle_memory(chatid, session_dir, cwd, history)
                _clear_history(session_dir)
            except Exception as e:
                log.error("整理记忆失败 chatid=%s: %s", chatid, e)


@asynccontextmanager
def _handle_exception(loop, context):
    """全局异常处理 — 防止未捕获的 Future 异常崩掉主进程"""
    msg = context.get("exception", context.get("message", "unknown"))
    log.error("未捕获的异步异常: %s", msg)


async def lifespan(app: FastAPI):
    log.info("kiro-wecom-bridge 启动")
    asyncio.get_event_loop().set_exception_handler(_handle_exception)
    cm.load(CHANNELS_PATH)
    ws_tasks = await cm.start_all()
    cleanup_task = asyncio.create_task(_cleanup_loop())
    memory_task = asyncio.create_task(_daily_memory_loop())
    scheduler.sync_all()
    zentao_task = asyncio.create_task(zentao_poller.poll_loop())
    # 预热进程池
    for ch in cm.channels:
        await ch.pool.warmup()
    yield
    zentao_task.cancel()
    cleanup_task.cancel()
    memory_task.cancel()
    for t in ws_tasks:
        t.cancel()
    for ch in cm.channels:
        await ch.pool.shutdown()


app = FastAPI(title="kiro-wecom-bridge", lifespan=lifespan)

# 注册 Requirement Lab 内部 API
lab_router.set_channel_manager(cm)
app.include_router(lab_router.router)


# ---- 定时任务触发接口 ----

class CronTriggerRequest(BaseModel):
    chatid: str
    prompt: str = ""    # 单轮模式的 prompt
    steps: list[str] | None = None  # 固定多轮模式：按顺序发送的 prompt 列表
    # 动态多轮模式：agent 自主决定是否继续
    loop: bool = False          # 是否启用动态循环模式
    max_rounds: int = 50        # 最大轮次上限
    continue_tag: str = "[CONTINUE]"  # agent 回复中包含此标记则继续下一轮
    done_tag: str = "[DONE]"    # agent 回复中包含此标记则结束（可选，无 continue_tag 也会结束）
    bot_index: int = 0
    timeout: int = 300          # 每轮执行超时秒数
    mode: str = "full"          # agent 权限模式：full=完整权限，safe=只读（无 execute_bash）


class SendMsgRequest(BaseModel):
    chatid: str = "dm_Alan.Li"
    content: str
    bot_index: int = 0
    chat_type: int = 1  # 1=单聊 2=群聊


@app.post("/send")
async def send_msg(req: SendMsgRequest):
    """主动发送消息到企微（供 notify-wecom 等 skill 调用）"""
    if req.bot_index >= len(cm.channels):
        return {"ok": False, "error": f"bot_index {req.bot_index} 超出范围"}
    ch = cm.channels[req.bot_index]
    try:
        await ch.ws.send_msg(req.chatid, req.chat_type, req.content)
        return {"ok": True}
    except Exception as e:
        log.error("send_msg 异常 chatid=%s: %s", req.chatid, e)
        return {"ok": False, "error": str(e)}


@app.post("/cron/trigger")
async def cron_trigger(req: CronTriggerRequest):
    """供 crontab 调用：向指定群发送 prompt，结果推送回企微。
    
    支持三种模式：
    - 单轮模式：传 prompt，执行一次返回结果
    - 固定多轮模式：传 steps 数组，按顺序逐轮发送
    - 动态循环模式：传 loop=true + prompt，agent 回复含 [CONTINUE] 则继续下一轮，
      直到 agent 回复 [DONE] 或达到 max_rounds 上限
    """
    if req.bot_index >= len(cm.channels):
        return {"ok": False, "error": f"bot_index {req.bot_index} 超出范围"}
    ch = cm.channels[req.bot_index]
    chat_cfg = ch._get_chat_config(req.chatid)
    agent = chat_cfg.get("agent")
    cwd = chat_cfg.get("cwd", os.getenv("KIRO_WORK_DIR", "/mnt/i/workspace/alan_bot"))

    try:
        proc = await ch.pool.get_or_create(req.chatid, agent=agent, cwd=cwd, mode=req.mode)

        # ---- 动态循环模式 ----
        if req.loop:
            if not req.prompt:
                return {"ok": False, "error": "loop 模式必须提供 prompt"}
            reply = await proc.send(f"[cron]: {req.prompt}", timeout=req.timeout)
            rounds = 1
            while rounds < req.max_rounds:
                if reply.startswith("⚠️ 处理超时") or reply.startswith("❌"):
                    log.warning("cron loop 第 %d 轮异常 chatid=%s", rounds, req.chatid)
                    return {"ok": False, "error": f"第 {rounds} 轮异常: {reply[:200]}"}
                # 检查是否需要继续
                if req.done_tag in reply:
                    # 去掉标记，保留正文
                    reply = reply.replace(req.done_tag, "").strip()
                    break
                if req.continue_tag not in reply:
                    # 没有 continue 标记也没有 done 标记，视为自然结束
                    break
                # 继续下一轮：去掉标记，把回复作为上下文让 agent 继续
                log.info("cron loop chatid=%s 第 %d 轮完成，继续...", req.chatid, rounds)
                reply = await proc.send(
                    f"[cron-followup]: 继续执行。上一轮你的输出已在上下文中，请基于已有信息继续下一步研究。如果还需要继续搜索请在回复末尾加 {req.continue_tag}，如果已完成最终报告请在末尾加 {req.done_tag}",
                    timeout=req.timeout
                )
                rounds += 1
            log.info("cron loop chatid=%s 完成，共 %d 轮", req.chatid, rounds)
            # 推送最终结果
            final_reply = reply.replace(req.continue_tag, "").replace(req.done_tag, "").strip()
            chat_type = 1 if req.chatid.startswith("dm_") else 2
            await ch.ws.send_msg(req.chatid, chat_type, final_reply)
            return {"ok": True, "reply_length": len(final_reply), "rounds": rounds}

        # ---- 固定多轮模式 ----
        if req.steps:
            prev_reply = ""
            for i, step_prompt in enumerate(req.steps):
                actual_prompt = step_prompt.replace("{prev_reply}", prev_reply)
                prefix = "[cron]: " if i == 0 else "[cron-followup]: "
                reply = await proc.send(f"{prefix}{actual_prompt}", timeout=req.timeout)
                if reply.startswith("⚠️ 处理超时") or reply.startswith("❌"):
                    log.warning("cron steps 第 %d 轮异常 chatid=%s", i + 1, req.chatid)
                    return {"ok": False, "error": f"第 {i+1} 轮异常: {reply[:200]}"}
                prev_reply = reply
                log.info("cron steps chatid=%s 第 %d/%d 轮完成", req.chatid, i + 1, len(req.steps))
            chat_type = 1 if req.chatid.startswith("dm_") else 2
            await ch.ws.send_msg(req.chatid, chat_type, prev_reply)
            return {"ok": True, "reply_length": len(prev_reply), "rounds": len(req.steps)}

        # ---- 单轮模式 ----
        if not req.prompt:
            return {"ok": False, "error": "prompt 不能为空"}
        reply = await proc.send(f"[cron]: {req.prompt}", timeout=req.timeout)
        if reply.startswith("⚠️ 处理超时") or reply.startswith("❌"):
            log.warning("cron 任务异常 chatid=%s reply=%s", req.chatid, reply[:80])
            return {"ok": False, "error": reply}
        chat_type = 1 if req.chatid.startswith("dm_") else 2
        await ch.ws.send_msg(req.chatid, chat_type, reply)
        return {"ok": True, "reply_length": len(reply), "rounds": 1}

    except Exception as e:
        log.error("cron trigger 异常 chatid=%s: %s", req.chatid, e)
        return {"ok": False, "error": str(e)}



# ---- 定时任务调度 API ----

class JobCreateRequest(BaseModel):
    cron: str           # crontab 表达式，如 "0 9 * * *"
    chatid: str         # 目标 chatid
    prompt: str         # 要执行的 prompt
    bot_index: int = 0
    description: str = ""
    timeout: int = 300  # 执行超时秒数

class JobUpdateRequest(BaseModel):
    cron: Optional[str] = None
    chatid: Optional[str] = None
    prompt: Optional[str] = None
    bot_index: Optional[int] = None
    enabled: Optional[bool] = None
    description: Optional[str] = None
    timeout: Optional[int] = None

@app.post("/scheduler/jobs")
async def create_job(req: JobCreateRequest):
    job = scheduler.create_job(req.cron, req.chatid, req.prompt, req.bot_index, req.description, req.timeout)
    return {"ok": True, "job": job}

@app.get("/scheduler/jobs")
async def list_jobs():
    return {"ok": True, "jobs": scheduler.list_jobs()}

@app.get("/scheduler/jobs/{job_id}")
async def get_job(job_id: str):
    job = scheduler.get_job(job_id)
    if not job:
        return {"ok": False, "error": "job not found"}
    return {"ok": True, "job": job}

@app.patch("/scheduler/jobs/{job_id}")
async def update_job(job_id: str, req: JobUpdateRequest):
    updates = {k: v for k, v in req.dict().items() if v is not None}
    if "enabled" in updates:
        updates["enabled"] = int(updates["enabled"])
    job = scheduler.update_job(job_id, **updates)
    if not job:
        return {"ok": False, "error": "job not found"}
    return {"ok": True, "job": job}

@app.delete("/scheduler/jobs/{job_id}")
async def delete_job(job_id: str):
    if not scheduler.delete_job(job_id):
        return {"ok": False, "error": "job not found"}
    return {"ok": True}


# ---- 定时任务仪表盘 API ----

@app.get("/cron/dashboard")
async def cron_dashboard():
    """返回所有定时任务 + 最新执行结果（供企微主动查询）"""
    from cron_dashboard import build_report, format_markdown
    report = build_report()
    return {"ok": True, "report": report, "markdown": format_markdown(report)}


# ---- Agent 看板 ----

from fastapi.responses import HTMLResponse

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    """Agent 实时看板 HTML 页面"""
    html_path = os.path.join(os.path.dirname(__file__), "static", "dashboard.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/dashboard/api")
async def dashboard_api():
    """看板数据 JSON API"""
    from dashboard import get_full_dashboard
    return get_full_dashboard(cm)


# ---- 禅道 Bug 轮询控制 API ----

class ZentaoToggleRequest(BaseModel):
    enabled: bool
    chatid: str = "dm_Alan.Li"
    product_id: int = 11
    interval_seconds: int | None = None

@app.get("/zentao/status")
async def zentao_status():
    return {"ok": True, **zentao_poller.get_status()}

@app.post("/zentao/toggle")
async def zentao_toggle(req: ZentaoToggleRequest):
    if req.interval_seconds is not None:
        zentao_poller.set_interval(req.interval_seconds)
    if req.enabled:
        zentao_poller.enable(req.chatid, req.product_id)
    else:
        zentao_poller.disable()
    return {"ok": True, **zentao_poller.get_status()}

@app.post("/zentao/reset")
async def zentao_reset():
    zentao_poller.reset_seen()
    return {"ok": True, "msg": "seen_ids 已重置"}


# ---- Task Helper HTTP API (Teams 模式) ----

class AddTaskRequest(BaseModel):
    session_dir: str
    id: str
    title: str
    agent: str
    depends_on: list[str] = []
    gate: Optional[str] = None

class CompleteTaskRequest(BaseModel):
    session_dir: str
    id: str
    result: str

class FailTaskRequest(BaseModel):
    session_dir: str
    id: str
    error: str

class SendMailRequest(BaseModel):
    model_config = {"populate_by_name": True}
    session_dir: str
    to: str
    content: str
    from_agent: str = Field("", alias="from")


@app.post("/teams/add_task")
async def teams_add_task(req: AddTaskRequest):
    import time as _t
    task = {
        "id": req.id, "title": req.title, "agent": req.agent,
        "status": "pending", "assignee": None, "depends_on": req.depends_on,
        "gate": req.gate, "created_by": "lead", "created_at": int(_t.time()),
        "started_at": None, "finished_at": None, "result": None,
    }
    TaskList(req.session_dir).add_task(task)
    return {"ok": True, "id": req.id}


@app.post("/teams/complete_task")
async def teams_complete_task(req: CompleteTaskRequest):
    TaskList(req.session_dir).complete(req.id, req.result)
    return {"ok": True, "id": req.id}


@app.post("/teams/fail_task")
async def teams_fail_task(req: FailTaskRequest):
    TaskList(req.session_dir).fail(req.id, req.error)
    return {"ok": True, "id": req.id}


@app.post("/teams/send_mail")
async def teams_send_mail(req: SendMailRequest):
    Mailbox(req.session_dir).send(req.from_agent, req.to, req.content)
    return {"ok": True}


# ---- 辩论式 Code Review API ----

class DebateStartRequest(BaseModel):
    chatid: str = "dm_Alan.Li"
    topic: str = ""              # 辩论主题（通用，任意话题）
    diff_text: str = ""          # Code Review 专用：直接传 diff 文本
    pr_url: str = ""             # Code Review 专用：PR 链接
    branch: str = ""             # Code Review 专用：分支名
    repo: str = ""               # 配合 branch 使用的仓库名
    context: str = ""            # 额外上下文
    preset: str = "free"         # 预设场景: code-review/tech-decision/requirement/security/free
    bot_index: int = 0


class DebateStopRequest(BaseModel):
    chatid: str = "dm_Alan.Li"
    bot_index: int = 0


@app.post("/debate/start")
async def debate_start(req: DebateStartRequest):
    """启动辩论（支持多种场景）"""
    if req.bot_index >= len(cm.channels):
        return {"ok": False, "error": f"bot_index {req.bot_index} 超出范围"}
    ch = cm.channels[req.bot_index]
    chat_cfg = ch._get_chat_config(req.chatid)
    from agents.debate.session import DebateSession
    session = ch._get_debate(req.chatid, chat_cfg)
    if session.running:
        return {"ok": False, "error": "辩论正在进行中，请先停止当前辩论"}

    # 确定辩论内容
    topic = req.topic
    context = req.context
    preset = req.preset

    # Code Review 场景：获取 diff 作为 topic
    if not topic and (req.diff_text or req.pr_url or req.branch):
        preset = "code-review"
        topic = req.diff_text

        if not topic and req.pr_url:
            import re, subprocess
            m = re.match(r'https://github\.com/([^/]+)/([^/]+)/pull/(\d+)', req.pr_url)
            if not m:
                return {"ok": False, "error": "无法解析 PR URL"}
            owner, repo, number = m.group(1), m.group(2), m.group(3)
            try:
                result = subprocess.run(
                    ["/mnt/i/workspace/kiro-wecom-bridge/.venv/bin/python3",
                     "/mnt/i/workspace/kiro-wecom-bridge/github_cli.py",
                     "get_pr_diff", f'{{"owner":"{owner}","repo":"{repo}","number":{number}}}'],
                    capture_output=True, text=True, timeout=30)
                topic = result.stdout.strip()
                result2 = subprocess.run(
                    ["/mnt/i/workspace/kiro-wecom-bridge/.venv/bin/python3",
                     "/mnt/i/workspace/kiro-wecom-bridge/github_cli.py",
                     "get_pr", f'{{"owner":"{owner}","repo":"{repo}","number":{number}}}'],
                    capture_output=True, text=True, timeout=15)
                if result2.stdout.strip():
                    context = result2.stdout.strip()
            except Exception as e:
                return {"ok": False, "error": f"获取 PR diff 失败: {e}"}

        if not topic and req.branch and req.repo:
            import subprocess
            frontend_repos = (
                "ec-website-nb", "ec-website-next", "ec-website-customer-nb",
                "ec-website-customer-next", "ec-website-trade-nb", "ec-mobilesite-nb",
                "ec-mobilesite-ssr", "ec-mobilesite-rma", "mobile_flutter",
            )
            repo_path = f"/mnt/c/Alan/workspace/{req.repo}" if req.repo in frontend_repos else f"/mnt/i/workspace/{req.repo}"
            try:
                result = subprocess.run(
                    ["git", "diff", f"master...{req.branch}"],
                    capture_output=True, text=True, timeout=30, cwd=repo_path)
                topic = result.stdout.strip()
                context = f"仓库: {req.repo}\n分支: {req.branch} vs master"
            except Exception as e:
                return {"ok": False, "error": f"获取分支 diff 失败: {e}"}

    if not topic:
        return {"ok": False, "error": "请提供 topic（辩论主题）或 diff_text/pr_url/branch"}

    try:
        await session.start_debate(topic, preset=preset, context=context)
        return {"ok": True, "message": f"{preset} 辩论已启动"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/debate/stop")
async def debate_stop(req: DebateStopRequest):
    """停止辩论并生成汇总"""
    if req.bot_index >= len(cm.channels):
        return {"ok": False, "error": f"bot_index {req.bot_index} 超出范围"}
    ch = cm.channels[req.bot_index]
    try:
        summary = await ch.stop_debate_review(req.chatid)
        return {"ok": True, "summary": summary}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---- 分支逐个 Review API ----

class BranchReviewRequest(BaseModel):
    chatid: str = "dm_Alan.Li"
    bot_index: int = 0


@app.post("/cron/branch-review")
async def branch_review_start(req: BranchReviewRequest):
    """启动分支逐个 Review（高消息量版）"""
    if req.bot_index >= len(cm.channels):
        return {"ok": False, "error": f"bot_index {req.bot_index} 超出范围"}
    ch = cm.channels[req.bot_index]
    from agents.branch_reviewer import run_branch_review
    asyncio.create_task(run_branch_review(req.chatid, ch.pool, ch.ws))
    return {"ok": True, "message": "分支 Review 已启动（后台执行）"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8900")))