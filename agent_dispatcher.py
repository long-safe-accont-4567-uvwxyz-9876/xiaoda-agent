import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol, runtime_checkable

from loguru import logger

from config import get_agent_display_name
from core.message import AgentMessage
from emotion.emoji_config import get_status_msg
from emotion.tts_engine import TTSEngine
from tool_engine.tool_call_handler import _extract_path_from_args
from tool_engine.tool_executor import ToolExecutor, ToolResult
from tool_engine.tool_guardrails import get_tool_guardrails
from tool_engine.tool_registry import to_openai_tools
from tool_engine.tool_repair import ToolCallRepair
from utils.credential_pool import CredentialPool
from utils.llm_cleanup import deduplicate_multi_reply
from utils.text_utils import has_dsml_tool_calls, humanize, parse_dsml_tool_calls, strip_dsml, strip_reasoning

# ── 拆分：tool_call_extractors + sub_agent 抽出（逐字节搬移）──
# 同名 re-export 保持兼容（契约见 tests/test_dispatcher_split.py）。
from agent_core.tool_call_extractors import (  # noqa: F401
    ExtractedToolCall, ToolCallExtractor, StandardExtractor,
    DsmlExtractor, ResourceBackend,
)
from agent_core.sub_agent import (  # noqa: F401
    SubAgent, SubAgentConfig,
    DELEGATE_BLOCKED_TOOLS, _RESOURCE_PATH_TOOLS,
    SUB_AGENT_PROFILE_TOOLS, SUB_AGENT_MEMORY_TOOL,
    SUB_AGENT_MESSAGE_TOOL, SUB_AGENT_EXTRA_TOOLS,
    _safe_log_path, _read_env_key, _is_tool_unsupported_error,
)

# RouterEngine agent name → task_type 反向映射
# 用于 classify_task 委托 RouterEngine 后保持返回格式一致（task_type 字符串）
_AGENT_TO_TASK_TYPE = {
    "xiaolang": "debug",       # 编程/调试/系统
    "xiaoke": "research",      # 学术研究
    "xiaolian": "info_search", # 信息搜索
    "xiaoli": "emotional",     # 情感陪伴
    "xiaoda": "memory",        # 记忆检索/主Agent
}


class AgentDispatcher:
    """管理多个子 Agent 的注册、调度与降级调用。"""
    def __init__(self, tts: TTSEngine,
                 tool_executor: ToolExecutor | None = None,
                 tool_repair: ToolCallRepair | None = None,
                 delegate_callback: Any | None=None,
                 core: Any | None=None) -> None:
        self._tts = tts
        self._tool_executor = tool_executor
        self._tool_repair = tool_repair
        self._delegate_callback = delegate_callback
        self._core = core
        self._agents: dict[str, SubAgent] = {}
        self._router_engine = None  # 懒加载 RouterEngine（权威路由源），由 _get_router_engine 初始化

    async def register(self, config: SubAgentConfig) -> bool:
        if config.name in self._agents:
            logger.warning("dispatcher.already_registered", name=config.name)
            return False

        agent = SubAgent(
            config=config,
            tts=self._tts,
            tool_executor=self._tool_executor,
            tool_repair=self._tool_repair,
            delegate_callback=self._delegate_callback,
            core=self._core,
        )
        await agent.init()

        # 降级模式下仍注册子 agent（探活失败但保留配置，实际调用时回退到主体 agent）
        if not agent.available and not agent.degraded:
            logger.warning("dispatcher.register_unavailable", name=config.name)
            return False

        self._agents[config.name] = agent
        if agent.degraded:
            logger.warning("dispatcher.registered_degraded", name=config.name, display_name=config.display_name)
        else:
            logger.info("dispatcher.registered", name=config.name, display_name=config.display_name)
        return True

    def unregister(self, name: str) -> bool:
        if name not in self._agents:
            return False
        del self._agents[name]
        logger.info("dispatcher.unregistered", name=name)
        return True

    async def close(self) -> None:
        for agent in self._agents.values():
            if hasattr(agent, 'close'):
                try:
                    await agent.close()
                except (OSError, RuntimeError):
                    logger.debug("agent_dispatcher.close_sub_agent_error", exc_info=True)

    async def dispatch_single(self, name: str, task: str, context: str = "", status_callback: Any | None=None, address_term: str = "爸爸", extra_system_prompt: str = "") -> str | None:
        """单子代理调度（原 dispatch 方法）。

        保留为独立方法以与并行调度（SubAgentManagerMixin.parallel_dispatch）区分；
        ``dispatch`` 仍作为向后兼容别名指向本方法。
        """
        from agent_core.subagents import SubAgentInvocation

        invocation = SubAgentInvocation(target=name, task=task, context=context)
        agent = self._agents.get(invocation.target)
        if not agent:
            logger.warning("dispatcher.agent_not_found", name=invocation.target)
            return None
        return await self._chat_with_scope(
            agent,
            invocation.task,
            context=invocation.context,
            status_callback=status_callback,
            address_term=address_term,
            extra_system_prompt=extra_system_prompt,
        )

    async def _chat_with_scope(self, agent: SubAgent, message: str, **kwargs: Any) -> str:
        from memory.scope import Scope, bind_scope, current_scope_or_default, reset_scope

        parent = current_scope_or_default()
        invocation = kwargs.get("invocation")
        isolated = agent.config.memory_scope == "isolated"
        session_id = f"{parent.session_id}:{agent.config.name}" if isolated else parent.session_id
        agent_id = agent.config.name if isolated else parent.agent_id
        request_id = invocation.request_id if invocation is not None and invocation.request_id else f"{parent.request_id}:{agent.config.name}"
        token = bind_scope(Scope(user_id=parent.user_id, session_id=session_id, agent_id=agent_id, request_id=request_id))
        try:
            return await agent.chat(message, **kwargs)
        finally:
            reset_scope(token)

    async def dispatch_invocation(self, invocation: Any, status_callback: Any | None = None, address_term: str = "爸爸", extra_system_prompt: str = "") -> Any:
        from agent_core.subagents import SubAgentInvocation, SubAgentInvocationResult

        if not isinstance(invocation, SubAgentInvocation):
            raise TypeError("invocation must be SubAgentInvocation")
        agent = self._agents.get(invocation.target)
        if not agent:
            return SubAgentInvocationResult(target=invocation.target, status="unavailable", error_code="SUB_AGENT_UNAVAILABLE", error_message="agent unavailable")
        try:
            report = await asyncio.wait_for(
                self._chat_with_scope(
                    agent,
                    invocation.task,
                    context=invocation.context,
                    status_callback=status_callback,
                    address_term=address_term,
                    extra_system_prompt=extra_system_prompt,
                    invocation=invocation,
                ),
                timeout=invocation.timeout_seconds,
            )
        except asyncio.CancelledError:
            return SubAgentInvocationResult(target=invocation.target, status="cancelled", error_code="SUB_AGENT_CANCELLED", error_message="agent invocation cancelled")
        except TimeoutError:
            logger.warning(
                "dispatcher.invocation_timeout",
                target=invocation.target,
                request_id=invocation.request_id or "",
                timeout_seconds=invocation.timeout_seconds,
                memory_scope=agent.config.memory_scope or "shared",
            )
            return SubAgentInvocationResult(target=invocation.target, status="timeout", error_code="SUB_AGENT_TIMEOUT", error_message="agent invocation timed out")
        except Exception:
            logger.exception("dispatcher.invocation_failed", name=invocation.target)
            return SubAgentInvocationResult(target=invocation.target, status="failed", error_code="SUB_AGENT_EXECUTION_FAILED", error_message="agent invocation failed")
        if not isinstance(report, str) or not report.strip():
            return SubAgentInvocationResult(target=invocation.target, status="failed", error_code="SUB_AGENT_EMPTY_RESULT", error_message="agent returned no final report")
        return SubAgentInvocationResult.completed(target=invocation.target, final_report=report)

    # 向后兼容别名：保留 dispatch 指向 dispatch_single
    dispatch = dispatch_single

    def get_agent(self, name: str) -> SubAgent | None:
        return self._agents.get(name)

    def list_agents(self) -> list[dict]:
        return [
            {"name": name, "display_name": agent.config.display_name}
            for name, agent in self._agents.items()
        ]

    @property
    def agent_names(self) -> list[str]:
        return list(self._agents.keys())

    def refresh_all_clients(self) -> int:
        count = 0
        for name, agent in self._agents.items():
            agent._router = getattr(self._core, "router", None)
            agent._initialized = agent._router is not None
            agent._degraded = not agent._initialized
            if agent._initialized:
                count += 1
                logger.info("sub_agent.router_refreshed", name=name)
        return count

    def route_task(self, task_type: str, input_text: str) -> str:
        """根据任务类型路由到对应子代理

        参考 Trae SOLO 模式：任务→代理 1:1 绑定

        :param task_type: 任务类型
            - "frontend" → 小狼（xiaolang，编程/系统）
            - "backend" → 小狼（xiaolang）
            - "debug" → 小狼（xiaolang）
            - "security" → 小狼（xiaolang，系统管理）
            - "test" → 小狼（xiaolang）
            - "info_search" → 小涟（xiaolian，信息助手）
            - "memory" → 小妲（xiaoda，记忆检索）
            - "hardware" → 小狼（xiaolang）
            - "emotional" → 小莉（xiaoli，萌系陪伴）
            - "research" → 小可（xiaoke，学术研究）
            - "general" → 默认（xiaoli）
        :param input_text: 用户输入文本
        :returns: 子代理名称（target）
        """
        routing = self._load_routing_config()
        target = routing.get(task_type, routing.get("general", "xiaoli"))

        # 验证目标代理可用
        agent = self.get_agent(target)
        if not agent or not agent.available:
            # I7: 智能回退 — 基于工作履历从可用 agent 中选成功率最高的
            default = routing.get("general", "xiaoli")
            fallback = default
            try:
                from core.agent_work_record import get_work_recorder
                available_agents = [n for n, a in self._agents.items()
                                    if a and a.available and n != target]
                if available_agents:
                    best = get_work_recorder().get_best_agent(
                        available_agents, task_type=task_type)
                    if best:
                        fallback = best
            except (ImportError, AttributeError, TypeError):
                logger.debug("agent.work_record_routing_unavailable", exc_info=True)  # work_record 不可用时使用默认路由
            if fallback != target:
                logger.info("agent.task_route_fallback",
                            task_type=task_type,
                            requested_target=target,
                            fallback_target=fallback)
                return fallback

        logger.info("agent.task_route", task_type=task_type, target=target)
        return target

    _routing_config_cache: tuple[float, dict] | None = None  # (mtime, config)

    def _load_routing_config(self) -> dict[str, str]:
        """从 config/agent_routing.json 加载路由配置（带文件修改时间缓存）

        若文件不存在或加载失败，使用内置默认配置。
        """
        import json
        from pathlib import Path

        config_path = Path(__file__).parent / "config" / "agent_routing.json"
        if config_path.exists():
            try:
                mtime = config_path.stat().st_mtime
                if (self._routing_config_cache
                        and self._routing_config_cache[0] == mtime):
                    return self._routing_config_cache[1]
                with open(config_path, encoding="utf-8") as f:
                    result = json.load(f)
                self._routing_config_cache = (mtime, result)
                return result
            except (OSError, json.JSONDecodeError, ValueError) as e:
                logger.warning("agent.routing_config_load_failed", error=str(e))

        # 默认路由
        return {
            "frontend": "xiaoke",
            "backend": "xiaoke",
            "debug": "xiaoke",
            "security": "xiaolang",
            "test": "xiaoke",
            "info_search": "xiaolian",
            "hardware": "xiaolang",
            "emotional": "xiaoli",
            "general": "xiaoli",
        }

    def _get_router_engine(self):
        """懒加载 RouterEngine 实例（无 belief_router，仅规则路由）。

        RouterEngine 作为权威路由源：classify_task 优先委托其决策，
        仅当其返回默认（xiaoda，无明确路由信号）时才回退到本地关键词分类。
        """
        if self._router_engine is None:
            from core.router_engine import RouterEngine
            self._router_engine = RouterEngine()
        return self._router_engine

    def classify_task(self, user_input: str) -> str:
        """根据用户输入自动分类任务类型

        已委托给 RouterEngine 作为权威路由源：先调用 RouterEngine.decide()，
        当其给出明确子代理路由（非 xiaoda）时反推 task_type；仅当 RouterEngine
        返回默认（xiaoda，表示无明确路由信号）时，才回退到本地关键词分类。

        :param user_input: 用户输入文本
        :returns: 任务类型（frontend/backend/debug/security/test/info_search/hardware/emotional/general）
        """
        # 委托给 RouterEngine（权威路由源）：明确子代理路由时反推 task_type
        # 注意：xiaoda 作为默认兜底路由时不应反推为 memory，仅当 RouterEngine
        # 通过关键词/mention 等明确路由到 xiaoda 时才视为 memory
        try:
            engine = self._get_router_engine()
            decision = engine.decide(user_input)
            for agent in decision.agent_names:
                if agent == "xiaoda" and decision.reasoning.startswith("default"):
                    continue  # 默认兜底路由，不反推 task_type，继续走本地分类
                task_type = _AGENT_TO_TASK_TYPE.get(agent)
                if task_type:
                    return task_type
        except (OSError, ValueError, RuntimeError) as e:
            logger.warning("classify_task.router_engine_delegate_failed", error=str(e)[:200])

        # 回退：本地关键词分类（RouterEngine 无明确路由信号时）
        text_lower = user_input.lower()

        # 关键词分类
        rules = [
            (["前端", "frontend", "vue", "react", "css", "html", "ui 设计"], "frontend"),
            (["后端", "backend", "api", "数据库", "python 服务", "fastapi"], "backend"),
            (["调试", "debug", "报错", "错误", "异常", "stack trace", "bug"], "debug"),
            (["安全", "security", "漏洞", "加密", "权限", "认证"], "security"),
            (["测试", "test", "pytest", "单测", "覆盖率"], "test"),
            (["搜索", "查询", "查找", "search", "browse", "网页"], "info_search"),
            (["回忆", "记得", "记忆", "recall", "remember", "记得吗", "上次", "昨天", "前几天", "上周", "上周"], "memory"),
            (["硬件", "gpio", "i2c", "传感器", "摄像头", "hardware"], "hardware"),
            (["难过", "开心", "生气", "焦虑", "陪伴", "聊天", "求安慰"], "emotional"),
        ]

        for keywords, task_type in rules:
            for kw in keywords:
                if kw in text_lower:
                    return task_type

        return "general"

    def classify_multi(self, user_input: str) -> list[str]:
        """检测用户输入中涉及的多个任务领域。

        返回去重后的任务类型列表，如 ["frontend", "security"]。
        单领域时返回单个元素的列表。
        """
        text_lower = user_input.lower()
        rules = [
            (["前端", "frontend", "vue", "react", "css", "html", "ui 设计"], "frontend"),
            (["后端", "backend", "api", "数据库", "python 服务", "fastapi"], "backend"),
            (["调试", "debug", "报错", "错误", "异常", "stack trace", "bug"], "debug"),
            (["安全", "security", "漏洞", "加密", "权限", "认证"], "security"),
            (["测试", "test", "pytest", "单测", "覆盖率"], "test"),
            (["搜索", "查询", "查找", "search", "browse", "网页"], "info_search"),
            (["回忆", "记得", "记忆", "recall", "remember", "记得吗", "上次", "昨天", "前几天", "上周"], "memory"),
            (["硬件", "gpio", "i2c", "传感器", "摄像头", "hardware"], "hardware"),
            (["难过", "开心", "生气", "焦虑", "陪伴", "聊天", "求安慰"], "emotional"),
        ]
        found: set[str] = set()
        for keywords, task_type in rules:
            for kw in keywords:
                if kw in text_lower:
                    found.add(task_type)
                    break
        return sorted(found) if found else ["general"]

    def route_multi(self, task_types: list[str]) -> dict:
        """多域组合路由 — 根据多个任务类型返回编排计划。

        Returns:
            {"targets": [...], "mode": "single|parallel_fanout|pipe|generate_verify",
             "synthesizer": "...", "verifier": "..."}
        """
        if len(task_types) == 1:
            # 单领域，使用 v1 路由
            target = self.route_task(task_types[0], "")
            return {"targets": [target], "mode": "single", "synthesizer": "", "verifier": ""}

        # 多领域，查 v2 配置
        v2_config = self._load_routing_v2_config()
        multi_domain = v2_config.get("multi_domain", {})

        # 尝试匹配组合键（如 "frontend+security"）
        combo_key = "+".join(task_types)
        plan = multi_domain.get(combo_key)
        if plan:
            return {
                "targets": plan.get("targets", []),
                "mode": plan.get("mode", "parallel_fanout"),
                "synthesizer": plan.get("synthesizer", ""),
                "verifier": plan.get("verifier", ""),
            }

        # 无精确匹配 → 各领域独立路由后去重（直接查配置，不检查可用性）
        routing = self._load_routing_config()
        targets = list(dict.fromkeys(
            routing.get(tt, routing.get("general", "xiaoli")) for tt in task_types))
        if len(targets) == 1:
            return {"targets": targets, "mode": "single", "synthesizer": "", "verifier": ""}
        return {"targets": targets, "mode": "parallel_fanout",
                "synthesizer": "xiaoda", "verifier": ""}

    def _load_routing_v2_config(self) -> dict:
        """从 config/agent_routing_v2.json 加载多域路由配置。"""
        import json
        from pathlib import Path

        config_path = Path(__file__).parent / "config" / "agent_routing_v2.json"
        if config_path.exists():
            try:
                with open(config_path, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError, ValueError) as e:
                logger.warning("agent.routing_v2_config_load_failed", error=str(e))
        return {"single_domain": {}, "multi_domain": {}, "operation_patterns": {}}
