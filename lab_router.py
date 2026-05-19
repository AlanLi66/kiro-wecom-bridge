"""Requirement Lab 内部 API — 供 BFF (8910) 调用

提供代码可行性分析、辩论、模拟器、创意生成等能力。
通过 agent 进程执行实际分析工作。
"""

import asyncio
import json
import logging
import os
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from channel import ChannelManager

log = logging.getLogger(__name__)

router = APIRouter(prefix="/lab", tags=["requirement-lab"])

# 分析用的 chatid（使用独立会话，不干扰正常对话）
LAB_CHATID = "_requirement_lab"
LAB_CWD = os.getenv("KIRO_WORK_DIR", "/mnt/i/workspace/alan_bot")


class AnalyzeCodeRequest(BaseModel):
    """代码可行性分析请求"""
    description: str
    target_pages: List[str] = []
    goal: Optional[str] = None


class AnalyzeCodeResponse(BaseModel):
    """代码可行性分析响应"""
    feasibility_score: float = 0.0
    effort_estimate: str = ""
    services_involved: List[str] = []
    existing_implementation: str = ""
    implementation_diff: str = ""
    risks: List[str] = []
    raw_analysis: str = ""


class GenerateIdeasRequest(BaseModel):
    """AI 创意生成请求"""
    goal: str
    target_pages: List[str] = []
    context: str = ""


class RunDebateRequest(BaseModel):
    """辩论请求"""
    idea_id: int
    topic: str
    context: str = ""
    preset: str = "requirement"


class SimulateRequest(BaseModel):
    """模拟器请求"""
    idea_id: int
    description: str
    goal: str = ""
    target_pages: List[str] = []


# 全局引用，在 main.py 中设置
_cm: Optional[ChannelManager] = None
# 模拟器并发锁 — 同一时间只允许一个模拟任务
_simulate_lock = False


def set_channel_manager(cm: ChannelManager):
    """由 main.py 调用，注入 ChannelManager 引用"""
    global _cm
    _cm = cm


def _extract_json(text: str) -> dict:
    """从 AI 回复中提取 JSON 对象（多种策略）"""
    clean = text.strip()

    # 策略 1：直接解析
    try:
        return json.loads(clean)
    except (json.JSONDecodeError, ValueError):
        pass

    # 策略 2：去掉 markdown 代码块
    if "```" in clean:
        # 找到第一个 ``` 和最后一个 ```
        start = clean.find("```")
        end = clean.rfind("```")
        if start != end:
            inner = clean[start + 3:end].strip()
            if inner.startswith("json"):
                inner = inner[4:].strip()
            try:
                return json.loads(inner)
            except (json.JSONDecodeError, ValueError):
                pass

    # 策略 3：找到第一个 { 和最后一个 }
    first_brace = clean.find("{")
    last_brace = clean.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        candidate = clean[first_brace:last_brace + 1]
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            pass

    # 所有策略失败
    raise ValueError(f"无法从回复中提取 JSON: {clean[:200]}")


@router.post("/analyze_code")
async def analyze_code(req: AnalyzeCodeRequest):
    """代码可行性分析 — 让 agent 搜索本地代码，评估需求可行性"""
    if not _cm or not _cm.channels:
        return {"error": "Bridge 未就绪"}

    ch = _cm.channels[0]
    pages_str = "、".join(req.target_pages) if req.target_pages else "未指定"
    goal_str = f"\n目标：{req.goal}" if req.goal else ""

    prompt = f"""[lab-analyze]: 请分析以下需求的代码可行性，搜索本地代码判断是否已有实现。

需求描述：{req.description}{goal_str}
涉及页面：{pages_str}

搜索范围：
- 前端：/mnt/c/Alan/workspace/ec-website-next/src/、/mnt/c/Alan/workspace/ec-mobilesite-ssr/src/
- 后端：/mnt/i/workspace/ 下的 Java 服务

请输出严格 JSON 格式（不要包含 markdown 代码块标记）：
{{
  "feasibility_score": 0-10 的可行性评分,
  "effort_estimate": "预估工期（如 3-5 人天）",
  "services_involved": ["涉及的服务列表"],
  "existing_implementation": "现有实现描述（如果有）",
  "implementation_diff": "与需求的差异（如果已有部分实现）",
  "risks": ["风险点列表"]
}}"""

    try:
        proc = await ch.pool.get_or_create(LAB_CHATID, cwd=LAB_CWD, mode="safe")
        reply = await proc.send(prompt, timeout=120)

        # 尝试解析 JSON
        try:
            result = _extract_json(reply)
            result["raw_analysis"] = reply
            return result
        except (json.JSONDecodeError, ValueError):
            # 解析失败，返回原始文本
            return {
                "feasibility_score": 0,
                "effort_estimate": "无法解析",
                "services_involved": [],
                "existing_implementation": "",
                "implementation_diff": "",
                "risks": ["AI 返回格式异常，需人工确认"],
                "raw_analysis": reply,
            }
    except Exception as e:
        log.error("analyze_code 异常: %s", e)
        return {"error": str(e)}


@router.post("/generate_ideas")
async def generate_ideas(req: GenerateIdeasRequest):
    """AI 创意生成 — 基于目标主动发散需求创意"""
    global _simulate_lock

    if not _cm or not _cm.channels:
        return {"error": "Bridge 未就绪"}

    if _simulate_lock:
        return {"error": "Agent 正在执行其他任务，请稍后再试"}

    _simulate_lock = True
    ch = _cm.channels[0]
    pages_str = "、".join(req.target_pages) if req.target_pages else "不限"
    context_str = f"\n\n## 已有功能参考（基于代码扫描）\n{req.context}" if req.context else ""

    prompt = f"""[lab-generate]: 你是电商产品专家，专注于 GMV 和用户体验提升。

## 背景
yamibuy 是北美最大的亚洲食品电商平台，面向华人用户。
主要页面：首页、搜索、商品详情页(PDP)、购物车、结算、个人中心。
技术栈：前端 Next.js + Vue，后端 Java 微服务。

## 用户目标
{req.goal}

## 涉及页面范围
{pages_str}
{context_str}

## 重要约束
- 不要生成已有功能的重复需求（参考上方已有功能清单）
- 只生成真正的新功能或对已有功能的显著增强
- 如果不确定是否已有，标注 existing_status 为"需确认"

## 任务
请生成 5-8 个具体的需求创意。每个创意要：
- 具体可执行（不是泛泛的方向）
- 有明确的用户价值
- 考虑电商场景的实际约束
- 参考已有功能清单判断 existing_status

## 输出要求
只输出 JSON，不要任何其他文字。直接以 {{ 开头。

{{
  "ideas": [
    {{
      "title": "一句话标题",
      "description": "用户故事格式的详细描述（作为XX用户，我希望XX，以便XX）",
      "goal": "预期效果（量化指标）",
      "priority": "high/medium/low",
      "effort_estimate": "预估工期（如 3-5 人天）",
      "target_pages": ["涉及页面"],
      "existing_status": "已有/部分已有/全新",
      "reference": "参考案例或竞品（如有）",
      "reasoning": "生成此创意的依据（如：竞品X有此功能但我们没有 / 数据显示Y指标偏低 / 基于目标Z的逻辑推导）"
    }}
  ],
  "analysis_summary": "整体分析总结，说明为什么推荐这些方向"
}}

只输出 JSON。"""

    try:
        proc = await ch.pool.get_or_create(LAB_CHATID, cwd=LAB_CWD, mode="safe")
        reply = await proc.send(prompt, timeout=180)

        if not reply or not reply.strip():
            return {"error": "Agent 无响应，请稍后重试"}

        try:
            result = _extract_json(reply)
            result["raw_output"] = reply
            return result
        except (json.JSONDecodeError, ValueError):
            return {"error": "AI 返回格式异常", "raw_output": reply}
    except Exception as e:
        log.error("generate_ideas 异常: %s", e)
        return {"error": str(e)}
    finally:
        _simulate_lock = False


@router.post("/run_debate")
async def run_debate(req: RunDebateRequest):
    """辩论评审 — 让 agent 模拟产品经理 vs 技术负责人的辩论，输出结论"""
    global _simulate_lock

    if not _cm or not _cm.channels:
        return {"error": "Bridge 未就绪"}

    if _simulate_lock:
        return {"error": "Agent 正在执行其他任务，请稍后再试"}

    _simulate_lock = True
    ch = _cm.channels[0]

    prompt = f"""[lab-debate]: 请模拟一场需求评审辩论。

## 辩论主题
{req.topic}

## 补充上下文
{req.context if req.context else "无"}

## 辩论规则
模拟两个角色进行 3 轮辩论：
- 🔴 产品经理：关注用户价值、业务收益、市场竞争
- 🔵 技术负责人：关注技术可行性、工期、风险、维护成本

每轮每人发言 100-200 字，观点要有对抗性。

## 输出要求
只输出 JSON，不要其他文字。直接以 {{ 开头。

{{
  "rounds": [
    {{
      "round": 1,
      "pm": "产品经理的发言",
      "tech": "技术负责人的发言"
    }}
  ],
  "consensus": ["双方达成共识的点"],
  "disagreements": ["仍有分歧的点"],
  "final_recommendation": "最终建议（做/不做/有条件做）",
  "conditions": ["如果做，需要满足的条件"],
  "estimated_effort": "综合工期评估"
}}

只输出 JSON。"""

    try:
        proc = await ch.pool.get_or_create(LAB_CHATID, cwd=LAB_CWD, mode="safe")
        reply = await proc.send(prompt, timeout=120)

        if not reply or not reply.strip():
            return {"error": "Agent 无响应"}

        try:
            result = _extract_json(reply)
            # 生成可读的 summary
            summary_parts = []
            if result.get("rounds"):
                for r in result["rounds"]:
                    summary_parts.append(f"**第{r['round']}轮**")
                    summary_parts.append(f"🔴 产品: {r['pm']}")
                    summary_parts.append(f"🔵 技术: {r['tech']}\n")
            if result.get("consensus"):
                summary_parts.append("**✅ 共识：**" + "；".join(result["consensus"]))
            if result.get("disagreements"):
                summary_parts.append("**⚠️ 分歧：**" + "；".join(result["disagreements"]))
            if result.get("final_recommendation"):
                summary_parts.append(f"**📋 建议：**{result['final_recommendation']}")

            result["summary"] = "\n".join(summary_parts)
            result["idea_id"] = req.idea_id
            return result
        except (json.JSONDecodeError, ValueError):
            return {"error": "AI 返回格式异常", "raw_output": reply}
    except Exception as e:
        log.error("run_debate 异常: %s", e)
        return {"error": str(e)}
    finally:
        _simulate_lock = False


@router.post("/simulate")
async def simulate(req: SimulateRequest):
    """运行需求模拟器 — 生成用户场景 + 代码验证 + 风险评估"""
    global _simulate_lock

    if not _cm or not _cm.channels:
        return {"error": "Bridge 未就绪"}

    if _simulate_lock:
        return {"error": "模拟器正在运行中，请等待当前任务完成后再试"}

    _simulate_lock = True

    ch = _cm.channels[0]
    pages_str = "、".join(req.target_pages) if req.target_pages else "未指定"
    goal_str = f"\n目标：{req.goal}" if req.goal else ""

    prompt = f"""[lab-simulate]: 请对以下需求进行完整的场景模拟分析。

需求描述：{req.description}{goal_str}
涉及页面：{pages_str}

## 任务

### Step 1: 场景生成
生成 10 个用户场景，覆盖：正常4个、边界3个、异常2个、并发1个。

### Step 2: 代码验证
搜索本地代码，判断每个场景的覆盖情况：
- 前端：/mnt/c/Alan/workspace/ec-website-next/src/
- 后端：/mnt/i/workspace/ 下的 Java 服务
用 grep 搜索关键词判断是否有相关实现。

### Step 3: 风险评估
基于场景和代码覆盖情况，输出风险和待确认问题。

## 输出要求
你必须且只能输出一个 JSON 对象。不要输出任何解释文字。不要用 markdown 代码块。直接以 {{ 开头。

{{
  "scenarios": [
    {{
      "id": "S01",
      "category": "normal",
      "title": "场景标题",
      "user_role": "用户角色",
      "preconditions": ["前置条件"],
      "steps": ["步骤1", "步骤2"],
      "expected_result": "预期结果",
      "code_coverage": "uncovered",
      "coverage_evidence": "搜索到的代码文件路径或关键信息"
    }}
  ],
  "coverage_summary": {{
    "total": 10,
    "covered": 3,
    "partial": 2,
    "uncovered": 5,
    "coverage_rate": 30.0
  }},
  "risks": [
    {{
      "level": "high",
      "description": "风险描述",
      "related_scenarios": ["S01"],
      "mitigation": "缓解措施"
    }}
  ],
  "questions_for_pm": ["问题1"],
  "overall_assessment": "总结"
}}

category 只能是: normal, edge, error, concurrent
code_coverage 只能是: covered, partial, uncovered
level 只能是: high, medium, low
只输出 JSON。"""

    try:
        proc = await ch.pool.get_or_create(LAB_CHATID, cwd=LAB_CWD, mode="safe")
        reply = await proc.send(prompt, timeout=300)

        if not reply or not reply.strip():
            return {
                "scenarios": [],
                "coverage_summary": {"total": 0, "covered": 0, "partial": 0, "uncovered": 0, "coverage_rate": 0},
                "risks": [{"level": "medium", "description": "Agent 返回空内容，可能正在处理其他任务", "related_scenarios": [], "mitigation": "稍后重试"}],
                "questions_for_pm": [],
                "overall_assessment": "模拟未完成 — Agent 无响应",
                "raw_output": reply or "",
            }

        # 尝试解析 JSON — 多种策略
        try:
            result = _extract_json(reply)
            result["raw_output"] = reply
            return result
        except (json.JSONDecodeError, ValueError):
            return {
                "scenarios": [],
                "coverage_summary": {"total": 0, "covered": 0, "partial": 0, "uncovered": 0, "coverage_rate": 0},
                "risks": [{"level": "high", "description": "AI 返回格式异常，需人工确认", "related_scenarios": [], "mitigation": "重新运行模拟"}],
                "questions_for_pm": [],
                "overall_assessment": "模拟结果解析失败",
                "raw_output": reply,
            }
    except Exception as e:
        log.error("simulate 异常: %s", e)
        return {"error": str(e)}
    finally:
        _simulate_lock = False


@router.post("/scan_features")
async def scan_features():
    """扫描代码，提取各页面已有功能清单"""
    global _simulate_lock

    if not _cm or not _cm.channels:
        return {"error": "Bridge 未就绪"}

    if _simulate_lock:
        return {"error": "Agent 正在执行其他任务，请稍后再试"}

    _simulate_lock = True
    ch = _cm.channels[0]

    prompt = """[lab-scan]: 请扫描以下前端代码目录，列出每个页面已实现的用户可见功能。

用 find 和 grep 搜索代码，关注组件名、路由、UI 文案、API 调用等线索。

扫描目标：
1. 首页: /mnt/c/Alan/workspace/ec-website-next/src/app/(home)/ 和 src/components/home/
2. PDP: /mnt/c/Alan/workspace/ec-website-next/src/app/product/ 和 src/components/product/
3. 购物车: /mnt/c/Alan/workspace/ec-website-next/src/app/cart/ 和 src/components/cart/
4. 搜索: /mnt/c/Alan/workspace/ec-website-next/src/app/search/ 和 src/components/search/
5. 结算: /mnt/c/Alan/workspace/ec-website-next/src/app/checkout/
6. 个人中心: /mnt/c/Alan/workspace/ec-website-next/src/app/account/

## 输出要求
只输出 JSON，不要其他文字。直接以 { 开头。

每个功能必须包含 name（功能名）和 description（功能描述，说明具体做了什么、用户如何交互、有什么业务价值）。

{
  "pages": [
    {
      "page": "首页(Home)",
      "features": [
        {"name": "Banner轮播", "description": "首屏大图轮播，展示促销活动和新品推荐，支持自动播放和手动切换，点击跳转活动页"},
        {"name": "分类导航", "description": "顶部/侧边分类入口，按商品类目（零食、饮料、美妆等）快速跳转到对应列表页"}
      ],
      "key_files": ["src/app/(home)/page.tsx"]
    }
  ],
  "scan_summary": "共扫描6个页面，发现XX个功能点"
}

description 要求：
- 20-60字，说清楚功能的具体表现和用户价值
- 基于代码中看到的实际实现来描述，不要编造
- 如果从代码中能看出交互细节（如支持筛选、排序、分页等），要写出来

只输出 JSON。"""

    try:
        proc = await ch.pool.get_or_create(LAB_CHATID, cwd=LAB_CWD, mode="full")
        reply = await proc.send(prompt, timeout=180)

        if not reply or not reply.strip():
            return {"error": "Agent 无响应"}

        try:
            result = _extract_json(reply)
            result["raw_output"] = reply
            return result
        except (json.JSONDecodeError, ValueError):
            return {"error": "解析失败", "raw_output": reply}
    except Exception as e:
        log.error("scan_features 异常: %s", e)
        return {"error": str(e)}
    finally:
        _simulate_lock = False


@router.post("/scan_competitors")
async def scan_competitors():
    """搜索竞品最新功能和动态"""
    global _simulate_lock

    if not _cm or not _cm.channels:
        return {"error": "Bridge 未就绪"}

    if _simulate_lock:
        return {"error": "Agent 正在执行其他任务，请稍后再试"}

    _simulate_lock = True
    ch = _cm.channels[0]

    prompt = """[lab-competitor]: 请搜索以下竞品电商平台的功能特点和最新动态。

竞品列表：
1. Temu - 拼多多海外版，主打低价
2. Sayweee - 亚洲生鲜电商
3. Amazon - 综合电商巨头
4. Weee! - 亚洲食品杂货配送
5. H Mart - 韩国超市线上化

对每个竞品，搜索其：
- 核心功能特点（首页/搜索/PDP/购物车/结算/会员/促销）
- 最近的产品更新或新功能
- 与 yamibuy 的差异化优势

## 输出要求
只输出 JSON，不要其他文字。直接以 { 开头。

每个功能必须包含 name（功能名）和 description（详细描述该功能的具体实现方式、用户体验、业务策略）。

{
  "competitors": [
    {
      "name": "temu",
      "features": [
        {"name": "限时闪购倒计时", "description": "商品页和列表页展示实时倒计时，营造紧迫感驱动用户快速下单，结合库存紧张提示增强转化"},
        {"name": "社交裂变拼团", "description": "用户发起拼团邀请好友参与，达到人数后享受更低价格，通过社交分享获取新用户"}
      ],
      "recent_changes": [
        {"name": "本地卖家计划", "description": "2024年推出，允许本地商家入驻，缩短配送时间，从纯跨境模式向本地化转型"}
      ],
      "highlights": "核心差异化优势一句话"
    }
  ],
  "scan_summary": "扫描总结"
}

description 要求：
- 30-80字，说清楚功能的具体实现方式、用户体验和业务价值
- 基于搜索到的真实信息描述，不要编造
- recent_changes 也要有详细描述，说明变化的背景和影响

只输出 JSON。"""

    try:
        proc = await ch.pool.get_or_create(LAB_CHATID, cwd=LAB_CWD, mode="full")
        reply = await proc.send(prompt, timeout=180)

        if not reply or not reply.strip():
            return {"error": "Agent 无响应"}

        try:
            result = _extract_json(reply)
            result["raw_output"] = reply
            return result
        except (json.JSONDecodeError, ValueError):
            return {"error": "解析失败", "raw_output": reply}
    except Exception as e:
        log.error("scan_competitors 异常: %s", e)
        return {"error": str(e)}
    finally:
        _simulate_lock = False
