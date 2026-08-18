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
        agent_names: 目标 Agent 名称列表（如 ["xiaoda"], ["xiaoli", "xiaolang"]）
        mode: 调度模式 — single 单 Agent / parallel 并行 / task_graph 任务图
        reasoning: 路由理由（可选，用于调试和审计）
    """

    agent_names: list[str]
    mode: Literal["single", "parallel", "task_graph"]
    reasoning: str = ""


# ── 路由规则定义 ──────────────────────────────────────────────────

# @mention 映射（默认值，运行时会合并用户自定义 display_name）
_DEFAULT_MENTION_MAP = {
    "@小莉": "xiaoli",
    "@小狼": "xiaolang",
    "@小涟": "xiaolian",
    "@小可": "xiaoke",
    "@小妲": "xiaoda",
    # 兼容旧名字
    "@可莉": "xiaoli",
    "@银狼": "xiaolang",
    "@昔涟": "xiaolian",
    "@尼可": "xiaoke",
    "@纳西妲": "xiaoda",
}

# 否定模式中的 agent 别名（内部名 + 默认 display_name）
_DEFAULT_AGENT_ALIASES = {
    "xiaoli": ["小莉", "xiaoli"],
    "xiaolang": ["小狼", "xiaolang"],
    "xiaolian": ["小涟", "xiaolian"],
    "xiaoke": ["小可", "xiaoke", "nike"],
}

# 自指模式：用户让小妲自己做事
SELF_TARGET_PATTERNS = [
    (r"(?:你|你自己|亲自)(?:去|来|帮我|帮我查|查|搜|找|看看|检查)", "xiaoda"),
]

# 语音相关模式
VOICE_PATTERNS = [
    r"(?:语音|声音|说话|朗读|念给|念出来|听你|听听|发语音|生成语音|语音回复|说给我听|读给我|读出来|给我读)",
]


def _build_mention_map() -> dict[str, str]:
    """构建 @mention 映射（含用户自定义 display_name）。"""
    from config import get_agent_display_name
    m = dict(_DEFAULT_MENTION_MAP)
    for name in ("xiaoda", "xiaoli", "xiaolang", "xiaolian", "xiaoke"):
        dn = get_agent_display_name(name)
        key = f"@{dn}"
        if key not in m:
            m[key] = name
    return m


def _build_agent_names_pattern() -> str:
    """构建匹配所有 agent 名称的正则片段（内部名 + 所有 display_name）。"""
    from config import get_agent_display_name
    names: set[str] = set()
    for name in ("xiaoda", "xiaoli", "xiaolang", "xiaolian", "xiaoke"):
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
    _names_pat = _build_agent_names_pattern()
    # 排除 xiaoda（否定模式只针对子代理）
    from config import get_agent_display_name
    sub_names: set[str] = set()
    for name in ("xiaoli", "xiaolang", "xiaolian", "xiaoke"):
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
        "xiaolang": ["小狼", "xiaolang"],
        "xiaoli": ["小莉", "xiaoli", "小炸弹"],
        "xiaolian": ["小涟", "xiaolian"],
        "xiaoke": ["小可", "xiaoke", "nike"],
        "xiaoda": ["小妲", "xiaoda", "记忆", "回忆", "记得"],
    }
    for name, keywords in agent_keywords.items():
        dn = get_agent_display_name(name)
        if dn and dn not in keywords:
            keywords.append(dn)
        kw_pat = "|".join(re.escape(k) for k in keywords)
        patterns.append((rf"(?:让|叫|请|麻烦|找|切换到)\s*(?:{kw_pat})", name))
        if name == "xiaoda":
            # xiaoda 的记忆/回忆等关键词单独匹配（无需前缀动词）
            # "回忆xxx"/"记得xxx"/"记忆xxx" 直接路由到 xiaoda
            _memory_kws = [k for k in keywords if k in ("记忆", "回忆", "记得", "recall", "remember")]
            if _memory_kws:
                _mem_pat = "|".join(re.escape(k) for k in _memory_kws)
                patterns.append((rf"(?:{_mem_pat})", name))
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
        self._negative_patterns: list[str] | None = None
        self._keyword_patterns: list[tuple[str, str]] | None = None
        self._mention_map: dict[str, str] | None = None

    def _ensure_patterns_cached(self) -> None:
        """延迟构建并缓存正则模式。config 不热更新，缓存安全。"""
        if self._negative_patterns is None:
            self._negative_patterns = _build_negative_patterns()
        if self._keyword_patterns is None:
            self._keyword_patterns = _build_keyword_patterns()
        if self._mention_map is None:
            self._mention_map = _build_mention_map()

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
        self._ensure_patterns_cached()
        targets = self._match_mentions(user_input)
        if targets:
            return RoutingDecision(
                agent_names=targets,
                mode="single" if len(targets) == 1 else "parallel",
                reasoning=f"@mention: {targets}",
            )

        q = user_input.lower()

        # 2. 否定模式 → xiaoda
        for pat in self._negative_patterns:
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
        # 5a. 硬编码关键词匹配（优先于 BeliefRouter，确保记忆等关键查询不被随机路由）
        for pattern, target in self._keyword_patterns:
            if re.search(pattern, q):
                return RoutingDecision(
                    agent_names=[target],
                    mode="single",
                    reasoning=f"keyword_pattern → {target}",
                )

        # 5b. 尝试 BeliefRouter（灰度，仅当关键词无匹配时使用）
        if self._use_belief:
            try:
                belief_target = self._belief_router.select_agent()
                if belief_target and belief_target != "xiaoda":
                    return RoutingDecision(
                        agent_names=[belief_target],
                        mode="single",
                        reasoning=f"belief_router → {belief_target}",
                    )
            except Exception as e:
                logger.debug("router.belief_fallback", error=str(e))

        # 6. 默认 → xiaoda
        return RoutingDecision(
            agent_names=["xiaoda"],
            mode="single",
            reasoning="default → xiaoda",
        )

    def _match_mentions(self, user_input: str) -> list[str]:
        """提取 @mention 目标列表（保序去重）。"""
        targets = []
        for mention, agent in (self._mention_map or _build_mention_map()).items():
            if mention in user_input:
                targets.append(agent)
        return list(dict.fromkeys(targets))

    # ── LLM 路由 ──────────────────────────────────────────────────

    async def decide_with_llm(self, user_input: str, user_id: str = "",
                              current_target: str | None = None) -> RoutingDecision:
        """LLM 驱动的子代理路由。

        流程：显式信号（@mention/否定/自指/语音）→ LLM 分类 → 关键词兜底 → 默认 xiaoda
        LLM 不可用或超时时自动降级到关键词匹配。
        """
        # 1-4. 显式信号检查（与 decide() 相同，这些是可靠信号，不需要 LLM）
        self._ensure_patterns_cached()

        targets = self._match_mentions(user_input)
        if targets:
            return RoutingDecision(
                agent_names=targets,
                mode="single" if len(targets) == 1 else "parallel",
                reasoning=f"@mention: {targets}",
            )

        q = user_input.lower()

        for pat in self._negative_patterns:
            if re.search(pat, q):
                return RoutingDecision(
                    agent_names=["xiaoda"], mode="single",
                    reasoning="negative_pattern → xiaoda",
                )

        for pattern, target in SELF_TARGET_PATTERNS:
            if re.search(pattern, q):
                return RoutingDecision(
                    agent_names=[target], mode="single",
                    reasoning=f"self_target_pattern → {target}",
                )

        for pattern in VOICE_PATTERNS:
            if re.search(pattern, q):
                return RoutingDecision(
                    agent_names=["xiaoda"], mode="single",
                    reasoning="voice_pattern → xiaoda",
                )

        # 5. LLM 意图分类（核心改进：让 LLM 判断子代理，而非关键词匹配）
        try:
            import config as _cfg
            llm_classify = getattr(_cfg, "INTENT_LLM_CLASSIFY", False)
            timeout = getattr(_cfg, "INTENT_CLASSIFY_TIMEOUT", 15.0)
        except (ImportError, AttributeError):
            llm_classify = False
            timeout = 15.0

        if llm_classify:
            agent = await self._classify_sub_agent_with_llm(user_input, timeout)
            if agent:
                return RoutingDecision(
                    agent_names=[agent], mode="single",
                    reasoning=f"llm_classify → {agent}",
                )
            # LLM 失败，降级到关键词匹配
            logger.debug("router.llm_classify_failed_fallback_to_keywords")

        # 5b. 关键词兜底（LLM 不可用或失败时）
        for pattern, target in self._keyword_patterns:
            if re.search(pattern, q):
                return RoutingDecision(
                    agent_names=[target], mode="single",
                    reasoning=f"keyword_pattern → {target}",
                )

        # 5c. BeliefRouter
        if self._use_belief:
            try:
                belief_target = self._belief_router.select_agent()
                if belief_target and belief_target != "xiaoda":
                    return RoutingDecision(
                        agent_names=[belief_target], mode="single",
                        reasoning=f"belief_router → {belief_target}",
                    )
            except Exception as e:
                logger.debug("router.belief_fallback", error=str(e))

        # 6. 默认 → xiaoda
        return RoutingDecision(
            agent_names=["xiaoda"], mode="single",
            reasoning="default → xiaoda",
        )

    async def _classify_sub_agent_with_llm(self, user_input: str,
                                            timeout: float = 15.0) -> str | None:
        """调用 LLM 判断子代理路由。返回 agent 名称或 None（失败时）。"""
        import os
        try:
            import httpx
        except ImportError:
            return None

        api_key = os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("EMBED_API_KEY", "")
        if not api_key:
            return None

        base_url = os.getenv("QUERY_TRANSFORM_BASE_URL", "https://api.siliconflow.cn/v1")
        model = os.getenv("QUERY_TRANSFORM_MODEL", "THUDM/GLM-4-9B-0414")

        # 子代理描述（与 _AGENT_TO_TASK_TYPE 对应）
        from config import get_agent_display_name
        agents_info = [
            ("xiaoda", get_agent_display_name("xiaoda") or "小妲", "记忆检索、回忆、个人历史、时间相关查询、通用闲聊（默认）"),
            ("xiaolang", get_agent_display_name("xiaolang") or "小狼", "编程、调试、代码、技术问题"),
            ("xiaoke", get_agent_display_name("xiaoke") or "小可", "学术研究、论文、调研"),
            ("xiaolian", get_agent_display_name("xiaolian") or "小涟", "信息搜索、查找资料"),
            ("xiaoli", get_agent_display_name("xiaoli") or "小莉", "情感陪伴、聊天、安慰"),
        ]
        agents_block = "\n".join(
            f"- {key}（{display}）: {desc}" for key, display, desc in agents_info
        )

        prompt = (
            f"你是一个路由分类器。根据用户消息判断应该由哪个子代理处理。\n\n"
            f"可用子代理：\n{agents_block}\n\n"
            f"用户消息: \"{user_input[:500]}\"\n\n"
            f"只回复子代理名称（如 xiaoda），不要其他内容："
        )

        try:
            # G4: 共享 httpx.AsyncClient（连接池复用 + HTTP/2），单次请求级别覆盖 timeout
            from utils.http_pool import get_shared_client
            client = get_shared_client()
            response = await client.post(
                f"{base_url}/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.0,
                    "max_tokens": 20,
                },
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(timeout),
            )
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices", [])
            if not choices:
                return None
            content = choices[0].get("message", {}).get("content", "").strip().lower()
            if not content:
                return None
            # 匹配返回的 agent 名称
            valid_agents = {"xiaoda", "xiaolang", "xiaoke", "xiaolian", "xiaoli"}
            for agent in valid_agents:
                if agent in content:
                    logger.info("router.llm_classified",
                                agent=agent, input_preview=user_input[:50])
                    return agent
            logger.warning("router.llm_classify_unrecognized", content=content[:50])
            return None
        except Exception as e:
            logger.warning("router.llm_classify_error",
                           error=str(e), error_type=type(e).__name__)
            return None


# ── 公共导出（保持向后兼容）────────────────────────────────────────
try:
    MENTION_MAP = _build_mention_map()
except Exception:
    MENTION_MAP = dict(_DEFAULT_MENTION_MAP)
