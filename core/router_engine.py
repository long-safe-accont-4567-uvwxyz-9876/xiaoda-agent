"""统一路由引擎 — 合并三个路由入口（@mention / 关键词 / 默认）为单一决策流。

决策顺序：显式 @mention → 否定模式 → 自指模式 → 语音模式 → 关键词意图 → 默认 xiaoda
返回 RoutingDecision 数据类，所有出口共用。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Literal

from loguru import logger


@dataclass
class RoutingDecision:
    """路由决策结果。

    Attributes:
        agent_names: 目标 Agent 名称列表（如 ["xiaoda"], ["keli", "xiaolang"]）
        mode: 调度模式 — single 单 Agent / parallel 并行 / task_graph 任务图
        reasoning: 路由理由（可选，用于调试和审计）
    """

    agent_names: list[str]
    mode: Literal["single", "parallel", "task_graph"]
    reasoning: str = ""


# ── 路由规则定义 ──────────────────────────────────────────────────

# @mention 映射（默认值，运行时会合并用户自定义 display_name）
_DEFAULT_MENTION_MAP = {
    "@可莉": "keli",
    "@银狼": "xiaolang",
    "@昔涟": "xilian",
    "@尼可": "nike",
    "@纳西妲": "xiaoda",
}

# 否定模式中的 agent 别名（内部名 + 默认 display_name）
_DEFAULT_AGENT_ALIASES = {
    "keli": ["可莉", "xiaoli"],
    "xiaolang": ["银狼", "xiaolang"],
    "xilian": ["昔涟", "xilian"],
    "nike": ["尼可", "nike"],
}

# 自指模式：用户让纳西妲自己做事
SELF_TARGET_PATTERNS = [
    (r"(?:你|你自己|亲自)(?:去|来|帮我|帮我查|查|搜|找|看看|检查)", "xiaoda"),
]

# 语音相关模式
VOICE_PATTERNS = [
    r"(?:语音|声音|说话|朗读|念|读|听你|听听|发语音|生成语音|语音回复|说给我听|念出来)",
]


def _build_mention_map() -> dict[str, str]:
    """构建 @mention 映射（含用户自定义 display_name）。"""
    from config import get_agent_display_name
    m = dict(_DEFAULT_MENTION_MAP)
    for name in ("xiaoda", "keli", "xiaolang", "xilian", "nike"):
        dn = get_agent_display_name(name)
        key = f"@{dn}"
        if key not in m:
            m[key] = name
    return m


def _build_agent_names_pattern() -> str:
    """构建匹配所有 agent 名称的正则片段（内部名 + 所有 display_name）。"""
    from config import get_agent_display_name
    names: set[str] = set()
    for name in ("xiaoda", "keli", "xiaolang", "xilian", "nike"):
        names.add(name)
        dn = get_agent_display_name(name)
        if dn:
            names.add(dn)
    # 也加入默认别名
    for aliases in _DEFAULT_AGENT_ALIASES.values():
        names.update(aliases)
    return "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))


def _build_negative_patterns() -> list[str]:
    """构建否定模式（含用户自定义 display_name）。"""
    names_pat = _build_agent_names_pattern()
    # 排除 xiaoda（否定模式只针对子代理）
    from config import get_agent_display_name
    sub_names: set[str] = set()
    for name in ("keli", "xiaolang", "xilian", "nike"):
        sub_names.add(name)
        dn = get_agent_display_name(name)
        if dn:
            sub_names.add(dn)
        sub_names.update(_DEFAULT_AGENT_ALIASES.get(name, []))
    sub_pat = "|".join(re.escape(n) for n in sorted(sub_names, key=len, reverse=True))
    return [rf"(?:不|别|不要|不用)\s*(?:让|叫|请)?\s*(?:{sub_pat})"]


def _build_keyword_patterns() -> list[tuple[str, str]]:
    """构建关键词意图模式（含用户自定义 display_name）。"""
    from config import get_agent_display_name
    patterns: list[tuple[str, str]] = []
    agent_keywords: dict[str, list[str]] = {
        "xiaolang": ["银狼", "xiaolang"],
        "keli": ["可莉", "xiaoli", "小炸弹"],
        "xilian": ["昔涟", "xilian", "记忆"],
        "nike": ["尼可", "nike"],
        "xiaoda": ["纳西妲", "草神", "小草神"],
    }
    for name, keywords in agent_keywords.items():
        dn = get_agent_display_name(name)
        if dn and dn not in keywords:
            keywords.append(dn)
        kw_pat = "|".join(re.escape(k) for k in keywords)
        patterns.append((rf"(?:让|叫|请|麻烦|找|切换到)\s*(?:{kw_pat})", name))
        if name == "xiaoda":
            continue
        # "X帮/来/去..." 模式
        patterns.append((rf"(?:{kw_pat})(?:帮|来|去|看一下|看看|检查|巡检|执行|处理|搜|查|找|搜索|研究|分析|计算|炸|boom)", name))
    return patterns


class RouterEngine:
    """统一路由引擎，合并 @mention / 关键词 / 默认三入口。"""

    def __init__(self, belief_router: Any | None=None) -> None:
        """
        Args:
            belief_router: 可选的 BeliefRouter 实例（Thompson Sampling），
                          灰度期仅 owner 会话启用，通过 ROUTER_ENGINE 环境变量控制。
        """
        self._belief_router = belief_router
        self._use_belief = os.getenv("ROUTER_ENGINE", "legacy") == "new" and belief_router is not None

    def decide(self, user_input: str, user_id: str = "",
               current_target: str | None = None) -> RoutingDecision:
        """根据用户输入决定路由目标。

        Args:
            user_input: 用户输入文本
            user_id: 用户 ID（用于 belief_router 灰度）
            current_target: 当前聊天目标（可选，用于上下文感知）

        Returns:
            RoutingDecision 路由决策
        """
        # 1. 显式 @mention
        targets = self._match_mentions(user_input)
        if targets:
            return RoutingDecision(
                agent_names=targets,
                mode="single" if len(targets) == 1 else "parallel",
                reasoning=f"@mention: {targets}",
            )

        q = user_input.lower()

        # 2. 否定模式 → xiaoda
        for pat in _build_negative_patterns():
            if re.search(pat, q):
                return RoutingDecision(
                    agent_names=["xiaoda"],
                    mode="single",
                    reasoning="negative_pattern → xiaoda",
                )

        # 3. 自指模式
        for pattern, target in SELF_TARGET_PATTERNS:
            if re.search(pattern, q):
                return RoutingDecision(
                    agent_names=[target],
                    mode="single",
                    reasoning=f"self_target_pattern → {target}",
                )

        # 4. 语音模式 → xiaoda
        for pattern in VOICE_PATTERNS:
            if re.search(pattern, q):
                return RoutingDecision(
                    agent_names=["xiaoda"],
                    mode="single",
                    reasoning="voice_pattern → xiaoda",
                )

        # 5. 关键词意图
        # 5a. 尝试 BeliefRouter（灰度）
        if self._use_belief:
            try:
                belief_target = self._belief_router.decide(user_input, user_id)
                if belief_target and belief_target != "xiaoda":
                    return RoutingDecision(
                        agent_names=[belief_target],
                        mode="single",
                        reasoning=f"belief_router → {belief_target}",
                    )
            except Exception as e:
                logger.debug("router.belief_fallback", error=str(e))

        # 5b. 硬编码关键词匹配
        for pattern, target in _build_keyword_patterns():
            if re.search(pattern, q):
                return RoutingDecision(
                    agent_names=[target],
                    mode="single",
                    reasoning=f"keyword_pattern → {target}",
                )

        # 6. 默认 → xiaoda
        return RoutingDecision(
            agent_names=["xiaoda"],
            mode="single",
            reasoning="default → xiaoda",
        )

    @staticmethod
    def _match_mentions(user_input: str) -> list[str]:
        """提取 @mention 目标列表。"""
        targets = []
        for mention, agent in _build_mention_map().items():
            if mention in user_input:
                targets.append(agent)
        return targets
