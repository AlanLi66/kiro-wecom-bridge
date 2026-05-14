"""DebateSession — 通用多 Agent 辩论模式（v2: 场景差异化策略）

核心改进：
1. 差异化场景策略 — 每种场景有独立的收敛规则和辩论引导
2. 自由辩论杠精模式 — 前10轮禁止认同，跨学科发散
3. 强制收敛质量检测 — 结束时必须输出共识/分歧/建议三段式

场景：
- code-review: 严格派 vs 务实派（禁止在安全问题上妥协）
- tech-decision: 激进派 vs 保守派（量化 Cost vs Benefit）
- requirement: 产品 vs 技术（引入资源约束，3轮僵持则强制输出矛盾点）
- security: 攻击者 vs 防御者（取消共识收敛，穷举攻击路径）
- free: 正方 vs 反方（杠精模式，跨学科发散，语义熵检测）
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

MAX_ROUNDS = 100

# ============================================================
# 预设场景模板（含差异化收敛策略）
# ============================================================

PRESETS = {
    "code-review": {
        "name": "Code Review",
        "icon_a": "\U0001f534", "icon_b": "\U0001f535",
        "name_a": "严格审查员", "name_b": "务实审查员",
        "system_a": (
            "你是 **严格审查员**，代号 \U0001f534A。\n\n"
            "审查风格：\n"
            "- 严格遵循编码规范，不放过任何潜在问题\n"
            "- 重点关注：安全漏洞、SQL注入、NPE、并发问题、N+1查询、硬编码、缺少注释\n"
            "- 对可疑代码宁可误报也不漏报\n"
            "- 用具体的代码行号和片段说明问题\n"
            "- 当你提出 Critical 级别问题时，必须要求对方给出具体修复方案，不接受「影响不大」的敷衍\n"
        ),
        "system_b": (
            "你是 **务实审查员**，代号 \U0001f535B。\n\n"
            "审查风格：\n"
            "- 关注代码的整体设计和可维护性\n"
            "- 重点关注：架构合理性、代码可读性、重复代码、过度设计、测试覆盖\n"
            "- 对严格派提出的问题会评估实际影响，区分「理论风险」和「实际问题」\n"
            "- 如果对方提出了安全/性能类 Critical 问题，你必须给出具体的补救方案或承认问题存在，不能空谈「影响可控」\n"
        ),
        # 收敛策略：禁止在存在未解决的 Critical 问题时结束
        "convergence": "strict",
        "min_rounds": 3,
        "summary_prompt": (
            "请根据辩论记录生成 Code Review 汇总：\n\n"
            "## [达成共识] 双方确认需要修改的问题\n"
            "## [存疑分歧] 双方意见不一致的点（附各自理由）\n"
            "## [最终决策参考] 基于辩论给出的修改优先级建议\n"
            "## 评分（1-10）+ 一句话总结"
        ),
    },
    "tech-decision": {
        "name": "技术方案决策",
        "icon_a": "\U0001f680", "icon_b": "\U0001f6e1\ufe0f",
        "name_a": "激进派", "name_b": "保守派",
        "system_a": (
            "你是 **激进派架构师**，代号 \U0001f680A。\n\n"
            "你的立场：\n"
            "- 倾向于采用新技术、新架构，追求长期收益\n"
            "- 关注：可扩展性、技术先进性、开发效率提升、未来维护成本\n"
            "- 愿意承担短期的迁移成本和学习曲线\n"
            "- 必须用量化数据支撑：预估节省的人天、性能提升百分比、未来N年的维护成本对比\n"
            "- 当对方质疑风险时，给出具体的回滚方案和灰度策略\n"
        ),
        "system_b": (
            "你是 **保守派架构师**，代号 \U0001f6e1\ufe0fB。\n\n"
            "你的立场：\n"
            "- 倾向于稳定可靠的方案，规避风险\n"
            "- 关注：稳定性、团队熟悉度、迁移成本、时间压力、回滚方案\n"
            "- 质疑新方案的必要性，要求证明现有方案不够用\n"
            "- 必须用量化数据反驳：迁移所需人天、学习曲线周期、历史故障案例\n"
            "- 当对方给出收益数据时，质疑其假设是否成立\n"
        ),
        # 收敛策略：僵持时输出对比清单，不强求统一意见
        "convergence": "tradeoff",
        "min_rounds": 3,
        "summary_prompt": (
            "请根据辩论记录生成技术决策汇总：\n\n"
            "## [达成共识] 双方无异议的结论\n"
            "## [存疑分歧] 核心冲突点（附各自量化论据）\n"
            "## [对比清单] Cost vs Benefit 表格（维度：开发成本/维护成本/性能/风险/时间线）\n"
            "## [最终决策参考] 基于当前信息的最优建议"
        ),
    },
    "requirement": {
        "name": "需求评审",
        "icon_a": "\U0001f4cb", "icon_b": "\U0001f4bb",
        "name_a": "产品经理", "name_b": "技术负责人",
        "system_a": (
            "你是 **产品经理**，代号 \U0001f4cbA。\n\n"
            "你的立场：\n"
            "- 从用户价值和业务目标出发论证需求合理性\n"
            "- 关注：用户体验、业务指标、竞品对比、优先级\n"
            "- 对技术方提出的「做不了」会追问是真的做不了还是不想做\n"
            "- 愿意在范围上妥协，但坚持核心价值\n"
            "- 必须明确资源约束：可接受的人天上限、截止日期、可砍的非核心功能\n"
        ),
        "system_b": (
            "你是 **技术负责人**，代号 \U0001f4bbB。\n\n"
            "你的立场：\n"
            "- 从技术可行性和工程成本出发评估需求\n"
            "- 关注：实现复杂度、系统影响面、工期、技术债务、边界case\n"
            "- 对需求中模糊的部分会追问细节和边界条件\n"
            "- 提出分期实现或简化方案作为替代\n"
            "- 必须给出具体工期估算（人天），而非模糊的「很复杂」\n"
        ),
        # 收敛策略：3轮僵持强制输出矛盾点
        "convergence": "deadline",
        "min_rounds": 2,
        "max_stalemate": 3,
        "summary_prompt": (
            "请根据辩论记录生成需求评审汇总：\n\n"
            "## [达成共识] 可直接实施的部分\n"
            "## [存疑分歧] 产品和技术的核心矛盾点\n"
            "## [最终决策参考] 建议的分期方案和优先级排序\n"
            "## 预估总工期"
        ),
    },
    "security": {
        "name": "安全攻防",
        "icon_a": "\U0001f5e1\ufe0f", "icon_b": "\U0001f6e1\ufe0f",
        "name_a": "攻击者", "name_b": "防御者",
        "system_a": (
            "你是 **安全攻击者（Red Team）**，代号 \U0001f5e1\ufe0fA。\n\n"
            "你的任务：\n"
            "- 从攻击者视角寻找系统漏洞和攻击面\n"
            "- 关注：注入攻击、认证绕过、权限提升、数据泄露、SSRF、XSS、CSRF\n"
            "- 构造具体的攻击场景和 PoC 思路\n"
            "- 评估漏洞的实际可利用性和影响范围\n"
            "- 永远不要说「没有更多威胁了」，除非你真的穷尽了所有攻击面\n"
            "- 当防御者说「已有防护」时，尝试绕过该防护\n"
        ),
        "system_b": (
            "你是 **安全防御者（Blue Team）**，代号 \U0001f6e1\ufe0fB。\n\n"
            "你的任务：\n"
            "- 评估攻击者提出的威胁是否在当前架构下可行\n"
            "- 指出已有的防护措施（WAF、参数校验、权限控制等）\n"
            "- 对确实存在的风险提出具体修复方案（代码级别）\n"
            "- 区分理论攻击和实际可利用的漏洞\n"
            "- 对每个威胁给出风险等级：Critical/High/Medium/Low\n"
        ),
        # 收敛策略：取消共识收敛，只有攻击者无法提出新威胁时才结束
        "convergence": "exhaustive",
        "min_rounds": 5,
        "summary_prompt": (
            "请根据辩论记录生成安全评估报告：\n\n"
            "## [确认威胁] 攻击者成功证明可利用的漏洞（按风险等级排序）\n"
            "## [已防护] 攻击者尝试但被现有防护阻止的路径\n"
            "## [存疑分歧] 双方对风险等级判断不一致的点\n"
            "## [最终决策参考] 修复优先级和具体方案建议"
        ),
    },
    "free": {
        "name": "自由辩论",
        "icon_a": "\U0001f7e0", "icon_b": "\U0001f7e3",
        "name_a": "正方", "name_b": "反方",
        "system_a": (
            "你是 **正方辩手**，代号 \U0001f7e0A。\n\n"
            "规则：\n"
            "- 支持论题中的正面立场\n"
            "- 用逻辑、数据、案例支撑你的观点\n"
            "- 鼓励从哲学、社会学、经济学、心理学等跨学科维度切入\n"
            "- 对反方的论点进行有理有据的反驳\n"
            "- 保持理性和尊重，但立场坚定\n"
            "- **前10轮禁止说「我同意对方部分观点」「对方说得有道理」等认同表述**\n"
            "- 你的目标是尽可能展开论证的广度和深度，而非快速达成共识\n"
        ),
        "system_b": (
            "你是 **反方辩手**，代号 \U0001f7e3B。\n\n"
            "规则：\n"
            "- 反对论题中的正面立场，提出质疑和替代观点\n"
            "- 用逻辑、数据、案例支撑你的观点\n"
            "- 鼓励从哲学、社会学、经济学、心理学等跨学科维度切入\n"
            "- 指出正方论证中的漏洞和隐含假设\n"
            "- 保持理性和尊重，但立场坚定\n"
            "- **前10轮禁止说「我同意对方部分观点」「对方说得有道理」等认同表述**\n"
            "- 你的目标是尽可能展开论证的广度和深度，而非快速达成共识\n"
        ),
        # 收敛策略：杠精模式，前10轮禁止收敛，之后检测语义熵
        "convergence": "entropy",
        "min_rounds": 10,
        "summary_prompt": (
            "请根据辩论记录生成总结：\n\n"
            "## [达成共识] 双方最终无异议的结论（如有）\n"
            "## [存疑分歧] 双方至死不让步的核心冲突点\n"
            "## [最终决策参考] 基于论据质量给出的倾向性判断和建议\n"
            "## 辩论质量评价（论证深度、跨学科广度、逻辑严密性）"
        ),
    },
}

# 通用回复格式指引
REPLY_RULES = """
回复规则：
1. 每次发言不超过 800 字
2. 先回应对方最新观点（同意标记 [共识]，不同意给出理由）
3. 再补充自己的新观点或新发现
4. 如果用户插话了，优先回应用户的问题
"""

# 杠精模式附加规则（自由辩论前10轮）
CONTRARIAN_RULES = """
**杠精模式生效中（前10轮）：**
- 禁止认同对方任何观点
- 禁止说「有道理」「我同意」「对方说得对」
- 必须从新的角度反驳或质疑
- 鼓励引入新的学科视角（哲学/心理学/经济学/社会学/生物学）
"""


class DebateLog:
    """辩论日志，JSONL 存储"""

    def __init__(self, session_dir: str):
        self._path = os.path.join(session_dir, "debate_log.jsonl")
        os.makedirs(session_dir, exist_ok=True)

    def append(self, speaker: str, content: str):
        entry = {
            "speaker": speaker,
            "content": content,
            "ts": int(time.time()),
        }
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def read_all(self) -> list[dict]:
        if not os.path.isfile(self._path):
            return []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]
        except Exception:
            return []

    def format_for_prompt(self, last_n: int = 20) -> str:
        """只取最近 N 条避免 context 爆炸"""
        entries = self.read_all()
        if len(entries) > last_n:
            entries = entries[-last_n:]
        lines = []
        for e in entries:
            lines.append(f"[{e['speaker']}]: {e['content']}")
        return "\n\n---\n\n".join(lines)

    def format_full(self) -> str:
        entries = self.read_all()
        lines = []
        for e in entries:
            lines.append(f"[{e['speaker']}]: {e['content']}")
        return "\n\n---\n\n".join(lines)

    def turn_count(self) -> int:
        return len(self.read_all())

    def agent_turns(self) -> int:
        """只计算 agent 发言轮次（不含用户插话）"""
        return sum(1 for e in self.read_all() if not e["speaker"].startswith("\U0001f464"))

    def last_n_unique_topics(self, n: int = 4) -> float:
        """简易语义熵估算：最近N条发言中新观点的比例"""
        entries = self.read_all()
        if len(entries) < n:
            return 1.0
        recent = [e["content"] for e in entries[-n:]]
        # 简单启发式：如果最近几条回复中出现大量重复短语，说明信息量低
        all_text = " ".join(recent)
        words = set(all_text)
        # 用字符集多样性作为粗略的熵指标
        if len(all_text) == 0:
            return 0.0
        return min(len(words) / len(all_text) * 10, 1.0)

    def clear(self):
        if os.path.isfile(self._path):
            os.remove(self._path)


class DebateSession:
    """通用辩论会话（v2: 场景差异化策略）"""

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
        self._stalemate_count = 0  # 僵持计数（需求评审用）

    @property
    def running(self) -> bool:
        return self._running

    async def start_debate(self, topic: str, preset: str = "free",
                           context: str = "", custom_roles: dict | None = None):
        """启动辩论"""
        if self._running:
            return

        self._running = True
        self._stop_event.clear()
        self._debate_log.clear()
        self._stalemate_count = 0

        if custom_roles:
            self._preset = {
                "name": "自定义辩论",
                "icon_a": custom_roles.get("icon_a", "\U0001f7e0"),
                "icon_b": custom_roles.get("icon_b", "\U0001f7e3"),
                "name_a": custom_roles.get("name_a", "角色A"),
                "name_b": custom_roles.get("name_b", "角色B"),
                "system_a": custom_roles.get("system_a", ""),
                "system_b": custom_roles.get("system_b", ""),
                "convergence": custom_roles.get("convergence", "normal"),
                "min_rounds": custom_roles.get("min_rounds", 3),
                "summary_prompt": custom_roles.get("summary_prompt",
                    PRESETS["free"]["summary_prompt"]),
            }
        else:
            self._preset = PRESETS.get(preset, PRESETS["free"])

        await self._start_agents()
        self._debate_task = asyncio.create_task(self._debate_loop(topic, context))

    async def stop_debate(self) -> str:
        """用户喊停，生成汇总"""
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
        """用户插话"""
        self._debate_log.append("\U0001f464用户", text)
        await self._push_msg(f"\U0001f464 **用户插话**: {text}")

    # ---- 内部方法 ----

    async def _start_agents(self):
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

        log.info("Debate agents started chatid=%s preset=%s", self._chatid, self._preset["name"])

    async def _debate_loop(self, topic: str, context: str):
        """辩论主循环"""
        p = self._preset
        try:
            convergence = p.get("convergence", "normal")
            min_rounds = p.get("min_rounds", 3)

            await self._push_msg(
                f"\U0001f3af **{p['name']}开始**\n"
                f"- {p['icon_a']}A: {p['name_a']}\n"
                f"- {p['icon_b']}B: {p['name_b']}\n"
                f"- 策略: {convergence} | 最少 {min_rounds} 轮\n"
                f"- 随时发消息插话，发「停止辩论」结束\n\n---")

            # === 第一轮 ===
            round_num = 1

            a_prompt = self._build_first_prompt("A", topic, context)
            a_reply = await self._agent_a.send(a_prompt, timeout=180)
            if self._stop_event.is_set():
                return
            self._debate_log.append(f"{p['icon_a']}A", a_reply)
            await self._push_msg(f"{p['icon_a']} **{p['name_a']} (R{round_num})**:\n\n{a_reply}")

            b_prompt = self._build_first_prompt("B", topic, context, prev_opinion=a_reply)
            b_reply = await self._agent_b.send(b_prompt, timeout=180)
            if self._stop_event.is_set():
                return
            self._debate_log.append(f"{p['icon_b']}B", b_reply)
            await self._push_msg(f"{p['icon_b']} **{p['name_b']} (R{round_num})**:\n\n{b_reply}")

            # === 后续轮次 ===
            for round_num in range(2, self._max_rounds + 1):
                if self._stop_event.is_set():
                    break

                await asyncio.sleep(2)

                # Agent-A
                a_prompt = self._build_reply_prompt("A", round_num)
                a_reply = await self._agent_a.send(a_prompt, timeout=120)
                if self._stop_event.is_set():
                    break
                self._debate_log.append(f"{p['icon_a']}A", a_reply)
                await self._push_msg(f"{p['icon_a']} **{p['name_a']} (R{round_num})**:\n\n{a_reply}")

                # 收敛检测
                if self._should_end(a_reply, round_num, "A"):
                    break

                await asyncio.sleep(2)

                # Agent-B
                b_prompt = self._build_reply_prompt("B", round_num)
                b_reply = await self._agent_b.send(b_prompt, timeout=120)
                if self._stop_event.is_set():
                    break
                self._debate_log.append(f"{p['icon_b']}B", b_reply)
                await self._push_msg(f"{p['icon_b']} **{p['name_b']} (R{round_num})**:\n\n{b_reply}")

                if self._should_end(b_reply, round_num, "B"):
                    break

            # 生成汇总
            summary = await self._generate_summary()
            await self._push_msg(f"\U0001f4cb **汇总报告**\n\n{summary}")

        except Exception as e:
            log.error("Debate loop error chatid=%s: %s", self._chatid, e)
            await self._push_msg(f"\u274c 辩论异常中断: {e}")
        finally:
            self._running = False
            await self._cleanup()

    def _should_end(self, reply: str, round_num: int, agent_id: str) -> bool:
        """根据场景策略判断是否应该结束"""
        p = self._preset
        convergence = p.get("convergence", "normal")
        min_rounds = p.get("min_rounds", 3)

        # 未达到最小轮次，不结束
        if round_num < min_rounds:
            return False

        if convergence == "strict":
            # Code Review: 存在未解决的 Critical 问题时禁止结束
            has_critical = any(kw in reply for kw in ["\U0001f6a8", "Critical", "严重", "安全漏洞"])
            has_consensus_end = reply.count("[共识]") >= 5 and not has_critical
            if has_consensus_end:
                asyncio.ensure_future(self._push_msg("\U0001f4a1 所有 Critical 问题已解决，辩论收敛。"))
                return True
            return False

        elif convergence == "tradeoff":
            # 技术决策: 僵持时输出对比清单即可结束
            if reply.count("[共识]") >= 4:
                asyncio.ensure_future(self._push_msg("\U0001f4a1 双方达成足够共识，进入汇总。"))
                return True
            # 检测僵持（连续重复论点）
            if round_num > 6 and self._debate_log.last_n_unique_topics(4) < 0.3:
                asyncio.ensure_future(self._push_msg("\U0001f4a1 论点趋于重复，进入汇总输出对比清单。"))
                return True
            return False

        elif convergence == "deadline":
            # 需求评审: 3轮僵持强制结束
            if reply.count("[共识]") >= 3:
                asyncio.ensure_future(self._push_msg("\U0001f4a1 达成共识，进入汇总。"))
                return True
            # 检测僵持
            max_stalemate = p.get("max_stalemate", 3)
            if "[共识]" not in reply and round_num > min_rounds:
                self._stalemate_count += 1
            else:
                self._stalemate_count = 0
            if self._stalemate_count >= max_stalemate:
                asyncio.ensure_future(self._push_msg(
                    f"\u26a0\ufe0f 连续 {max_stalemate} 轮未达成新共识，强制输出核心矛盾点。"))
                return True
            return False

        elif convergence == "exhaustive":
            # 安全攻防: 取消共识收敛，只有攻击者无法提出新威胁时结束
            if agent_id == "A":
                # 攻击者说没有更多威胁了
                exhausted_phrases = ["无法提出新", "攻击面已穷尽", "没有更多威胁", "暂时想不到"]
                if any(phrase in reply for phrase in exhausted_phrases):
                    asyncio.ensure_future(self._push_msg("\U0001f4a1 攻击者已穷尽攻击路径，进入汇总。"))
                    return True
            return False

        elif convergence == "entropy":
            # 自由辩论: 杠精模式，检测语义熵
            # 前10轮绝不结束
            if round_num <= 10:
                return False
            # 10轮后检测信息量
            entropy = self._debate_log.last_n_unique_topics(6)
            if entropy < 0.25:
                asyncio.ensure_future(self._push_msg(
                    f"\U0001f4a1 信息量下降（熵={entropy:.2f}），辩论自然收敛。"))
                return True
            # 即使有共识标记，信息量高就继续
            if reply.count("[共识]") >= 3 and entropy > 0.5:
                return False  # 还有新东西可聊
            if reply.count("[共识]") >= 5:
                asyncio.ensure_future(self._push_msg("\U0001f4a1 大量共识达成，进入汇总。"))
                return True
            return False

        else:
            # normal: 传统收敛
            if reply.count("[共识]") >= 3:
                asyncio.ensure_future(self._push_msg("\U0001f4a1 达成共识，进入汇总。"))
                return True
            end_phrases = ["辩论结束", "以上为最终意见", "没有更多补充"]
            if any(phrase in reply for phrase in end_phrases):
                asyncio.ensure_future(self._push_msg("\U0001f4a1 辩论自然结束。"))
                return True
            return False

    def _build_first_prompt(self, agent_id: str, topic: str,
                            context: str, prev_opinion: str = "") -> str:
        p = self._preset
        system = p["system_a"] if agent_id == "A" else p["system_b"]
        other_name = p["name_b"] if agent_id == "A" else p["name_a"]
        other_icon = p["icon_b"] if agent_id == "A" else p["icon_a"]

        # 自由辩论前10轮附加杠精规则
        extra_rules = ""
        convergence = p.get("convergence", "normal")
        if convergence == "entropy":
            extra_rules = CONTRARIAN_RULES

        parts = [system + REPLY_RULES + extra_rules, "\n\n---\n\n"]

        if context:
            parts.append(f"## 背景信息\n{context}\n\n")

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
        p = self._preset
        system = p["system_a"] if agent_id == "A" else p["system_b"]
        other_name = p["name_b"] if agent_id == "A" else p["name_a"]
        other_icon = p["icon_b"] if agent_id == "A" else p["icon_a"]
        convergence = p.get("convergence", "normal")

        # 自由辩论前10轮附加杠精规则
        extra_rules = ""
        if convergence == "entropy" and round_num <= 10:
            extra_rules = CONTRARIAN_RULES

        debate_history = self._debate_log.format_for_prompt(last_n=10)

        return (
            f"{system}{REPLY_RULES}{extra_rules}\n\n"
            f"---\n\n"
            f"## 辩论记录（第 {round_num} 轮，最近发言）\n\n"
            f"{debate_history}\n\n"
            f"---\n\n"
            f"这是第 {round_num} 轮。请：\n"
            f"1. 回应 {other_icon}{other_name} 最新的观点\n"
            f"2. 如果用户有插话，优先回应用户\n"
            f"3. 补充新的论点或证据\n"
            f"4. 达成共识的点标记 [共识]"
        )

    async def _generate_summary(self) -> str:
        debate_log = self._debate_log.format_full()
        if not debate_log:
            return "无辩论记录"

        if len(debate_log) > 20000:
            debate_log = debate_log[:20000] + "\n\n...(记录过长已截断)"

        summary_template = self._preset.get("summary_prompt", PRESETS["free"]["summary_prompt"])
        prompt = (
            f"请根据以下辩论记录生成汇总报告。严格按照以下格式输出：\n\n"
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
        log.info("Debate session cleanup chatid=%s", self._chatid)
