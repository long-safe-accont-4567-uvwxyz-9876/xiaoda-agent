"""统一路由引擎 — 合并三个路由入口（@mention / 关键词 / 默认）为单一决策流。

决策顺序：显式 @mention → 否定模式 → 自指模式 → 语音模式 → 关键词意图 → 默认 nahida
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
        agent_names: 目标 Agent 名称列表（如 ["nahida"], ["keli", "yinlang"]）
        mode: 调度模式 — single 单 Agent / parallel 并行 / task_graph 任务图
        reasoning: 路由理由（可选，用于调试和审计）
    """

    agent_names: list[str]
    mode: Literal["single", "parallel", "task_graph"]
    reasoning: str = ""


# ── 路由规则定义 ──────────────────────────────────────────────────

# @mention 映射
MENTION_MAP = {
    "@可莉": "keli",
    "@银狼": "yinlang",
    "@昔涟": "xilian",
    "@尼可": "nike",
    "@纳西妲": "nahida",
}

# 否定模式：明确不要某个子代理 → 回到 nahida
NEGATIVE_PATTERNS = [
    r"(?:不|别|不要|不用)\s*(?:让|叫|请)?\s*(?:可莉|klee|银狼|yinlang|昔涟|xilian|尼可|nike)",
]

# 自指模式：用户让纳西妲自己做事
SELF_TARGET_PATTERNS = [
    (r"(?:你|你自己|亲自)(?:去|来|帮我|帮我查|查|搜|找|看看|检查)", "nahida"),
]

# 语音相关模式
VOICE_PATTERNS = [
    r"(?:语音|声音|说话|朗读|念|读|听你|听听|发语音|生成语音|语音回复|说给我听|念出来)",
]

# 关键词意图模式
KEYWORD_PATTERNS = [
    (r"(?:让|叫|请|麻烦|找|切换到)\s*(?:银狼|yinlang)", "yinlang"),
    (r"(?:让|叫|请|麻烦|找|切换到)\s*(?:可莉|klee|小炸弹)", "keli"),
    (r"(?:让|叫|请|麻烦|找|切换到)\s*(?:昔涟|xilian|记忆)", "xilian"),
    (r"(?:让|叫|请|麻烦|找|切换到)\s*(?:尼可|nike)", "nike"),
    (r"(?:让|叫|请|麻烦|找|切换到)\s*(?:纳西妲|草神|小草神)", "nahida"),
    (r"(?:银狼|yinlang)(?:帮|来|去|看一下|看看|检查|巡检|执行|处理)", "yinlang"),
    (r"(?:可莉|klee|小炸弹)(?:帮|来|去|炸|boom)", "keli"),
    (r"(?:昔涟|xilian)(?:帮|搜|查|找|搜索)", "xilian"),
    (r"(?:尼可|nike)(?:帮|研究|分析|计算)", "nike"),
]


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

        # 2. 否定模式 → nahida
        for pat in NEGATIVE_PATTERNS:
            if re.search(pat, q):
                return RoutingDecision(
                    agent_names=["nahida"],
                    mode="single",
                    reasoning="negative_pattern → nahida",
                )

        # 3. 自指模式
        for pattern, target in SELF_TARGET_PATTERNS:
            if re.search(pattern, q):
                return RoutingDecision(
                    agent_names=[target],
                    mode="single",
                    reasoning=f"self_target_pattern → {target}",
                )

        # 4. 语音模式 → nahida
        for pattern in VOICE_PATTERNS:
            if re.search(pattern, q):
                return RoutingDecision(
                    agent_names=["nahida"],
                    mode="single",
                    reasoning="voice_pattern → nahida",
                )

        # 5. 关键词意图
        # 5a. 尝试 BeliefRouter（灰度）
        if self._use_belief:
            try:
                belief_target = self._belief_router.decide(user_input, user_id)
                if belief_target and belief_target != "nahida":
                    return RoutingDecision(
                        agent_names=[belief_target],
                        mode="single",
                        reasoning=f"belief_router → {belief_target}",
                    )
            except Exception as e:
                logger.debug("router.belief_fallback", error=str(e))

        # 5b. 硬编码关键词匹配
        for pattern, target in KEYWORD_PATTERNS:
            if re.search(pattern, q):
                return RoutingDecision(
                    agent_names=[target],
                    mode="single",
                    reasoning=f"keyword_pattern → {target}",
                )

        # 6. 默认 → nahida
        return RoutingDecision(
            agent_names=["nahida"],
            mode="single",
            reasoning="default → nahida",
        )

    @staticmethod
    def _match_mentions(user_input: str) -> list[str]:
        """提取 @mention 目标列表。"""
        targets = []
        for mention, agent in MENTION_MAP.items():
            if mention in user_input:
                targets.append(agent)
        return targets
