"""SubAgent + SubAgentConfig + 常量 —— 拆分自 agent_dispatcher.py。

内容：SubAgentConfig 数据类、SubAgent 类（26 方法）、子代理常量、
辅助函数（_safe_log_path / _read_env_key / _is_tool_unsupported_error）、
J-Space Hook 全局量。函数体逐字节搬移。

外部消费者：web/agent_registry.py、core/bootstrap.py、
tool_engine/tool_call_handler.py、xiaoli_agent.py。
agent_dispatcher re-export 保持兼容。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from loguru import logger

from agent_core._shared import TIRED_MSG, _current_request_ctx
from agent_core.tool_call_extractors import (
    DsmlExtractor,
    StandardExtractor,
)
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
from utils.text_utils import humanize, strip_dsml, strip_reasoning

# J-Space Hook: 干预闭环
try:
    from config import ENABLE_J_SPACE_HOOKS
    if ENABLE_J_SPACE_HOOKS:
        from core.behavioral_signal import BehavioralSignalStream
        from core.intervention_loop import InterventionLoop
        _signal_stream: BehavioralSignalStream | None = None
        _intervention_loop: InterventionLoop | None = None
    else:
        _signal_stream = None
        _intervention_loop = None
except ImportError:
    _signal_stream = None
    _intervention_loop = None

# 子代理禁止使用的工具列表（借鉴 Hermes delegate_tool.py）
DELEGATE_BLOCKED_TOOLS = {
    "delegate_task",      # 禁止递归委托
    "send_message",       # 禁止跨平台消息
    "memory_write",       # 禁止共享记忆写入
    "agnes_video_generate",  # 视频生成耗时过长
}

_RESOURCE_PATH_TOOLS = {"list_files", "read_file", "write_file", "search_files", "delete_file", "edit_file", "create_file", "document_reader"}

SUB_AGENT_PROFILE_TOOLS = frozenset({
    "profile_get", "profile_set", "profile_history", "profile_forget",
})
SUB_AGENT_MEMORY_TOOL = "submit_memory"
SUB_AGENT_MESSAGE_TOOL = "send_message_to_agent"
SUB_AGENT_EXTRA_TOOLS = frozenset({SUB_AGENT_MEMORY_TOOL, SUB_AGENT_MESSAGE_TOOL})


def _safe_log_path(path: str) -> str:
    return path.replace("\r", " ").replace("\n", " ").replace("\x00", " ")[:200]


@dataclass
class SubAgentConfig:
    name: str
    display_name: str
    provider: str
    model: str
    personality_file: str | None = None
    voice_ref: str | None = None
    excluded_tools: set[str] = field(default_factory=set)
    base_url: str = ""
    api_key_env: str = ""
    display_name_en: str = ""
    capabilities: list[str] = field(default_factory=list)
    route_description: str = ""
    mcp_servers: list[str] = field(default_factory=list)
    max_spawn_depth: int = 1  # 子代理最大嵌套深度
    # 增强配置字段
    max_turns: int | None = None           # 最大对话轮数
    effort: str | None = None              # 思考努力程度: "low"/"medium"/"high"
    permission_mode: str | None = None     # 权限模式: "default"/"dev"/"strict"
    memory_scope: str | None = None        # 记忆作用域: "shared"/"isolated"
    background: bool = False               # 是否后台运行
    wallpaper: str = ""                    # 聊天背景板 URL（/assets/... 或上传后的 /media/...）
    sticker_dir: str = ""                  # 表情包目录路径（为空则自动推导）
    allowed_paths: list[str] = field(default_factory=list)    # 允许修改的路径白名单（glob 模式）
    forbidden_paths: list[str] = field(default_factory=list)  # 禁止修改的路径黑名单


def _read_env_key(env_var: str) -> str:
    """读取环境变量或 .env 文件中的配置值（委托给共享模块）。"""
    from utils.env_reader import read_env_key
    return read_env_key(env_var)


def _is_tool_unsupported_error(error_str: str) -> bool:
    """判断错误是否表示模型不支持工具调用（委托给共享模块）。"""
    from utils.env_reader import is_tool_unsupported_error
    return is_tool_unsupported_error(error_str)


class SubAgent:
    """单个子 Agent 实例，封装客户端、配置与调用逻辑。"""
    def __init__(self, config: SubAgentConfig, tts: TTSEngine,
                 tool_executor: ToolExecutor | None = None,
                 tool_repair: ToolCallRepair | None = None,
                 delegate_callback: Any | None=None,
                 core: Any | None=None) -> None:
        self.config = config
        self._tts = tts
        self._tool_executor = tool_executor
        self._tool_repair = tool_repair
        self._delegate_callback = delegate_callback
        self._core = core
        self._router = getattr(core, "router", None)
        self._personality: str = ""
        self._initialized = False
        self._degraded = False  # 探活失败时进入降级模式：仍注册但不可实际调用
        self._credential_pool: CredentialPool | None = None
        self._memory_submit_count = 0  # 子代理单次任务记忆提交计数（上限 3）
        self._communicating_with: str | None = None  # 子代理间直接通信防循环标记

    async def init(self) -> None:
        self._load_personality()

        self._initialized = self._router is not None
        if self._initialized:
            # 探活已禁用：max_tokens=1 在某些 API 上会被拒绝，
            # 而 4 个子 Agent 串行探活会消耗配额/触发限流。
            # 实际调用时如果 Key 无效会自然报错，无需提前探活。
            logger.info("sub_agent.initialized", name=self.config.name,
                        provider=self.config.provider, model=self.config.model)
        else:
            self._degraded = True
            logger.warning("sub_agent.degraded_no_router", name=self.config.name)

    def _load_personality(self) -> None:
        """加载人格文件并应用全局名称替换。"""
        self._personality = ""
        if self.config.personality_file:
            p = Path(self.config.personality_file)
            if p.exists():
                self._personality = p.read_text(encoding="utf-8-sig")

        if not self._personality:
            self._personality = f"你是{self.config.display_name}。"

        # 全局替换所有 agent 原名为 display_name（统一机制）
        from config import apply_agent_name_replacements
        self._personality = apply_agent_name_replacements(self._personality)

        # effort 思考努力程度提示
        if self.config.effort:
            effort_hints = {
                "low": "请简洁回答，不需要深入分析。",
                "medium": "请适度分析后回答。",
                "high": "请深入思考和分析后给出详细回答。",
            }
            hint = effort_hints.get(self.config.effort, "")
            if hint:
                self._personality = f"{self._personality}\n\n{hint}"

    def reload_personality(self) -> None:
        """重新加载人格文件（display_name 变更时调用）。"""
        self._load_personality()

    def set_credential_pool(self, pool: CredentialPool) -> None:
        """设置凭证池（由父代理传递）"""
        self._credential_pool = pool

    async def close(self) -> None:
        return None

    async def reload_model_config(self, provider: str, model: str,
                                  base_url: str, api_key_env: str) -> bool:
        """热重载模型配置：用新配置创建客户端并原子替换，不重新运行启动探活。

        用于一键切换子 Agent 模型时避免服务重启。
        """
        if self._router is None:
            logger.warning("sub_agent.reload_failed",
                           name=self.config.name,
                           reason="router_unavailable",
                           api_key_env=api_key_env)
            return False
        self.config.provider = provider
        self.config.model = model
        self.config.base_url = base_url
        self.config.api_key_env = api_key_env
        self._initialized = True
        self._degraded = False  # 清除降级标记：新 Key 已就位，允许调用
        logger.info("sub_agent.model_reloaded",
                    name=self.config.name, provider=provider, model=model)
        return True

    @property
    def available(self) -> bool:
        return self._initialized and self._router is not None and not self._degraded

    @property
    def degraded(self) -> bool:
        """降级模式：探活失败但仍注册，实际调用时回退到主体 agent。"""
        return self._degraded

    @staticmethod
    def _memory_submission_scope() -> Any | None:
        request = _current_request_ctx.get()
        principal = getattr(request, "principal", None)
        is_owner = (
            getattr(principal, "is_owner", False) is True
            or (principal is None and getattr(request, "is_master", False) is True)
        )
        if not is_owner:
            return None
        try:
            from memory.scope import current_scope

            return current_scope()
        except RuntimeError:
            return None

    def _excluded_tool_names(self) -> set[str]:
        """子代理过滤工具时排除的集合：配置排除项 + 画像工具（实例方法拦截）。"""
        return (
            self.config.excluded_tools
            | SUB_AGENT_PROFILE_TOOLS
            | {SUB_AGENT_MEMORY_TOOL}
        )

    def _filtered_tools(self) -> list[dict] | None:
        if not self._tool_executor:
            return None
        all_tools = to_openai_tools()
        excluded = self._excluded_tool_names()
        tools = [t for t in all_tools if t["function"]["name"] not in excluded]

        if self._memory_submission_scope() is not None:
            tools.append({
            "type": "function",
            "function": {
                "name": SUB_AGENT_MEMORY_TOOL,
                "description": "向主记忆提交重要观察（单次任务最多 3 次）",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "key_points": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "关键观察点列表",
                        },
                        "importance": {
                            "type": "integer",
                            "description": "重要程度(0-4)，默认 3，最大 4",
                            "default": 3,
                            "maximum": 4,
                        },
                    },
                    "required": ["key_points"],
                },
            },
            })

        # 子代理专属工具：send_message_to_agent（子代理间直接通信，实例方法拦截执行）
        tools.append({
            "type": "function",
            "function": {
                "name": SUB_AGENT_MESSAGE_TOOL,
                "description": f"直接向另一个子代理发消息获取响应（无需通过{get_agent_display_name('xiaoda')}中转）",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target_agent": {
                            "type": "string",
                            "description": "要联系的小伙伴名字",
                        },
                        "message": {
                            "type": "string",
                            "description": "要发送的消息内容",
                        },
                    },
                    "required": ["target_agent", "message"],
                },
            },
        })

        # Add MCP tools if available
        if hasattr(self._core, '_mcp_manager') and self._core._mcp_manager:
            mcp_server_names = self.config.mcp_servers
            if mcp_server_names:
                mcp_tools = self._core._mcp_manager.get_tools_for_agent(mcp_server_names)
                tools.extend(mcp_tools)

        return tools if tools else None

    def _filtered_tool_names(self) -> set[str]:
        if not self._tool_executor:
            return set()
        excluded = self._excluded_tool_names()
        names = {t["function"]["name"] for t in to_openai_tools() if t["function"]["name"] not in excluded}
        names.update({SUB_AGENT_MESSAGE_TOOL})
        if self._memory_submission_scope() is not None:
            names.add(SUB_AGENT_MEMORY_TOOL)
        return names

    async def chat(self, message: str, context: str = "", status_callback: Any | None=None, address_term: str = "爸爸", extra_system_prompt: str = "", invocation: Any | None = None, interjections: list | None = None) -> str:
        if self._degraded:
            self._router = getattr(self._core, "router", None)
            if self._router is not None:
                self._degraded = False
                self._initialized = True
                logger.info("sub_agent.auto_recovered", name=self.config.name)

        if not self.available:
            return f"{self.config.display_name}{TIRED_MSG}"

        # 单次任务开始时重置记忆提交计数
        self._memory_submit_count = 0

        if status_callback:
            try:
                await status_callback(get_status_msg(self.config.name, "thinking", "", self.config.personality_file))
            except (AttributeError, RuntimeError, OSError):
                logger.debug("sub_agent.status_callback_failed", exc_info=True)  # status_callback 失败不影响任务执行

        system_prompt = self._personality
        if "{address_term}" in system_prompt:
            system_prompt = system_prompt.replace("{address_term}", address_term)
        if extra_system_prompt:
            system_prompt += f"\n\n{extra_system_prompt}"
        if context:
            system_prompt += f"\n\n[背景信息]\n{context}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ]

        tools = self._filtered_tools()
        if invocation is not None:
            allowed_tools = set(invocation.allowed_tools)
            tools = [tool for tool in tools or [] if tool["function"]["name"] in allowed_tools] or None

        # J-Space Hook: 干预前评估
        if _intervention_loop is not None:
            try:
                interventions = await _intervention_loop.evaluate({})
                for intervention in interventions:
                    # 应用干预到上下文（此处上下文为字符串，仅消费 prompt 维度方向）
                    direction_ctx = await _intervention_loop.apply_intervention({}, intervention)
                    prompt_modifier = direction_ctx.get("prompt_modifier", 0.0)
                    if prompt_modifier > 0:
                        system_prompt += (
                            f"\n\n[干预方向] 行为权重 {prompt_modifier:.2f}，"
                            "请在回复中适度体现此方向倾向。"
                        )
            except Exception:
                logger.debug("JSpace.intervention_evaluate_failed", exc_info=True)

        response: str | None = None
        success = False
        try:
            response = await self._chat_loop(messages, tools, invocation=invocation,
                                             interjections=interjections)
            success = True
        except (TimeoutError, OSError, RuntimeError, ValueError) as e:
            logger.warning("sub_agent.chat_failed name={} error={}", self.config.name, str(e))
            if tools and _is_tool_unsupported_error(str(e)):
                try:
                    response = await self._chat_loop(messages, None, invocation=invocation,
                                                     interjections=interjections)
                    success = True
                except (TimeoutError, OSError, RuntimeError, ValueError) as e2:
                    logger.warning("sub_agent.fallback_failed name={} error={}", self.config.name, str(e2))

        # J-Space Hook: emit agent success signal
        if _signal_stream is not None:
            try:
                success_score = 1.0 if success else 0.0
                await _signal_stream.emit(
                    f"agent_{self.config.name}_success", success_score, "agent_dispatcher")
            except Exception:
                logger.debug("agent_dispatcher.signal_emit_failed", exc_info=True)

        if response is not None:
            return response
        return f"{self.config.display_name}{TIRED_MSG}"

    async def _handle_tool_result(self, tool_name: str, result: ToolResult) -> str:
        result_text = ""
        from core.delegation import DelegationRequest
        delegation_req = None
        if result.success and isinstance(result.data, DelegationRequest):
            delegation_req = result.data
        elif result.success and isinstance(result.data, AgentMessage) and result.data.is_delegate_request():
            # 优先用 AgentMessage 结构化协议识别
            delegation_req = DelegationRequest(
                type="xiaoda", question=result.data.content, delegator=self.config.name
            )
        elif result.success and isinstance(result.data, str) and result.data.startswith("[NAHIDA_PENDING]"):
            # fallback: 旧字符串匹配（过渡期保留）
            import logging
            logging.getLogger(__name__).warning(
                "使用废弃的 [NAHIDA_PENDING] 字符串匹配识别委托，请迁移到 AgentMessage 协议"
            )
            delegation_req = DelegationRequest(
                type="xiaoda", question=result.data[len("[NAHIDA_PENDING]"):], delegator=self.config.name
            )

        if delegation_req and delegation_req.type == "xiaoda":
            question = delegation_req.question
            if self._delegate_callback:
                # 委托深度检查：超过 2 层直接返回兜底回复，防止无限循环
                from agent_core import _current_request_ctx
                _ctx = _current_request_ctx.get()
                if _ctx and _ctx.delegate_depth >= 2:
                    logger.warning("delegate.depth_exceeded", depth=_ctx.delegate_depth, from_agent=self.config.name)
                    result_text = f"{get_agent_display_name('xiaoda')}姐姐现在也在忙，先自己想想办法吧！"
                else:
                    delegate_reply = await self._delegate_callback(question)
                    result_text = f"[主Agent的回答（{self.config.display_name}需要用自己的话转述，不要直接复制原话）]\n{delegate_reply}"
            else:
                result_text = "主Agent现在不在...先自己想想办法吧！"
        elif result.success:
            result_text = json.dumps(result.data, ensure_ascii=False) if not isinstance(result.data, str) else result.data
        else:
            result_text = f"错误: {result.error}"
        if len(result_text) > 4000:
            result_text = result_text[:4000] + f"\n...(结果过长已截断，共{len(result_text)}字符)"
        return result_text

    def _is_reasoning_model(self) -> bool:
        model = self.config.model.lower()
        return any(kw in model for kw in [
            "v4-flash", "v4-pro", "v3", "reasoner", "r1",
            "nex-n2", "nex-agi", "thinking", "o1", "o3", "o4",
            "agnes",  # agnes 系列模型默认开启推理模式
        ])

    def _build_dsml_tool_prompt(self, allowed_tools: set[str] | None = None) -> str:
        tools = self._filtered_tools()
        if allowed_tools is not None:
            tools = [tool for tool in tools or [] if tool["function"]["name"] in allowed_tools]
        if not tools:
            return ""
        lines = ["你可以使用以下工具，调用时必须使用DSML格式："]
        for t in tools:
            f = t["function"]
            params = f.get("parameters", {}).get("properties", {})
            required = f.get("parameters", {}).get("required", [])
            param_desc = ", ".join(
                f'{k}({", ".join(str(x) for x in v.get("enum", []))})' if "enum" in v else k
                for k, v in params.items()
            )
            req_mark = "必填" if required else ""
            lines.append(f'- {f["name"]}({param_desc}) {req_mark}: {f.get("description", "")}')
        lines.append("""
调用格式示例:
<｜｜DSML｜｜tool_calls>
<｜｜DSML｜｜invoke name="web_search">
<｜｜DSML｜｜parameter name="query">搜索关键词</｜｜DSML｜｜parameter>
</｜｜DSML｜｜invoke>
</｜｜DSML｜｜tool_calls>

重要：需要调用工具时必须使用上述DSML格式，不要用其他格式。不需要调用工具时直接回复即可。""")
        return "\n".join(lines)

    @staticmethod
    def _drain_interjections(interjections: list | None) -> list[dict]:
        return _drain_interjections(interjections)

    async def _chat_loop(self, messages: list[dict], tools: list[dict] | None, invocation: Any | None = None, interjections: list | None = None) -> list | str:
        """主循环：调用 LLM → 提取工具调用 → 执行 → 反馈，最多 max_rounds 轮。"""
        max_rounds = self.config.max_turns if self.config.max_turns is not None else 5
        working = list(messages)
        tool_names = self._filtered_tool_names()
        if invocation is not None:
            tool_names &= set(invocation.allowed_tools)
        # 超时配置从 config 读取 (支持环境变量覆盖)
        import config as _cfg
        api_timeout = getattr(_cfg, 'SUB_AGENT_API_TIMEOUT', 60)
        total_timeout = getattr(_cfg, 'SUB_AGENT_TOTAL_TIMEOUT', 150)
        total_deadline = asyncio.get_running_loop().time() + total_timeout
        is_reasoning = self._is_reasoning_model()

        # 选择 extractor：推理模型用 DSML，否则用标准
        standard_ext = StandardExtractor()
        dsml_ext = DsmlExtractor(allowed_tools=tool_names)

        tools = self._inject_dsml_if_needed(working, tools, is_reasoning, tool_names)

        for round_idx in range(max_rounds):
            # 后台委托：用户/主代理在执行期插入的指示，下一轮 LLM 前生效
            for note_msg in _drain_interjections(interjections):
                working.append(note_msg)
                logger.info("sub_agent.interjected name={}", self.config.name)

            if asyncio.get_running_loop().time() > total_deadline:
                logger.warning("sub_agent.total_timeout", name=self.config.name)
                return f"{self.config.display_name}处理超时了，请稍后再试吧～"

            remaining = total_deadline - asyncio.get_running_loop().time()
            if remaining < 10:
                logger.warning("sub_agent.time_exhausted", name=self.config.name)
                return f"{self.config.display_name}处理超时了，请稍后再试吧～"

            response = await self._call_llm_one_round(working, tools, remaining, round_idx)
            if isinstance(response, str):
                return response  # 超时提示

            msg = response.choices[0].message
            # 统一提取工具调用：先尝试标准，再尝试 DSML
            extracted = standard_ext.extract(msg)
            is_dsml = False
            if extracted is None and self._tool_executor:
                extracted = dsml_ext.extract(msg)
                is_dsml = extracted is not None

            # 无工具调用 → 直接返回清理后的内容
            if extracted is None:
                content = msg.content or ""
                # 修复：不使用 reasoning_content 代替 content（防止推理泄漏）
                # 根因：reasoning_content 是模型内部思考链，regex 无法可靠清理整段中文/英文思考链，
                # 用它代替 content 会导致推理过程被当成最终回复发给用户。
                # content 为空时让上层 fallback（返回提示语），更安全。
                content = strip_dsml(content)
                content = strip_reasoning(content)
                content = deduplicate_multi_reply(content)
                content = humanize(content, style="xiaoda")
                logger.info("sub_agent.chat.ok", name=self.config.name, model=self.config.model, rounds=round_idx)
                result = content.strip()
                # 兜底：如果过滤后为空（如模型只输出推理泄露），返回提示
                if not result:
                    return f"{self.config.display_name}思考了一下，但还没有整理好回答，请稍等或换个问题问我吧～"
                return result

            # 构造 assistant 消息并加入 working
            working.append(self._build_assistant_msg(msg, extracted, is_dsml))

            # 统一执行工具调用
            await self._execute_round_tool_calls(extracted, working, invocation=invocation)

        # 达到最大轮次：让 LLM 基于已有工具结果做总结回复
        remaining = total_deadline - asyncio.get_running_loop().time()
        if remaining < 5:
            return f"{self.config.display_name}{TIRED_MSG}"
        return await self._summarize_after_tools(working, api_timeout, remaining)

    def _inject_dsml_if_needed(self, working: list[dict], tools: list[dict] | None,
                                is_reasoning: bool,
                                tool_names: list[str]) -> list[dict] | None:
        """推理模型注入 DSML 工具提示并禁用原生 tools; 非推理模型保持原样返回 tools"""
        if is_reasoning and tools:
            dsml_prompt = self._build_dsml_tool_prompt(allowed_tools=set(tool_names))
            if dsml_prompt and working and working[0]["role"] == "system":
                working[0] = {
                    "role": "system",
                    "content": working[0]["content"] + "\n\n" + dsml_prompt,
                }
            tools = None
        return tools

    async def _call_llm_one_round(self, working: list[dict], tools: list[dict] | None,
                                  remaining: float, round_idx: int) -> Any:
        """单轮调用 LLM API; 超时返回用户可见的提示字符串, 成功返回响应对象

        超时重试: 网络抖动导致首次超时时, 用半超时值重试一次 (工业标准做法).
        重试也超时才返回错误提示.
        """
        import config as _cfg
        api_timeout = getattr(_cfg, 'SUB_AGENT_API_TIMEOUT', 60)
        retry_count = getattr(_cfg, 'SUB_AGENT_API_RETRY', 1)
        loop = asyncio.get_running_loop()

        for attempt in range(max(retry_count, 0) + 1):
            # 每次重试使用半超时值 (重试时网络通常已恢复, 用更短超时快速失败)
            cur_timeout = api_timeout if attempt == 0 else api_timeout / 2
            cur_timeout = min(cur_timeout, remaining)
            if remaining < 5:
                # 总循环剩余时间不足以做有意义的调用
                return f"{self.config.display_name}思考时间太长了，请稍后再试吧～"
            try:
                t0 = loop.time()
                route_config = {
                    "client": self.config.provider,
                    "model": self.config.model,
                    "max_tokens": 6144 if tools else 3072,
                }
                if self.config.provider == "agnes":
                    from model_router import ROUTE_TABLE
                    chat_config = ROUTE_TABLE.get("chat", {})
                    route_config["thinking"] = chat_config.get("thinking")
                from config import get_temperature
                response = await self._router.route_config(
                        config=route_config,
                        messages=working,
                        temperature=get_temperature(default=0.9),
                        max_tokens=route_config["max_tokens"],
                        tools=tools,
                        tool_choice="auto" if tools else None,
                    timeout=cur_timeout,
                    )
                if isinstance(response, str):
                    response = SimpleNamespace(
                        choices=[SimpleNamespace(
                            message=SimpleNamespace(content=response, tool_calls=None),
                        )],
                    )
                elapsed = loop.time() - t0
                logger.info("sub_agent.api_ok", name=self.config.name,
                            round=round_idx, attempt=attempt, elapsed=f"{elapsed:.1f}s",
                            thinking=(route_config.get("thinking") or {}).get("type") == "enabled")
                return response
            except TimeoutError:
                if attempt < retry_count:
                    logger.warning("sub_agent.api_timeout_retry",
                                   name=self.config.name, round=round_idx,
                                   attempt=attempt, next_timeout=f"{cur_timeout/2:.1f}s")
                    # 更新 remaining (扣除已等待时间)
                    remaining -= cur_timeout
                    continue
                logger.warning("sub_agent.api_timeout", name=self.config.name,
                               round=round_idx, attempts=attempt + 1)
                return f"{self.config.display_name}思考时间太长了，请稍后再试吧～"
        # 防御性兜底: retry_count 为负数时 for 循环不执行, 确保始终有返回值
        return f"{self.config.display_name}思考时间太长了，请稍后再试吧～"

    async def _execute_round_tool_calls(self, extracted: Any, working: list[dict], invocation: Any | None = None) -> None:
        """并行执行本轮工具调用, 将结果 (含错误) 追加到 working"""
        try:
            tool_results = await asyncio.wait_for(
                asyncio.gather(
                    *[self._exec_one_tool_call(tc, invocation=invocation) for tc in extracted],
                    return_exceptions=True,
                ),
                timeout=120,
            )
        except TimeoutError:
            logger.warning("sub_agent.tool_gather_timeout name={} count={}",
                           self.config.name, len(extracted))
            for tc in extracted:
                working.append({"role": "tool", "tool_call_id": tc.id,
                                "content": "错误: 工具执行超时"})
            return

        for tc, r in zip(extracted, tool_results, strict=False):
            if isinstance(r, Exception):
                logger.warning("sub_agent.tool_error", name=self.config.name, tool=tc.name, error=str(r))
                working.append({"role": "tool", "tool_call_id": tc.id, "content": f"错误: {r}"})
            else:
                working.append({"role": "tool", "tool_call_id": r["tool_call_id"], "content": r["content"]})

    def _build_assistant_msg(self, msg: Any, extracted: Any, is_dsml: bool) -> dict:
        """根据 LLM 响应与提取结果构造 assistant 消息（含 tool_calls 字段）。"""
        msg_rc = getattr(msg, "reasoning_content", None) or ""
        clean_content = strip_dsml(msg.content or "") if is_dsml else msg.content or ""
        assistant_msg = {
            "role": "assistant",
            "content": clean_content,
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.name, "arguments": tc.arguments_json}}
                for tc in extracted
            ],
        }
        if msg_rc:
            assistant_msg["reasoning_content"] = msg_rc
        return assistant_msg

    async def _exec_one_tool_call(self, tc: Any, invocation: Any | None = None) -> dict:
        """执行单个工具调用：风暴检测 → 截断修复 → 授权/路径/权限检查 → 执行 → 后处理。"""
        tool_name = tc.name
        args_str = tc.arguments_json

        if self._tool_repair:
            # 风暴检测：拦截重复调用同一工具+相同参数的循环
            if self._tool_repair.detect_storm(tool_name, args_str):
                logger.warning("sub_agent.storm_detected", tool=tool_name)
                return {"tool_call_id": tc.id, "content": "错误: 该工具调用已被风暴检测拦截，请换个思路尝试"}

            repaired = self._tool_repair.repair_truncation(args_str)
            if repaired:
                args_str = repaired

        args = tc.parse_arguments()

        # 结构化隔离调用的工具授权检查（allowed_tools + 禁止嵌套代理通信）
        denied = self._check_invocation_authorization(tc, tool_name, invocation)
        if denied:
            return denied

        # 路径策略检查（资源路径工具的路径安全校验）
        denied = self._check_path_policy(tc, tool_name, args, invocation)
        if denied:
            return denied

        # strict 模式的工具权限检查（仅 READ_ONLY 工具可执行）
        denied = self._check_strict_mode(tc, tool_name, invocation)
        if denied:
            return denied

        if tool_name not in self._filtered_tool_names():
            return {
                "tool_call_id": tc.id,
                "content": json.dumps(
                    {"error": f"工具 {tool_name} 未授权或在子代理中被禁止使用"},
                    ensure_ascii=False,
                ),
            }

        # 过滤被禁止的工具
        if tool_name in DELEGATE_BLOCKED_TOOLS:
            tool_result_content = json.dumps({
                "error": f"工具 {tool_name} 在子代理中被禁止使用"
            }, ensure_ascii=False)
            return {"tool_call_id": tc.id, "content": tool_result_content}

        # 子代理专属工具（submit_memory / send_message_to_agent）实例方法拦截
        special = await self._exec_sub_agent_special_tool(tc, tool_name, args)
        if special is not None:
            return special

        # 工具护栏检查
        guardrails = get_tool_guardrails()
        action, guard_msg = await guardrails.check(tool_name, args)
        if action == "halt":
            return {"tool_call_id": tc.id, "content": f"错误: {guard_msg}"}

        result = await self._tool_executor.execute(tool_name, args)

        # 记录工具调用到护栏
        await guardrails.record_call(tool_name, args, result.success,
                               str(result.data)[:100] if result.data else "")

        result_text = await self._handle_tool_result(tool_name, result)

        # 护栏警告注入
        if action == "warn" and guard_msg and result.success:
            result_text = f"[护栏警告: {guard_msg}]\n{result_text}"

        return {"tool_call_id": tc.id, "content": result_text}

    @staticmethod
    def _check_invocation_authorization(tc: Any, tool_name: str, invocation: Any | None) -> dict | None:
        """结构化隔离调用的工具授权检查。拒绝返回错误 dict，通过返回 None。"""
        if invocation is None:
            return None
        if tool_name not in invocation.allowed_tools:
            return {"tool_call_id": tc.id, "content": json.dumps({"error": f"工具 {tool_name} 未授权"}, ensure_ascii=False)}
        if tool_name == SUB_AGENT_MESSAGE_TOOL:
            return {"tool_call_id": tc.id, "content": json.dumps({"error": "结构化隔离调用禁止嵌套代理通信"}, ensure_ascii=False)}
        return None

    def _check_path_policy(self, tc: Any, tool_name: str, args: dict, invocation: Any | None) -> dict | None:
        """路径策略检查（资源路径工具）。拒绝返回错误 dict，通过返回 None。"""
        if invocation is None or tool_name not in _RESOURCE_PATH_TOOLS:
            return None
        import fnmatch

        path = _extract_path_from_args(tool_name, args)
        if tool_name == "search_files":
            path = args.get("pattern", "")
        path = path.replace("\\", "/") if isinstance(path, str) else ""
        if not path:
            logger.warning(
                "sub_agent.path_policy_denied",
                target=invocation.target,
                request_id=invocation.request_id or "",
                tool=tool_name,
                reason="missing_path",
                path="",
                allowed_pattern_count=len(invocation.allowed_paths),
                forbidden_pattern_count=len(invocation.forbidden_paths),
            )
            return {"tool_call_id": tc.id, "content": json.dumps({"error": f"工具 {tool_name} 缺少路径，路径策略拒绝执行"}, ensure_ascii=False)}
        path_parts = path.split("/")
        is_windows_absolute = len(path) >= 3 and path[0].isalpha() and path[1:3] == ":/"
        if path.startswith(("/", "~")) or is_windows_absolute or any(part == ".." for part in path_parts) or "\x00" in path:
            logger.warning(
                "sub_agent.path_policy_denied",
                target=invocation.target,
                request_id=invocation.request_id or "",
                tool=tool_name,
                reason="unsafe_path",
                path=_safe_log_path(path),
                allowed_pattern_count=len(invocation.allowed_paths),
                forbidden_pattern_count=len(invocation.forbidden_paths),
            )
            return {"tool_call_id": tc.id, "content": json.dumps({"error": f"路径策略拒绝不安全路径 {path}"}, ensure_ascii=False)}
        if invocation.allowed_paths or invocation.forbidden_paths:
            forbidden = any(fnmatch.fnmatch(path, pattern) for pattern in invocation.forbidden_paths)
            allowed = not invocation.allowed_paths or any(fnmatch.fnmatch(path, pattern) for pattern in invocation.allowed_paths)
            if forbidden or not allowed:
                logger.warning(
                    "sub_agent.path_policy_denied",
                    target=invocation.target,
                    request_id=invocation.request_id or "",
                    tool=tool_name,
                    reason="forbidden_pattern" if forbidden else "not_allowed",
                    path=_safe_log_path(path),
                    allowed_pattern_count=len(invocation.allowed_paths),
                    forbidden_pattern_count=len(invocation.forbidden_paths),
                )
                return {"tool_call_id": tc.id, "content": json.dumps({"error": f"路径策略拒绝访问 {path}"}, ensure_ascii=False)}
        return None

    @staticmethod
    def _check_strict_mode(tc: Any, tool_name: str, invocation: Any | None) -> dict | None:
        """strict 模式的工具权限检查（仅 READ_ONLY 可执行）。拒绝返回错误 dict，通过返回 None。"""
        if invocation is None or invocation.permission_mode != "strict":
            return None
        from tool_engine.tool_registry import ToolPermission, get_tool

        registered_tool = get_tool(tool_name)
        permission = registered_tool.get("permission", ToolPermission.READ_ONLY) if registered_tool else None
        if permission != ToolPermission.READ_ONLY:
            return {"tool_call_id": tc.id, "content": json.dumps({"error": f"strict 模式拒绝执行工具 {tool_name}"}, ensure_ascii=False)}
        return None

    async def _exec_sub_agent_special_tool(self, tc: Any, tool_name: str, args: dict) -> dict | None:
        """子代理专属工具（submit_memory / send_message_to_agent）实例方法拦截。非专属返回 None。"""
        if tool_name == SUB_AGENT_MEMORY_TOOL:
            try:
                result_text = await self.submit_memory(**args)
            except (OSError, ValueError, RuntimeError) as e:
                logger.warning("sub_agent.submit_memory_call_failed", error=str(e)[:200])
                result_text = f"错误: {e}"
            return {"tool_call_id": tc.id, "content": result_text}

        if tool_name == SUB_AGENT_MESSAGE_TOOL:
            try:
                result_text = await self.send_message_to_agent(**args)
            except (OSError, ValueError, RuntimeError) as e:
                logger.warning("sub_agent.send_message_to_agent_call_failed", error=str(e)[:200])
                result_text = f"错误: {e}"
            return {"tool_call_id": tc.id, "content": result_text}
        return None

    async def _summarize_after_tools(self, working: list[dict], api_timeout: int,
                                       remaining: float) -> str:
        """达到最大轮次后：若有未消化的 tool 结果，让 LLM 做一次总结回复。

        超时或异常时降级返回 tool 内容的前若干行，避免完全无响应。
        """
        last_tool = working[-1] if working else {}
        if isinstance(last_tool, dict) and last_tool.get("role") == "tool":
            working.append({
                "role": "system",
                "content": f"你已经调用了工具并拿到了结果。现在请基于工具返回的数据，用{self.config.display_name}的风格做总结回复。\n\n"
                f"回复结构要求：\n"
                f"1. 首先明确说明执行了什么操作（如「搜索了XX」「查看了XX」），不要用「数据加载完毕」这种模糊表述\n"
                f"2. 然后用自然语言描述关键结果，数据要清楚明确\n"
                f"3. 最后可以加一句个性化评论\n\n"
                f"不要只复制原始数据，要用自然语言解释关键信息。如果数据有异常要指出。",
            })

        try:
            route_config = {
                "client": self.config.provider,
                "model": self.config.model,
                "max_tokens": 3072,
            }
            if self.config.provider == "agnes":
                from model_router import ROUTE_TABLE
                chat_config = ROUTE_TABLE.get("chat", {})
                route_config["thinking"] = chat_config.get("thinking")
            from config import get_temperature
            response = await self._router.route_config(
                    config=route_config,
                    messages=working,
                    temperature=get_temperature(default=0.7),
                    max_tokens=3072,
                timeout=min(api_timeout, remaining),
                )
            reply = response if isinstance(response, str) else response.choices[0].message.content or ""
            # 不使用 reasoning_content 代替 content（防止推理泄漏）
            result = strip_reasoning(strip_dsml(reply)).strip()
            # 兜底：如果过滤后为空（如模型只输出推理泄露），返回提示
            if not result:
                return f"{self.config.display_name}思考了一下，但还没有整理好回答，请稍等或换个问题问我吧～"
            return result
        except (TimeoutError, asyncio.TimeoutError):
            last_tool = working[-1] if working else {}
            if isinstance(last_tool, dict) and last_tool.get("role") == "tool":
                raw_content = last_tool.get("content", "").strip()
                lines = [line.strip() for line in raw_content.splitlines() if line.strip()]
                if len(lines) > 1:
                    formatted = "\n".join(lines[:15])
                    if len(lines) > 15:
                        formatted += f"\n...（共{len(lines)}行）"
                    return formatted
                return raw_content
            return f"{self.config.display_name}{TIRED_MSG}"
    async def submit_memory(self, key_points: list[str], importance: int = 3) -> str:
        """子代理向主记忆提交关键信息（受控写入）"""
        scope = self._memory_submission_scope()
        request = _current_request_ctx.get()
        principal = getattr(request, "principal", None)
        is_owner = (
            getattr(principal, "is_owner", False) is True
            or (principal is None and getattr(request, "is_master", False) is True)
        )
        if not is_owner:
            return "（拒绝：访客不能提交个人记忆）"
        if scope is None:
            return "（拒绝：记忆作用域未绑定）"

        # 频率限制：单次任务最多 3 次
        if self._memory_submit_count >= 3:
            return "已达本次任务记忆提交上限（3次）"

        # importance 上限校验：防止子代理提权写入高敏感记忆
        importance = min(importance, 4)

        # 拼接内容
        memory_text = f"[{self.config.display_name}观察] " + "; ".join(key_points)

        # 检查记忆系统可用性（实际属性名为 memory，非 memory_manager）
        if not self._core or not hasattr(self._core, "memory") or self._core.memory is None:
            return "（记忆系统不可用）"

        try:
            mm = self._core.memory
            # 适配实际接口：MemoryManager.memory.insert_episodic_memory(summary, importance:float, emotion_label)
            # importance 整数(0-4) 归一化到 float(0-1)
            importance_float = importance / 4.0
            mem_id = await mm.memory.insert_episodic_memory(
                summary=memory_text,
                importance=importance_float,
                emotion_label="",
                source="sub_agent",
                scope=scope,
            )
            # 同步写入向量索引（与 remember 工具保持一致）
            if getattr(mm, "vec", None) and memory_text:
                try:
                    await mm.vec.upsert(mem_id, memory_text)
                except (OSError, RuntimeError, ValueError) as ve:
                    logger.warning("sub_agent.submit_memory.vec_failed", error=str(ve)[:200])

            invalidate = getattr(mm, "invalidate_read_caches", None)
            if callable(invalidate):
                invalidate()

            self._memory_submit_count += 1
            logger.info("sub_agent.submit_memory", name=self.config.name, count=self._memory_submit_count)
            return f"已记录：{memory_text[:50]}..."
        except (OSError, ValueError, RuntimeError) as e:
            logger.warning("sub_agent.submit_memory_failed", error=str(e)[:200])
            return "（记忆系统不可用）"

    async def send_message_to_agent(self, target_agent: str, message: str) -> str:
        """子代理直接给另一个子代理发消息，无需主代理中转"""
        # 防循环：消息内容包含本工具名，或目标已在通信栈中
        if "send_message_to_agent" in message or self._communicating_with == target_agent:
            return "（避免循环通信）"

        # 检查通信渠道
        if not self._core or not hasattr(self._core, "dispatcher") or self._core.dispatcher is None:
            return "（找不到通信渠道）"

        dispatcher = self._core.dispatcher

        # 获取目标 Agent：优先按内部 name 查找，再按 display_name 匹配
        target = None
        try:
            target = dispatcher.get_agent(target_agent)
        except (KeyError, AttributeError):
            target = None

        if target is None:
            agents_dict = getattr(dispatcher, "_agents", {}) or {}
            for agent in agents_dict.values():
                if getattr(agent.config, "display_name", "") == target_agent:
                    target = agent
                    break

        if target is None:
            return f"（找不到 {target_agent}）"

        # 调用目标 Agent 的 chat 方法
        context = f"这是{self.config.display_name}发来的消息：\n{message}"
        self._communicating_with = target_agent
        try:
            reply = await target.chat(message, context=context)
            return reply if reply else f"（{target_agent} 没有回应）"
        except (TimeoutError, OSError, RuntimeError) as e:
            logger.warning(
                "sub_agent.send_message_failed",
                sender=self.config.name,
                target=target_agent,
                error=str(e)[:200],
            )
            return f"（{target_agent} 暂时无法响应：{e}）"
        finally:
            self._communicating_with = None

    async def synthesize(self, text: str, style: str = "", emotion: str = "") -> Path | None:
        if not self.config.voice_ref:
            return None
        return await self._tts.synthesize(text, voice=self.config.voice_ref, style=style, emotion=emotion)


def _drain_interjections(queue: list | None) -> list[dict]:
    """出队全部待消费插话并转为 user 消息（后台委托执行期注入用）。"""
    if not queue:
        return []
    out = []
    while queue:
        note = str(queue.pop(0))[:600]
        out.append({"role": "user",
                    "content": f"【用户插话】{note}\n请把这条补充纳入接下来的处理。"})
    return out
