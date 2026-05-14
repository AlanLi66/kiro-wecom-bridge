"""DebateSession — 通用多 Agent 辩论模式

支持任意话题的结构化辩论，预设多种场景模板，也支持自定义角色。

场景示例：
- Code Review: 严格派 vs 务实派
- 技术方案决策: 激进派 vs 保守派
- 需求评审: 产品视角 vs 开发视角
- 自由辩论: 正方 vs 反方（任意话题）

每轮发言实时推送企微，用户可随时插话、追问、或喊停。
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

# 默认最大辩论轮次
MAX_ROUNDS = 100

# ============================================================
# 预设场景模板
# ============================================================

PRESETS = {
    "code-review": {
        "name": "Code Review",
        "icon_a": "🔴", "icon_b": "🔵",
        "name_a": "严格审查员", "name_b": "务实审查员",
        "system_a": (
            "你是 **严格审查员**，代号 🔴A。\n\n"
            "审查风格：\n"
            "- 严格遵循编码规范，不放过任何潜在问题\n"
            "- 重点关注：安全漏洞、SQL注入、NPE、并发问题、N+1查询、硬编码、缺少注释\n"
            "- 对可疑代码宁可误报也不漏报\n"
            "- 用具体的代码行号和片段说明问题\n"
        ),
        "system_b": (
            "你是 **务实审查员**，代号 🔵B。\n\n"
            "审查风格：\n"
            "- 关注代码的整体设计和可维护性\n"
            "- 重点关注：架构合理性、代码可读性、重复代码、过度设计、测试覆盖\n"
            "- 对严格派提出的问题会评估实际影响，区分「理论风险」和「实际问题」\n"
            "- 如果某个问题在当前上下文中不是真正的风险，会指出原因\n"
        ),
        "summary_prompt": (
            "请根据辩论记录生成 Code Review 汇总：\n\n"
            "## ✅ 双方共识（必须修改）\n"
            "## ⚠️ 建议修改\n"
            "## 💬 有争议（供参考）\n"
            "## 📊 总评（评分1-10 + 一句话总结）"
        ),
    },
    "tech-decision": {
        "name": "技术方案决策",
        "icon_a": "🚀", "icon_b": "🛡️",
        "name_a": "激进派", "name_b": "保守派",
        "system_a": (
            "你是 **激进派架构师**，代号 🚀A。\n\n"
            "你的立场：\n"
            "- 倾向于采用新技术、新架构，追求长期收益\n"
            "- 关注：可扩展性、技术先进性、开发效率提升、未来维护成本\n"
            "- 愿意承担短期的迁移成本和学习曲线\n"
            "- 用数据和案例支撑你的观点\n"
        ),
        "system_b": (
            "你是 **保守派架构师**，代号 🛡️B。\n\n"
            "你的立场：\n"
            "- 倾向于稳定可靠的方案，规避风险\n"
            "- 关注：稳定性、团队熟悉度、迁移成本、时间压力、回滚方案\n"
            "- 质疑新方案的必要性，要求证明现有方案不够用\n"
            "- 用实际项目经验和失败案例来论证\n"
        ),
        "summary_prompt": (
            "请根据辩论记录生成技术决策汇总：\n\n"
            "## 🎯 核心分歧\n"
            "## ✅ 双方共识\n"
            "## 📊 方案对比表（维度：成本/收益/风险/时间）\n"
            "## 💡 建议决策（附理由）"
        ),
    },
    "requirement": {
        "name": "需求评审",
        "icon_a": "📋", "icon_b": "💻",
        "name_a": "产品经理", "name_b": "技术负责人",
        "system_a": (
            "你是 **产品经理**，代号 📋A。\n\n"
            "你的立场：\n"
            "- 从用户价值和业务目标出发论证需求合理性\n"
            "- 关注：用户体验、业务指标、竞品对比、优先级\n"
            "- 对技术方提出的「做不了」会追问是真的做不了还是不想做\n"
            "- 愿意在范围上妥协，但坚持核心价值\n"
        ),
        "system_b": (
            "你是 **技术负责人**，代号 💻B。\n\n"
            "你的立场：\n"
            "- 从技术可行性和工程成本出发评估需求\n"
            "- 关注：实现复杂度、系统影响面、工期、技术债务、边界case\n"
            "- 对需求中模糊的部分会追问细节和边界条件\n"
            "- 提出分期实现或简化方案作为替代\n"
        ),
        "summary_prompt": (
            "请根据辩论记录生成需求评审汇总：\n\n"
            "## ✅ 可直接实施\n"
            "## ⚠️ 需要细化/调整\n"
            "## ❌ 建议砍掉或延后\n"
            "## 📅 建议排期和分期方案"
        ),
    },
    "security": {
        "name": "安全攻防",
        "icon_a": "🗡️", "icon_b": "🛡️",
        "name_a": "攻击者", "name_b": "防御者",
        "system_a": (
            "你是 **安全攻击者（Red Team）**，代号 🗡️A。\n\n"
            "你的任务：\n"
            "- 从攻击者视角寻找系统漏洞和攻击面\n"
            "- 关注：注入攻击、认证绕过、权限提升、数据泄露、SSRF、XSS\n"
            "- 构造具体的攻击场景和 PoC 思路\n"
            "- 评估漏洞的实际可利用性和影响范围\n"
        ),
        "system_b": (
            "你是 **安全防御者（Blue Team）**，代号 🛡️B。\n\n"
            "你的任务：\n"
            "- 评估攻击者提出的威胁是否在当前架构下可行\n"
            "- 指出已有的防护措施（WAF、参数校验、权限控制等）\n"
            "- 对确实存在的风险提出修复方案\n"
            "- 区分理论攻击和实际可利用的漏洞\n"
        ),
        "summary_prompt": (
            "请根据辩论记录生成安全评估报告：\n\n"
            "## 🚨 确认的安全风险（需立即修复）\n"
            "## ⚠️ 潜在风险（建议加固）\n"
            "## ✅ 已有防护确认有效\n"
            "## 📋 修复优先级排序"
        ),
    },
    "free": {
        "name": "自由辩论",
        "icon_a": "🟠", "icon_b": "🟣",
        "name_a": "正方", "name_b": "反方",
        "system_a": (
            "你是 **正方辩手**，代号 🟠A。\n\n"
            "规则：\n"
            "- 支持论题中的正面立场\n"
            "- 用逻辑、数据、案例支撑你的观点\n"
            "- 对反方的论点进行有理有据的反驳\n"
            "- 承认对方有道理的部分，但坚持核心立场\n"
            "- 保持理性和尊重，不人身攻击\n"
        ),
        "system_b": (
            "你是 **反方辩手**，代号 🟣B。\n\n"
            "规则：\n"
            "- 反对论题中的正面立场，提出质疑和替代观点\n"
            "- 用逻辑、数据、案例支撑你的观点\n"
            "- 指出正方论证中的漏洞和假设\n"
            "- 承认对方有道理的部分，但坚持核心立场\n"
            "- 保持理性和尊重，不人身攻击\n"
        ),
        "summary_prompt": (
            "请根据辩论记录生成总结：\n\n"
            "## 🟠 正方核心论点\n"
            "## 🟣 反方核心论点\n"
            "## ✅ 双方共识\n"
            "## 💬 核心分歧\n"
            "## 🎯 综合结论（基于论据质量给出倾向性判断）"
        ),
    },
}

# 通用回复格式指引（附加到所有 system prompt）
REPLY_RULES = """
回复规则：
1. 每次发言不超过 800 字
2. 先回应对方最新观点（同意标记 [共识]，不同意给出理由）
3. 再补充自己的新观点或新发现
4. 如果用户插话了，优先回应用户的问题
5. 如果所有问题都已讨论清楚，说「辩论结束，以上为最终意见」
"""


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

    def format_for_prompt(self, last_n: int = 20) -> str:
        """格式化为 prompt 可用的文本，只取最近 N 条避免超长"""
        entries = self.read_all()
        if len(entries) > last_n:
            entries = entries[-last_n:]
        lines = []
        for e in entries:
            lines.append(f"[{e['speaker']}]: {e['content']}")
        return "\n\n---\n\n".join(lines)

    def format_full(self) -> str:
        """格式化完整记录（用于最终汇总）"""
        entries = self.read_all()
        lines = []
        for e in entries:
            lines.append(f"[{e['speaker']}]: {e['content']}")
        return "\n\n---\n\n".join(lines)

    def turn_count(self) -> int:
        """返回发言总数"""
        return len(self.read_all())

    def clear(self):
        """清空日志"""
        if os.path.isfile(self._path):
            os.remove(self._path)


class DebateSession:
    """通用辩论会话"""

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
        self._preset: dict | None = None

    @property
    def running(self) -> bool:
        return self._running

    async def start_debate(self, topic: str, preset: str = "free",
                           context: str = "", custom_roles: dict | None = None):
        """启动辩论

        Args:
            topic: 辩论主题/内容（可以是代码diff、技术方案、任意问题）
            preset: 预设场景 (code-review/tech-decision/requirement/security/free)
            context: 额外上下文
            custom_roles: 自定义角色 {"name_a": "", "name_b": "", "system_a": "", "system_b": ""}
        """
        if self._running:
            return

        self._running = True
        self._stop_event.clear()
        self._debate_log.clear()

        # 确定角色配置
        if custom_roles:
            self._preset = {
                "name": "自定义辩论",
                "icon_a": custom_roles.get("icon_a", "🟠"),
                "icon_b": custom_roles.get("icon_b", "🟣"),
                "name_a": custom_roles.get("name_a", "角色A"),
                "name_b": custom_roles.get("name_b", "角色B"),
                "system_a": custom_roles.get("system_a", ""),
                "system_b": custom_roles.get("system_b", ""),
                "summary_prompt": custom_roles.get("summary_prompt",
                    PRESETS["free"]["summary_prompt"]),
            }
        else:
            self._preset = PRESETS.get(preset, PRESETS["free"])

        # 启动两个 agent 进程
        await self._start_agents()

        # 后台运行辩论循环
        self._debate_task = asyncio.create_task(
            self._debate_loop(topic, context))

    async def stop_debate(self) -> str:
        """用户喊停，生成当前汇总"""
        self._stop_event.set()
        if self._debate_task:
            try:
                await asyncio.wait_for(self._debate_task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        summary = await self._generate_summary()
        self._running = False
        await self._cleanup()
        return summary

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

        log.info("Debate agents 启动 chatid=%s preset=%s", self._chatid, self._preset["name"])

    async def _debate_loop(self, topic: str, context: str):
        """辩论主循环"""
        p = self._preset
        try:
            await self._push_msg(
                f"🎯 **{p['name']}开始**\n"
                f"- {p['icon_a']}A: {p['name_a']}\n"
                f"- {p['icon_b']}B: {p['name_b']}\n"
                f"- 最大轮次: {self._max_rounds}\n"
                f"- 随时发消息插话，发「停止辩论」结束\n\n"
                f"---")

            # === 第一轮 ===
            round_num = 1

            # Agent-A 首轮
            a_prompt = self._build_first_prompt("A", topic, context)
            a_reply = await self._agent_a.send(a_prompt, timeout=180)
            if self._stop_event.is_set():
                return
            self._debate_log.append(f"{p['icon_a']}A", a_reply)
            await self._push_msg(f"{p['icon_a']} **{p['name_a']} (Round {round_num})**:\n\n{a_reply}")

            # Agent-B 首轮
            b_prompt = self._build_first_prompt("B", topic, context, prev_opinion=a_reply)
            b_reply = await self._agent_b.send(b_prompt, timeout=180)
            if self._stop_event.is_set():
                return
            self._debate_log.append(f"{p['icon_b']}B", b_reply)
            await self._push_msg(f"{p['icon_b']} **{p['name_b']} (Round {round_num})**:\n\n{b_reply}")

            # === 后续轮次 ===
            for round_num in range(2, self._max_rounds + 1):
                if self._stop_event.is_set():
                    break

                await asyncio.sleep(2)

                # Agent-A 回应
                a_prompt = self._build_reply_prompt("A", round_num)
                a_reply = await self._agent_a.send(a_prompt, timeout=120)
                if self._stop_event.is_set():
                    break
                self._debate_log.append(f"{p['icon_a']}A", a_reply)
                await self._push_msg(f"{p['icon_a']} **{p['name_a']} (Round {round_num})**:\n\n{a_reply}")

                # 检查是否自然结束
                if self._check_end(a_reply):
                    await self._push_msg("💡 辩论自然收敛，进入汇总。")
                    break

                await asyncio.sleep(2)

                # Agent-B 回应
                b_prompt = self._build_reply_prompt("B", round_num)
                b_reply = await self._agent_b.send(b_prompt, timeout=120)
                if self._stop_event.is_set():
                    break
                self._debate_log.append(f"{p['icon_b']}B", b_reply)
                await self._push_msg(f"{p['icon_b']} **{p['name_b']} (Round {round_num})**:\n\n{b_reply}")

                if self._check_end(b_reply):
                    await self._push_msg("💡 辩论自然收敛，进入汇总。")
                    break

            # 生成汇总
            summary = await self._generate_summary()
            await self._push_msg(f"📋 **汇总报告**\n\n{summary}")

        except Exception as e:
            log.error("Debate loop 异常 chatid=%s: %s", self._chatid, e)
            await self._push_msg(f"❌ 辩论异常中断: {e}")
        finally:
            self._running = False
            await self._cleanup()

    def _check_end(self, reply: str) -> bool:
        """检查是否自然结束（共识达成或明确表示结束）"""
        if reply.count("[共识]") >= 3:
            return True
        end_phrases = ["辩论结束", "以上为最终意见", "没有更多补充", "讨论可以结束"]
        return any(phrase in reply for phrase in end_phrases)

    def _build_first_prompt(self, agent_id: str, topic: str,
                            context: str, prev_opinion: str = "") -> str:
        """构建首轮 prompt"""
        p = self._preset
        system = p["system_a"] if agent_id == "A" else p["system_b"]
        other_name = p["name_b"] if agent_id == "A" else p["name_a"]
        other_icon = p["icon_b"] if agent_id == "A" else p["icon_a"]

        parts = [system + REPLY_RULES, "\n\n---\n\n"]

        if context:
            parts.append(f"## 背景信息\n{context}\n\n")

        # 主题内容（可能是 diff、方案描述、或任意问题）
        # 超长截断
        display_topic = topic
        if len(display_topic) > 15000:
            display_topic = display_topic[:15000] + "\n\n... (内容过长已截断)"

        parts.append(f"## 辩论主题\n{display_topic}\n\n")

        if prev_opinion:
            parts.append(f"## 对方（{other_icon}{other_name}）的观点\n{prev_opinion}\n\n")
            parts.append("请先发表你自己的观点，然后回应对方。")
        else:
            parts.append("请就以上主题发表你的观点和分析。")

        return "".join(parts)

    def _build_reply_prompt(self, agent_id: str, round_num: int) -> str:
        """构建后续轮次的 prompt"""
        p = self._preset
        system = p["system_a"] if agent_id == "A" else p["system_b"]
        other_name = p["name_b"] if agent_id == "A" else p["name_a"]
        other_icon = p["icon_b"] if agent_id == "A" else p["icon_a"]

        # 只取最近的对话记录避免 context 爆炸
        debate_history = self._debate_log.format_for_prompt(last_n=10)

        return (
            f"{system}{REPLY_RULES}\n\n"
            f"---\n\n"
            f"## 辩论记录（第 {round_num} 轮，显示最近发言）\n\n"
            f"{debate_history}\n\n"
            f"---\n\n"
            f"这是第 {round_num} 轮。请：\n"
            f"1. 回应 {other_icon}{other_name} 最新的观点\n"
            f"2. 如果用户有插话，优先回应用户\n"
            f"3. 补充新的论点或证据\n"
            f"4. 达成共识的点标记 [共识]"
        )

    async def _generate_summary(self) -> str:
        """生成汇总报告"""
        debate_log = self._debate_log.format_full()
        if not debate_log:
            return "无辩论记录"

        # 如果辩论记录太长，只取关键部分
        if len(debate_log) > 20000:
            debate_log = debate_log[:20000] + "\n\n...(记录过长已截断)"

        summary_template = self._preset.get("summary_prompt", PRESETS["free"]["summary_prompt"])
        prompt = (
            f"请根据以下辩论记录生成汇总报告。\n\n"
            f"{summary_template}\n\n"
            f"---\n辩论记录：\n{debate_log}"
        )

        try:
            if self._agent_a and self._agent_a.alive:
                return await self._agent_a.send(prompt, timeout=90)
        except Exception as e:
            log.error("生成汇总失败: %s", e)

        return f"（汇总生成失败，原始记录已保存在 {self._session_dir}/debate_log.jsonl）"

    async def _push_msg(self, text: str):
        """推送消息到企微"""
        chat_type = 1 if self._chatid.startswith("dm_") else 2
        if len(text) <= 1500:
            try:
                await self._ws.send_msg(self._chatid, chat_type, text)
            except Exception as e:
                log.error("推送企微失败: %s", e)
        else:
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
