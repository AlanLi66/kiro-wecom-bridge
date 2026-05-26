"""Requirement Lab 内部 API — 供 BFF (8910) 调用

提供代码可行性分析、辩论、模拟器、创意生成等能力。
通过 agent 进程执行实际分析工作。
"""

import asyncio
import json
import logging
import os
from typing import List, Optional

import aiohttp
from fastapi import APIRouter
from pydantic import BaseModel

from channel import ChannelManager

log = logging.getLogger(__name__)

router = APIRouter(prefix="/lab", tags=["requirement-lab"])

# 分析用的 chatid（使用独立会话，不干扰正常对话）
LAB_CHATID = "_requirement_lab"
LAB_COMPETITOR_CHATID = "_requirement_lab_competitor"
LAB_CWD = os.getenv("KIRO_WORK_DIR", "/mnt/i/workspace/alan_bot")

# 企微通知配置
NOTIFY_CHATID = "dm_Alan.Li"
BRIDGE_SEND_URL = "http://localhost:8900/send"


async def _notify_wecom(content: str):
    """推送消息到企微"""
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                BRIDGE_SEND_URL,
                json={"chatid": NOTIFY_CHATID, "content": content, "chat_type": 1},
                timeout=aiohttp.ClientTimeout(total=10),
            )
    except Exception as e:
        log.warning("企微通知发送失败: %s", e)


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


@router.post("/reset_lock")
async def reset_lock():
    """手动释放并发锁（调试用）"""
    global _simulate_lock
    was_locked = _simulate_lock
    _simulate_lock = False
    return {"ok": True, "was_locked": was_locked}


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
        proc = await ch.pool.get_or_create(LAB_CHATID, cwd=LAB_CWD, mode="full")
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

## 去重规则（严格执行，最高优先级）
生成每个创意前，必须逐条检查已有功能清单中的 covers_scenarios：
1. 如果已有功能的 covers_scenarios 中包含你要生成的创意所解决的场景，则视为已有，禁止生成
2. 语义相近即视为重复：
   - "用户评价摘要/评价展示/评论统计/口碑" ≈ 已有的"评论区"功能
   - "最近购买记录/购买历史/浏览记录" ≈ 已有的"浏览足迹"功能
   - "社交证明/购买信心/从众心理" → 如果已有评论区+浏览足迹，则该场景已被覆盖
3. 对已有功能换个展示位置、换个文案、换个样式，不算新需求
4. 只有功能逻辑和用户价值完全不同于所有已有功能的，才算"全新"
5. 如果实在不确定，标注 existing_status 为"需确认"并说明与哪个已有功能可能重叠

## 任务
请生成 5-8 个具体的需求创意。每个创意要：
- 具体可执行（不是泛泛的方向）
- 有明确的用户价值
- 考虑电商场景的实际约束
- 与已有功能无语义重叠（严格遵守去重规则）

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
        proc = await ch.pool.get_or_create(LAB_CHATID, cwd=LAB_CWD, mode="full")
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
        proc = await ch.pool.get_or_create(LAB_CHATID, cwd=LAB_CWD, mode="full")
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
        proc = await ch.pool.get_or_create(LAB_CHATID, cwd=LAB_CWD, mode="full")
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



# ============================================================
# 功能扫描（两阶段：路由发现 → 逐页面深入）
# ============================================================

# 逐项目扫描配置
SCAN_PROJECTS = [
    {
        "project": "ec-website-next",
        "platform": "PC",
        "tech": "Next.js/React",
        "base_path": "/mnt/c/Alan/workspace/ec-website-next",
        "route_discovery": "find /mnt/c/Alan/workspace/ec-website-next/src/app -maxdepth 2 -name 'page.tsx' -o -name 'page.ts' | grep -v node_modules | sort",
        "file_types": "*.tsx,*.ts",
        "exclude_dirs": "node_modules,.next,.git,dist",
    },
    {
        "project": "ec-website-nb",
        "platform": "PC",
        "tech": "PHP/Laravel",
        "base_path": "/mnt/c/Alan/workspace/ec-website-nb",
        "route_discovery": "find /mnt/c/Alan/workspace/ec-website-nb/resources/views -maxdepth 1 -type d | sort",
        "file_types": "*.blade.php",
        "exclude_dirs": "vendor,node_modules,.git",
    },
    {
        "project": "ec-website-customer-next",
        "platform": "PC",
        "tech": "Next.js/React",
        "base_path": "/mnt/c/Alan/workspace/ec-website-customer-next",
        "route_discovery": "find /mnt/c/Alan/workspace/ec-website-customer-next/src/app -maxdepth 2 -name 'page.tsx' -o -name 'page.ts' | grep -v node_modules | sort",
        "file_types": "*.tsx,*.ts",
        "exclude_dirs": "node_modules,.next,.git,dist",
    },
    {
        "project": "ec-website-customer-nb",
        "platform": "PC",
        "tech": "PHP/Laravel",
        "base_path": "/mnt/c/Alan/workspace/ec-website-customer-nb",
        "route_discovery": "find /mnt/c/Alan/workspace/ec-website-customer-nb/resources/views -maxdepth 1 -type d | sort",
        "file_types": "*.blade.php",
        "exclude_dirs": "vendor,node_modules,.git",
    },
    {
        "project": "ec-website-trade-nb",
        "platform": "PC",
        "tech": "PHP/Laravel",
        "base_path": "/mnt/c/Alan/workspace/ec-website-trade-nb",
        "route_discovery": "find /mnt/c/Alan/workspace/ec-website-trade-nb/resources/views -maxdepth 1 -type d | sort",
        "file_types": "*.blade.php",
        "exclude_dirs": "vendor,node_modules,.git",
    },
    {
        "project": "ec-mobilesite-nb",
        "platform": "H5",
        "tech": "Nuxt.js/Vue",
        "base_path": "/mnt/c/Alan/workspace/ec-mobilesite-nb",
        "route_discovery": "find /mnt/c/Alan/workspace/ec-mobilesite-nb/src/pages -maxdepth 1 -type d | sort",
        "file_types": "*.vue,*.js",
        "exclude_dirs": "node_modules,.nuxt,.git,dist",
    },
    {
        "project": "ec-mobilesite-ssr",
        "platform": "H5",
        "tech": "Nuxt.js/Vue",
        "base_path": "/mnt/c/Alan/workspace/ec-mobilesite-ssr",
        "route_discovery": "find /mnt/c/Alan/workspace/ec-mobilesite-ssr/src/pages -maxdepth 1 -type d | sort",
        "file_types": "*.vue,*.js",
        "exclude_dirs": "node_modules,.nuxt,.git,dist,.output",
    },
    {
        "project": "ec-mobilesite-rma",
        "platform": "H5",
        "tech": "Vue",
        "base_path": "/mnt/c/Alan/workspace/ec-mobilesite-rma",
        "route_discovery": "find /mnt/c/Alan/workspace/ec-mobilesite-rma/src -maxdepth 2 -name '*.vue' -path '*/views/*' | sort",
        "file_types": "*.vue",
        "exclude_dirs": "node_modules,.git,dist",
    },
    {
        "project": "mobile_flutter",
        "platform": "APP",
        "tech": "Flutter/Dart",
        "base_path": "/mnt/c/Alan/workspace/mobile_flutter",
        "route_discovery": "grep -rn 'GoRoute\\|MaterialPageRoute\\|GetPage' /mnt/c/Alan/workspace/mobile_flutter/lib/ --include='*.dart' --exclude-dir=build --exclude-dir=.dart_tool --exclude-dir=.git -l | head -20",
        "file_types": "*.dart",
        "exclude_dirs": "build,.dart_tool,.git",
    },
]

# 竞品列表（逐竞品深入扫描）
COMPETITORS = [
    {
        "name": "Temu",
        "description": "拼多多海外版，主打极致低价和社交裂变",
        "focus": "低价策略、社交玩法、游戏化、新用户获取、物流体验",
    },
    {
        "name": "Sayweee/Weee!",
        "description": "北美亚洲生鲜食品电商，与 yamibuy 直接竞争",
        "focus": "生鲜配送、本地仓储、社区团购、会员体系、亚洲食品品类",
    },
    {
        "name": "Amazon",
        "description": "全球最大综合电商，行业标杆",
        "focus": "个性化推荐、Prime会员、一键下单、评论体系、物流速度、搜索体验",
    },
    {
        "name": "H Mart",
        "description": "韩国超市线上化，亚洲食品杂货",
        "focus": "线上线下融合、本地配送、韩国食品特色、促销活动",
    },
    {
        "name": "Instacart",
        "description": "北美即时配送平台，杂货电商标杆",
        "focus": "即时配送、替代品推荐、购物清单、个性化、广告变现",
    },
]


def _build_route_discovery_prompt(project_config: dict) -> str:
    """第一阶段：发现项目所有页面/路由"""
    return f"""[lab-scan-routes]: 请扫描 {project_config['project']}（{project_config['tech']}，{project_config['platform']}端）的路由结构，列出所有用户可访问的页面。

执行以下命令发现路由：
```
{project_config['route_discovery']}
```

然后根据结果，列出所有页面。对于每个页面，给出：
- 页面名称（中文+英文）
- 对应的代码目录路径

## 输出要求
只输出 JSON，不要其他文字。直接以 {{ 开头。

{{
  "project": "{project_config['project']}",
  "platform": "{project_config['platform']}",
  "pages": [
    {{
      "name": "首页(Home)",
      "path": "{project_config['base_path']}/src/app/(home)/"
    }}
  ]
}}

注意：
- 排除纯 API 路由（如 /api/）、layout 文件、error 页面、not-found 页面
- 只列出有实际 UI 的用户页面
- 如果是 Flutter 项目，从路由注册文件中提取页面列表，path 填对应的 lib/ 子目录
- 如果是 PHP/Laravel 项目，每个 views 子目录通常对应一个页面模块

只输出 JSON。"""


def _build_page_scan_prompt(project_config: dict, page_name: str, page_path: str) -> str:
    """第二阶段：深入扫描单个页面的功能"""
    return f"""[lab-scan-page]: 请深入扫描 **{page_name}** 页面的代码，列出所有用户可见功能。

项目：{project_config['project']}（{project_config['platform']}端，{project_config['tech']}）
代码路径：{page_path}

## 扫描方法
1. 先 find 列出该目录下所有代码文件（排除 {project_config['exclude_dirs']}）
2. 对关键文件 cat 查看内容，关注：组件引用、API 调用、UI 文案、条件渲染、用户交互
3. 不要遗漏任何用户可见的功能点（包括小功能如分享、收藏、标签、弹窗等）

## 输出要求
只输出 JSON，不要其他文字。直接以 {{ 开头。

{{
  "page": "{page_name}",
  "project": "{project_config['project']}",
  "platform": "{project_config['platform']}",
  "features": [
    {{
      "name": "功能名称",
      "description": "30-80字，说清楚功能的具体表现、用户交互方式和业务价值",
      "covers_scenarios": ["该功能覆盖的业务场景1", "场景2", "场景3"]
    }}
  ],
  "key_files": ["关键文件路径"]
}}

description 要求：
- 30-80字，基于代码实际实现描述，不要编造
- 用业务语言，说清楚用户能看到什么、能做什么

covers_scenarios 要求（重要，用于后续创意去重）：
- 列出 3-6 个该功能覆盖的业务场景
- 用产品语言：如"社交证明"、"购买信心"、"历史行为回溯"
- 包含近义词和相关概念，越全越好
- 例如评论区：["用户评价展示", "评分统计", "社交证明", "购买信心", "UGC内容", "口碑展示"]
- 例如浏览足迹：["历史浏览记录", "最近看过的商品", "行为回溯", "商品回访", "浏览历史"]

只输出 JSON。"""


def _build_competitor_scan_prompt(competitor: dict) -> str:
    """为单个竞品构建深入扫描 prompt"""
    return f"""[lab-competitor]: 请深入搜索 **{competitor['name']}** 的产品功能和最新动态。

## 竞品简介
{competitor['description']}

## 重点关注方向
{competitor['focus']}

## 搜索任务
请从多个角度搜索该竞品的信息：

1. **核心功能清单**：逐页面列出其主要功能（首页、搜索、商品详情、购物车、结算、会员中心、促销活动）
2. **差异化功能**：与 yamibuy 相比，它有哪些我们没有的功能？
3. **最近更新**：过去 6 个月的产品更新、新功能发布
4. **用户体验亮点**：交互设计、个性化、社交功能等方面的亮点

## 输出要求
只输出 JSON，不要其他文字。直接以 {{ 开头。

{{
  "name": "{competitor['name']}",
  "core_features": [
    {{
      "page": "页面名称",
      "features": [
        {{
          "name": "功能名称",
          "description": "50-100字详细描述：具体实现方式、用户体验、业务策略"
        }}
      ]
    }}
  ],
  "unique_features": [
    {{
      "name": "差异化功能名称",
      "description": "详细描述该功能的实现和价值",
      "yamibuy_gap": "yamibuy 缺少此功能的影响"
    }}
  ],
  "recent_changes": [
    {{
      "name": "更新名称",
      "description": "更新内容和影响",
      "date": "大致时间"
    }}
  ],
  "ux_highlights": ["体验亮点1", "体验亮点2"]
}}

description 要求：
- 50-100字，说清楚功能的具体实现方式、用户体验和业务策略
- 基于搜索到的真实信息，不要编造
- 如果搜索不到某方面信息，该字段留空数组，不要瞎编

只输出 JSON。"""


@router.post("/scan_features")
async def scan_features():
    """扫描代码，提取各页面已有功能清单（增量+并行：只扫有变更的项目，多进程并发）"""
    global _simulate_lock

    if not _cm or not _cm.channels:
        return {"error": "Bridge 未就绪"}

    if _simulate_lock:
        return {"error": "Agent 正在执行其他任务，请稍后再试"}

    _simulate_lock = True
    ch = _cm.channels[0]

    all_pages = []
    errors = []
    skipped = []

    try:
        # 第 0 步：检测哪些项目有代码变更（增量扫描）
        changed_projects = await _detect_changed_projects(ch)
        if not changed_projects:
            log.info("[scan_features] 所有项目无变更，跳过扫描")
            await _notify_wecom("📊 **功能扫描跳过**\n\n所有项目自上次扫描以来无代码变更。")
            return {"pages": [], "scan_summary": "所有项目无变更，跳过扫描", "skipped": [p["project"] for p in SCAN_PROJECTS]}

        skipped = [p["project"] for p in SCAN_PROJECTS if p not in changed_projects]
        log.info("[scan_features] 检测到 %d/%d 个项目有变更: %s",
                 len(changed_projects), len(SCAN_PROJECTS),
                 [p["project"] for p in changed_projects])

        # 第 1 步：并行扫描各项目（每个项目用独立 chatid 的 agent 进程）
        scan_tasks = []
        for i, project_config in enumerate(changed_projects):
            scan_tasks.append(_scan_single_project(ch, project_config, i))

        # 并发执行所有项目扫描
        results = await asyncio.gather(*scan_tasks, return_exceptions=True)

        for i, result in enumerate(results):
            project_name = changed_projects[i]["project"]
            if isinstance(result, Exception):
                errors.append(f"{project_name}: 异常 {result}")
                log.error("[scan_features] %s 扫描异常: %s", project_name, result)
            elif isinstance(result, dict):
                if result.get("pages"):
                    all_pages.extend(result["pages"])
                if result.get("errors"):
                    errors.extend(result["errors"])

        total_features = sum(len(p.get("features", [])) for p in all_pages)
        result = {
            "pages": all_pages,
            "scan_summary": f"增量扫描{len(changed_projects)}个项目（跳过{len(skipped)}个无变更）、{len(all_pages)}个页面，发现{total_features}个功能点",
        }
        if skipped:
            result["skipped"] = skipped
        if errors:
            result["errors"] = errors

        # 推送企微通知
        msg = f"📊 **功能扫描完成（增量+并行）**\n\n"
        msg += f"✅ 扫描项目: {len(changed_projects)} 个\n"
        msg += f"⏭️ 跳过(无变更): {len(skipped)} 个\n"
        msg += f"✅ 发现页面: {len(all_pages)} 个\n"
        msg += f"✅ 功能点: {total_features} 个\n"
        if errors:
            msg += f"⚠️ 失败: {len(errors)} 个\n"
            msg += "失败详情:\n" + "\n".join(f"  - {e}" for e in errors[:10])
        await _notify_wecom(msg)

        return result

    except Exception as e:
        log.error("scan_features 异常: %s", e)
        await _notify_wecom(f"❌ **功能扫描异常**\n\n{str(e)[:200]}")
        return {"error": str(e)}
    finally:
        _simulate_lock = False


async def _detect_changed_projects(ch) -> list:
    """检测哪些项目自上次扫描以来有代码变更（基于 git log）
    
    策略：检查项目 src/ 目录最近 24 小时是否有新 commit
    如果 git 命令失败（如目录不存在），视为有变更（保守策略）
    """
    changed = []
    # 用一个临时 agent 进程批量检测
    proc = await ch.pool.get_or_create(LAB_CHATID, cwd=LAB_CWD, mode="full")

    # 构建批量检测命令
    check_commands = []
    for p in SCAN_PROJECTS:
        base = p["base_path"]
        check_commands.append(f'echo "---{p["project"]}---"; cd {base} 2>/dev/null && git log --oneline --since="24 hours ago" --name-only -- . 2>/dev/null | head -5 || echo "NO_GIT"')

    batch_cmd = " ; ".join(check_commands)
    prompt = f"""[lab-check-changes]: 请执行以下命令，检查各项目最近 24 小时是否有代码变更：

```bash
{batch_cmd}
```

直接执行命令，把完整输出原样返回给我，不要做任何解析或总结。"""

    try:
        reply = await proc.send(prompt, timeout=60)
        if not reply:
            # 检测失败，保守策略：全部扫描
            log.warning("[detect_changes] agent 无响应，全部项目纳入扫描")
            return list(SCAN_PROJECTS)

        # 解析输出，判断每个项目是否有变更
        for p in SCAN_PROJECTS:
            marker = f"---{p['project']}---"
            idx = reply.find(marker)
            if idx == -1:
                # 找不到标记，保守策略：纳入扫描
                changed.append(p)
                continue

            # 取该项目到下一个项目之间的输出
            next_markers = [f"---{pp['project']}---" for pp in SCAN_PROJECTS if pp != p]
            end_idx = len(reply)
            for nm in next_markers:
                ni = reply.find(nm, idx + len(marker))
                if ni != -1 and ni < end_idx:
                    end_idx = ni

            section = reply[idx + len(marker):end_idx].strip()

            # 判断是否有变更
            if "NO_GIT" in section:
                # git 不可用，保守纳入
                changed.append(p)
            elif section and section not in ("", "\n"):
                # 有 git log 输出 = 有变更
                lines = [l for l in section.split("\n") if l.strip() and not l.startswith("$")]
                if lines:
                    changed.append(p)
                    log.info("[detect_changes] %s 有变更: %s", p["project"], lines[0][:60])
            # else: 无输出 = 无变更，跳过

    except Exception as e:
        log.warning("[detect_changes] 检测异常: %s，全部项目纳入扫描", e)
        return list(SCAN_PROJECTS)

    return changed


async def _scan_single_project(ch, project_config: dict, index: int) -> dict:
    """扫描单个项目（使用独立 chatid 的 agent 进程实现并行）
    
    每个项目分配独立的 chatid: _lab_scan_{index}
    这样多个项目可以同时用不同的 agent 进程并行扫描
    """
    project_name = project_config["project"]
    chatid = f"_lab_scan_{index}"
    pages = []
    errors = []

    try:
        proc = await ch.pool.get_or_create(chatid, cwd=LAB_CWD, mode="full")

        # 第一阶段：发现路由
        log.info("[scan_features] 并行扫描 %s (chatid=%s)", project_name, chatid)
        route_prompt = _build_route_discovery_prompt(project_config)
        route_reply = await proc.send(route_prompt, timeout=60)

        if not route_reply or not route_reply.strip():
            return {"pages": [], "errors": [f"{project_name}: 路由发现无响应"]}

        try:
            route_result = _extract_json(route_reply)
            discovered_pages = route_result.get("pages", [])
        except (json.JSONDecodeError, ValueError):
            return {"pages": [], "errors": [f"{project_name}: 路由发现 JSON 解析失败"]}

        if not discovered_pages:
            return {"pages": [], "errors": [f"{project_name}: 未发现任何页面"]}

        log.info("[scan_features] %s 发现 %d 个页面", project_name, len(discovered_pages))

        # 第二阶段：逐页面深入扫描
        for page_info in discovered_pages:
            page_name = page_info.get("name", "未知页面")
            page_path = page_info.get("path", "")

            if not page_path:
                continue

            page_prompt = _build_page_scan_prompt(project_config, page_name, page_path)
            page_reply = await proc.send(page_prompt, timeout=90)

            if not page_reply or not page_reply.strip():
                errors.append(f"{project_name}/{page_name}: 无响应")
                continue

            try:
                page_result = _extract_json(page_reply)
                page_result["page"] = page_name
                page_result["project"] = project_name
                page_result["platform"] = project_config["platform"]
                pages.append(page_result)
            except (json.JSONDecodeError, ValueError):
                errors.append(f"{project_name}/{page_name}: JSON 解析失败")

            await asyncio.sleep(0.3)

        return {"pages": pages, "errors": errors if errors else None}

    except Exception as e:
        log.error("[scan_features] %s 扫描异常: %s", project_name, e)
        return {"pages": [], "errors": [f"{project_name}: {str(e)}"]}


@router.post("/scan_competitors")
async def scan_competitors():
    """搜索竞品最新功能和动态（逐竞品并行扫描，使用独立 chatid）"""
    global _simulate_lock

    if not _cm or not _cm.channels:
        return {"error": "Bridge 未就绪"}

    if _simulate_lock:
        return {"error": "Agent 正在执行其他任务，请稍后再试"}

    _simulate_lock = True
    ch = _cm.channels[0]

    all_competitors = []
    errors = []

    try:
        # 并行扫描各竞品（每个竞品用独立 chatid）
        scan_tasks = []
        for i, competitor in enumerate(COMPETITORS):
            scan_tasks.append(_scan_single_competitor(ch, competitor, i))

        results = await asyncio.gather(*scan_tasks, return_exceptions=True)

        for i, result in enumerate(results):
            comp_name = COMPETITORS[i]["name"]
            if isinstance(result, Exception):
                errors.append(f"{comp_name}: 异常 {result}")
            elif isinstance(result, dict):
                if result.get("data"):
                    all_competitors.append(result["data"])
                if result.get("error"):
                    errors.append(f"{comp_name}: {result['error']}")

        result = {
            "competitors": all_competitors,
            "scan_summary": f"并行扫描{len(all_competitors)}个竞品",
        }
        if errors:
            result["errors"] = errors

        # 推送企微通知
        msg = f"📊 **竞品扫描完成（并行）**\n\n"
        msg += f"✅ 成功: {len(all_competitors)} 个竞品\n"
        for comp in all_competitors:
            feature_count = sum(len(p.get("features", [])) for p in comp.get("core_features", []))
            unique_count = len(comp.get("unique_features", []))
            msg += f"  - {comp['name']}: {feature_count} 核心功能, {unique_count} 差异化功能\n"
        if errors:
            msg += f"⚠️ 失败: {len(errors)} 个\n"
            msg += "失败详情:\n" + "\n".join(f"  - {e}" for e in errors[:10])
        await _notify_wecom(msg)

        return result

    except Exception as e:
        log.error("scan_competitors 异常: %s", e)
        await _notify_wecom(f"❌ **竞品扫描异常**\n\n{str(e)[:200]}")
        return {"error": str(e)}
    finally:
        _simulate_lock = False


async def _scan_single_competitor(ch, competitor: dict, index: int) -> dict:
    """扫描单个竞品（使用独立 chatid 实现并行）"""
    comp_name = competitor["name"]
    chatid = f"_lab_competitor_{index}"

    try:
        proc = await ch.pool.get_or_create(chatid, cwd=LAB_CWD, mode="full")
        prompt = _build_competitor_scan_prompt(competitor)
        reply = await proc.send(prompt, timeout=180)

        if not reply or not reply.strip():
            return {"data": None, "error": "Agent 无响应"}

        try:
            comp_result = _extract_json(reply)
            comp_result["name"] = comp_name
            return {"data": comp_result, "error": None}
        except (json.JSONDecodeError, ValueError):
            return {"data": None, "error": "JSON 解析失败"}

    except Exception as e:
        log.error("[scan_competitors] %s 异常: %s", comp_name, e)
        return {"data": None, "error": str(e)}
