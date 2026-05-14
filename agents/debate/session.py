"""DebateSession — 两个 Agent 辩论式 Code Review

流程：
1. 用户提供 PR 链接或分支 diff
2. Agent-A（严格派）先发表审查意见
3. Agent-B（务实派）回应 A 的意见，补充自己的发现
4. 轮流辩论 N 轮
5. 最终汇总双方共识 + 争议点

每轮发言实时推送企微，用户可随时插话或喊停。
"""
import asyncio
import json
import logging
import os
import time

from agents.process import KiroProcess

log = logging.getLogger(__name__)

WORK_DIR = os.getenv("KIRO_WORK_DIR", "/mnt/i/workspace/alan_bot")
SESSIONS_DIR = os.path.join(WORK_DIR, "wecom-sessions")

# 默认最大辩论轮次（每个 agent 发言一次算一轮）
MAX_ROUNDS = 4

# Agent-A: 严格派，关注安全、规范、潜在 bug
AGENT_A_SYSTEM = """你是 **严格审查员（Strict Reviewer）**，代号 🔴A。

你的审查风格：
- 严格遵循编码规范，不放过任何潜在问题
- 重点关注：安全漏洞、SQL注入、NPE、并发问题、N+1查询、硬编码、缺少注释
- 对可疑代码宁可误报也不漏报
- 用具体的代码行号和片段说明问题

回复格式：
1. 先列出你发现的问题（按严重程度排序：🚨严重 / ⚠️警告 / 💡建议）
2. 对对方（🔵B）提出的观点进行回应（同意/反驳/补充）
3. 如果认为某个问题已经讨论清楚，标记 [共识]

保持简洁，每次发言不超过 800 字。"""

# Agent-B: 务实派，关注可维护性、设计、实际影响
AGENT_B_SYSTEM = """你是 **务实审查员（Pragmatic Reviewer）**，代号 🔵B。

你的审查风格：
- 关注代码的整体设计和可维护性
- 重点关注：架构合理性、代码可读性、重复代码、过度设计、测试覆盖
- 对严格派提出的问题会评估实际影响，区分"理论风险"和"实际问题"
- 如果某个问题在当前上下文中不是真正的风险，会指出原因

回复格式：
1. 先列出你发现的问题（按实际影响排序：🚨必须修 / ⚠️建议修 / 💡可以优化）
2. 对对方（🔴A）提出的观点进行回应（同意/反驳/补充）
3. 如果认为某个问题已经讨论清楚，标记 [共识]

保持简洁，每次发言不超过 800 字。"""

# 汇总 prompt
SUMMARY_PROMPT = """请根据以下辩论记录，生成最终的 Code Review 汇总报告。

格式：
## ✅ 双方共识（必须修改）
- 列出双方都认同需要修改的问题

## ⚠️ 建议修改
- 列出至少一方认为应该修改，且另一方未强烈反对的问题

## 💬 有争议（供参考）
- 列出双方意见不一致的点，附上各自理由

## 📊 总评
- 代码质量评分（1-10）
- 一句话总结

---
辩论记录：
{debate_log}"""


class DebateLog:
    """辩论日志，JSONL 存储"""

    def __init__(self, session_dir: str):
        self._path = os.path.join(session_dir, "debate_log.jsonl")
        os.makedirs(session_dir, exist_ok=True)

    def append(self, speaker: str, content: str):
        """追加一条发言"""
        entry = {
            "speaker": speaker,
            "content": content,
            "ts": int(time.time()),
        }
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def read_all(self) -> list[dict]:
        """读取所有发言"""
        if not os.path.isfile(self._path):
            return []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]
        except Exception:
            return []

    def format_for_prompt(self) -> str:
        """格式化为 prompt 可用的文本"""
        entries = self.read_all()
        lines = []
        for e in entries:
            lines.append(f"[{e['speaker']}]: {e['content']}")
        return "\n\n---\n\n".join(lines)

    def clear(self):
        """清空日志"""
        if os.path.isfile(self._path):
            os.remove(self._path)


class DebateSession:
    """辩论式 Code Review 会话"""

    def __init__(self, chatid: str, chat_config: dict, ws):
        self._chatid = chatid
        self._config = chat_config
        self._ws = ws
        self._cwd = chat_config.get("cwd", WORK_DIR)
        self._mode = chat_config.get("mode", "full")
        self._session_dir = os.path.join(SESSIONS_DIR, chatid, "_debate")
        self._max_rounds = chat_config.get("debate_rounds", MAX_ROUNDS)

        self._agent_a: KiroProcess | None = None
        self._agent_b: KiroProcess | None = None
        self._debate_log = DebateLog(self._session_dir)
        self._running = False
        self._debate_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._running

    async def start_debate(self, diff_text: str, context: str = ""):
        """启动辩论流程

        Args:
            diff_text: PR diff 或分支 diff 内容
            context: 额外上下文（PR 标题、描述等）
        """
        if self._running:
            return

        self._running = True
        self._stop_event.clear()
        self._debate_log.clear()

        # 启动两个 agent 进程
        await self._start_agents()

        # 后台运行辩论循环
        self._debate_task = asyncio.create_task(
            self._debate_loop(diff_text, context))

    async def stop_debate(self) -> str:
        """用户喊停，生成当前汇总"""
        self._stop_event.set()
        if self._debate_task:
            try:
                await asyncio.wait_for(self._debate_task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        return await self._generate_summary()

    async def inject_human(self, text: str):
        """用户插话，写入辩论日志，下一轮 agent 会看到"""
        self._debate_log.append("👤用户", text)
        await self._push_msg(f"👤 **用户插话**: {text}")

    # ---- 内部方法 ----

    async def _start_agents(self):
        """启动两个 agent 进程"""
        dir_a = os.path.join(self._session_dir, "agent_a")
        dir_b = os.path.join(self._session_dir, "agent_b")

        self._agent_a = KiroProcess(
            f"{self._chatid}/debate_a", dir_a,
            agent=None, cwd=self._cwd,
            mode="readonly", interruptible=False)
        await self._agent_a.start()

        self._agent_b = KiroProcess(
            f"{self._chatid}/debate_b", dir_b,
            agent=None, cwd=self._cwd,
            mode="readonly", interruptible=False)
        await self._agent_b.start()

        log.info("Debate agents 启动 chatid=%s", self._chatid)

    async def _debate_loop(self, diff_text: str, context: str):
        """辩论主循环"""
        try:
            # 第一轮：Agent-A 先审查
            round_num = 1
            await self._push_msg(
                f"🎯 **辩论式 Code Review 开始**\n"
                f"- 🔴A: 严格审查员\n"
                f"- 🔵B: 务实审查员\n"
                f"- 最大轮次: {self._max_rounds}\n"
                f"- 你可以随时发消息插话或发「停止辩论」结束\n\n"
                f"---")

            # Agent-A 首轮
            a_prompt = self._build_first_prompt("A", diff_text, context)
            a_reply = await self._agent_a.send(a_prompt, timeout=180)
            if self._stop_event.is_set():
                return
            self._debate_log.append("🔴A", a_reply)
            await self._push_msg(f"🔴 **严格审查员 (Round {round_num})**:\n\n{a_reply}")

            # Agent-B 首轮
            b_prompt = self._build_first_prompt("B", diff_text, context, prev_opinion=a_reply)
            b_reply = await self._agent_b.send(b_prompt, timeout=180)
            if self._stop_event.is_set():
                return
            self._debate_log.append("🔵B", b_reply)
            await self._push_msg(f"🔵 **务实审查员 (Round {round_num})**:\n\n{b_reply}")

            # 后续轮次
            for round_num in range(2, self._max_rounds + 1):
                if self._stop_event.is_set():
                    break

                await asyncio.sleep(2)  # 短暂间隔，避免刷屏

                # Agent-A 回应
                a_prompt = self._build_reply_prompt("A", round_num)
                a_reply = await self._agent_a.send(a_prompt, timeout=120)
                if self._stop_event.is_set():
                    break
                self._debate_log.append("🔴A", a_reply)
                await self._push_msg(f"🔴 **严格审查员 (Round {round_num})**:\n\n{a_reply}")

                # 检查是否达成共识（简单启发式：回复中包含大量 [共识] 标记）
                if a_reply.count("[共识]") >= 3:
                    await self._push_msg("💡 大部分问题已达成共识，提前结束辩论。")
                    break

                await asyncio.sleep(2)

                # Agent-B 回应
                b_prompt = self._build_reply_prompt("B", round_num)
                b_reply = await self._agent_b.send(b_prompt, timeout=120)
                if self._stop_event.is_set():
                    break
                self._debate_log.append("🔵B", b_reply)
                await self._push_msg(f"🔵 **务实审查员 (Round {round_num})**:\n\n{b_reply}")

                if b_reply.count("[共识]") >= 3:
                    await self._push_msg("💡 大部分问题已达成共识，提前结束辩论。")
                    break

            # 生成汇总
            summary = await self._generate_summary()
            await self._push_msg(f"📋 **Review 汇总报告**\n\n{summary}")

        except Exception as e:
            log.error("Debate loop 异常 chatid=%s: %s", self._chatid, e)
            await self._push_msg(f"❌ 辩论异常中断: {e}")
        finally:
            self._running = False
            await self._cleanup()

    def _build_first_prompt(self, agent_id: str, diff_text: str,
                            context: str, prev_opinion: str = "") -> str:
        """构建首轮 prompt"""
        system = AGENT_A_SYSTEM if agent_id == "A" else AGENT_B_SYSTEM
        parts = [system, "\n\n---\n\n"]

        if context:
            parts.append(f"## PR 信息\n{context}\n\n")

        # diff 可能很长，截断到合理长度
        if len(diff_text) > 15000:
            diff_text = diff_text[:15000] + "\n\n... (diff 过长已截断，请基于可见部分审查)"

        parts.append(f"## 代码变更 (diff)\n```\n{diff_text}\n```\n\n")

        if prev_opinion:
            parts.append(f"## 对方（🔴A）的审查意见\n{prev_opinion}\n\n")
            parts.append("请先发表你自己的审查意见，然后回应对方的观点。")
        else:
            parts.append("请对以上代码变更进行审查，发表你的意见。")

        return "".join(parts)

    def _build_reply_prompt(self, agent_id: str, round_num: int) -> str:
        """构建后续轮次的 prompt"""
        debate_history = self._debate_log.format_for_prompt()
        system = AGENT_A_SYSTEM if agent_id == "A" else AGENT_B_SYSTEM
        other = "🔵B" if agent_id == "A" else "🔴A"

        return (
            f"{system}\n\n"
            f"---\n\n"
            f"## 辩论记录（第 {round_num} 轮）\n\n"
            f"{debate_history}\n\n"
            f"---\n\n"
            f"这是第 {round_num} 轮辩论。请：\n"
            f"1. 回应 {other} 最新的观点（同意标记 [共识]，不同意给出理由）\n"
            f"2. 补充新发现的问题（如果有）\n"
            f"3. 如果所有问题都已讨论清楚，可以说「辩论结束，以上为最终意见」"
        )

    async def _generate_summary(self) -> str:
        """用 Agent-A 生成汇总（复用已有进程）"""
        debate_log = self._debate_log.format_for_prompt()
        if not debate_log:
            return "无辩论记录"

        prompt = SUMMARY_PROMPT.format(debate_log=debate_log)
        try:
            if self._agent_a and self._agent_a.alive:
                return await self._agent_a.send(prompt, timeout=60)
        except Exception as e:
            log.error("生成汇总失败: %s", e)

        return f"（汇总生成失败，原始辩论记录已保存在 {self._session_dir}/debate_log.jsonl）"

    async def _push_msg(self, text: str):
        """推送消息到企微"""
        chat_type = 1 if self._chatid.startswith("dm_") else 2
        # 企微消息长度限制，超长分段发送
        if len(text) <= 1500:
            try:
                await self._ws.send_msg(self._chatid, chat_type, text)
            except Exception as e:
                log.error("推送企微失败: %s", e)
        else:
            # 分段发送
            chunks = [text[i:i+1400] for i in range(0, len(text), 1400)]
            for chunk in chunks:
                try:
                    await self._ws.send_msg(self._chatid, chat_type, chunk)
                    await asyncio.sleep(1)
                except Exception as e:
                    log.error("推送企微失败: %s", e)

    async def _cleanup(self):
        """清理 agent 进程"""
        if self._agent_a:
            try:
                await self._agent_a.stop()
            except Exception:
                pass
            self._agent_a = None
        if self._agent_b:
            try:
                await self._agent_b.stop()
            except Exception:
                pass
            self._agent_b = None
        log.info("Debate session 清理完成 chatid=%s", self._chatid)
