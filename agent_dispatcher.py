import json
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from openai import AsyncOpenAI

from loguru import logger
from tool_engine.tool_registry import to_openai_tools
from tool_engine.tool_executor import ToolExecutor, ToolResult
from tool_engine.tool_repair import ToolCallRepair
from utils.text_utils import has_dsml_tool_calls, parse_dsml_tool_calls, strip_dsml, strip_reasoning
from emotion.tts_engine import TTSEngine
from emotion.emoji_config import get_status_msg
from tool_engine.tool_guardrails import get_tool_guardrails
from utils.credential_pool import CredentialPool
from core.message import AgentMessage
from config import get_agent_display_name


# ── ToolCallExtractor 统一接口 ──────────────────────────────

@dataclass
class ExtractedToolCall:
    """统一的工具调用结构，无论来源是标准 tool_calls 还是 DSML 文本。"""
    id: str
    name: str
    arguments_json: str  # JSON string

    def parse_arguments(self) -> dict:
        try:
            return json.loads(self.arguments_json)
        except json.JSONDecodeError:
            return {}


@runtime_checkable
class ToolCallExtractor(Protocol):
    """从 LLM 响应中提取工具调用的策略接口。"""

    def extract(self, message: Any) -> list[ExtractedToolCall] | None:
        """从 message 中提取工具调用。返回 None 表示无工具调用。"""
        ...


class StandardExtractor:
    """从标准 message.tool_calls 中提取工具调用。"""

    def extract(self, message: Any) -> list[ExtractedToolCall] | None:
        if not message.tool_calls:
            return None
        return [
            ExtractedToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments_json=tc.function.arguments,
            )
            for tc in message.tool_calls
        ]


class DsmlExtractor:
    """从 DSML 文本标记中提取工具调用（用于推理模型）。"""

    def __init__(self, allowed_tools: set[str] | None = None) -> None:
        self._allowed_tools = allowed_tools

    def extract(self, message: Any) -> list[ExtractedToolCall] | None:
        content = message.content or ""
        if not content:
            return None
        if not has_dsml_tool_calls(content):
            return None
        dsml_calls = parse_dsml_tool_calls(content, self._allowed_tools)
        if not dsml_calls:
            return None
        return [
            ExtractedToolCall(
                id=tc["id"],
                name=tc["function"]["name"],
                arguments_json=tc["function"]["arguments"],
            )
            for tc in dsml_calls
        ]


# 子代理禁止使用的工具列表（借鉴 Hermes delegate_tool.py）
DELEGATE_BLOCKED_TOOLS = {
    "delegate_task",      # 禁止递归委托
    "send_message",       # 禁止跨平台消息
    "memory_write",       # 禁止共享记忆写入
    "agnes_video_generate",  # 视频生成耗时过长
}


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
        self._client: AsyncOpenAI | None = None
        self._personality: str = ""
        self._initialized = False
        self._degraded = False  # 探活失败时进入降级模式：仍注册但不可实际调用
        self._credential_pool: CredentialPool | None = None
        self._memory_submit_count = 0  # 子代理单次任务记忆提交计数（上限 3）
        self._communicating_with: str | None = None  # 子代理间直接通信防循环标记

    async def init(self) -> None:
        api_key = _read_env_key(self.config.api_key_env)
        if api_key and self.config.base_url:
            self._client = AsyncOpenAI(api_key=api_key, base_url=self.config.base_url)

        self._load_personality()

        self._initialized = self._client is not None
        if self._initialized:
            # 探活已禁用：max_tokens=1 在某些 API 上会被拒绝，
            # 而 4 个子 Agent 串行探活会消耗配额/触发限流。
            # 实际调用时如果 Key 无效会自然报错，无需提前探活。
            logger.info("sub_agent.initialized", name=self.config.name,
                        provider=self.config.provider, model=self.config.model)
        else:
            # 客户端创建失败（API Key 未找到或 base_url 缺失），
            # 标记为降级模式：仍注册但实际调用时回退到主 Agent
            self._degraded = True
            logger.warning("sub_agent.degraded_no_client", name=self.config.name,
                           reason="api_key_missing" if not _read_env_key(self.config.api_key_env) else "no_base_url")

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
        """关闭 AsyncOpenAI 客户端, 释放 TCP 连接."""
        if self._client is not None:
            try:
                await self._client.close()
            except (OSError, RuntimeError):
                logger.debug("agent_dispatcher.close_client_error", exc_info=True)
            self._client = None

    async def reload_model_config(self, provider: str, model: str,
                                  base_url: str, api_key_env: str) -> bool:
        """热重载模型配置：用新配置创建客户端并原子替换，不重新运行启动探活。

        用于一键切换子 Agent 模型时避免服务重启。
        """
        api_key = _read_env_key(api_key_env)
        if not api_key or not base_url:
            logger.warning("sub_agent.reload_failed",
                           name=self.config.name,
                           reason="missing_api_key_or_base_url",
                           api_key_env=api_key_env)
            return False
        try:
            new_client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        except (ValueError, OSError, RuntimeError) as e:
            logger.warning("sub_agent.reload_client_failed",
                           name=self.config.name, error=str(e)[:200])
            return False
        # 原子替换：先就位再切，避免半成品状态
        self._client = new_client
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
        return self._initialized and self._client is not None and not self._degraded

    @property
    def degraded(self) -> bool:
        """降级模式：探活失败但仍注册，实际调用时回退到主体 agent。"""
        return self._degraded

    def _filtered_tools(self) -> list[dict] | None:
        if not self._tool_executor:
            return None
        all_tools = to_openai_tools()
        excluded = self.config.excluded_tools
        tools = [t for t in all_tools if t["function"]["name"] not in excluded]

        # 子代理专属工具：submit_memory（受控记忆提交，实例方法拦截执行）
        tools.append({
            "type": "function",
            "function": {
                "name": "submit_memory",
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
                "name": "send_message_to_agent",
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
        excluded = self.config.excluded_tools
        names = {t["function"]["name"] for t in to_openai_tools() if t["function"]["name"] not in excluded}
        names.add("submit_memory")  # 子代理专属工具
        names.add("send_message_to_agent")  # 子代理专属工具：子代理间直接通信
        return names

    async def chat(self, message: str, context: str = "", status_callback: Any | None=None, address_term: str = "爸爸", extra_system_prompt: str = "") -> str:
        # 降级模式下尝试自动恢复：用最新环境变量中的 Key 重建客户端
        if self._degraded:
            api_key = _read_env_key(self.config.api_key_env)
            if api_key and self.config.base_url:
                old_client = self._client
                try:
                    self._client = AsyncOpenAI(api_key=api_key, base_url=self.config.base_url)
                    self._degraded = False
                    self._initialized = True
                    logger.info("sub_agent.auto_recovered", name=self.config.name)
                    # 关闭旧客户端释放连接
                    if old_client is not None:
                        try:
                            await old_client.close()
                        except (OSError, RuntimeError):
                            logger.debug("agent_dispatcher.close_old_client_error", exc_info=True)
                except (ImportError, ValueError, OSError) as e:
                    logger.debug("sub_agent.recover_failed", name=self.config.name, error=str(e)[:80])

        if not self.available:
            return f"{self.config.display_name}现在有点累了...等会儿再来吧！💤"

        # 单次任务开始时重置记忆提交计数
        self._memory_submit_count = 0

        if status_callback:
            try:
                await status_callback(get_status_msg(self.config.name, "thinking", "", self.config.personality_file))
            except (AttributeError, RuntimeError, OSError):
                pass  # status_callback 失败不影响任务执行

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

        try:
            return await self._chat_loop(messages, tools)
        except (TimeoutError, OSError, RuntimeError, ValueError) as e:
            logger.warning("sub_agent.chat_failed name={} error={}", self.config.name, str(e))
            if tools and _is_tool_unsupported_error(str(e)):
                try:
                    return await self._chat_loop(messages, None)
                except (TimeoutError, OSError, RuntimeError, ValueError) as e2:
                    logger.warning("sub_agent.fallback_failed name={} error={}", self.config.name, str(e2))

        return f"{self.config.display_name}现在有点累了...等会儿再来吧！💤"

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

    def _build_dsml_tool_prompt(self) -> str:
        tools = self._filtered_tools()
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

    async def _chat_loop(self, messages: list[dict], tools: list[dict] | None) -> str:
        """主循环：调用 LLM → 提取工具调用 → 执行 → 反馈，最多 max_rounds 轮。"""
        max_rounds = self.config.max_turns if self.config.max_turns is not None else 5
        working = list(messages)
        tool_names = self._filtered_tool_names()
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
                if not content:
                    rc = getattr(msg, "reasoning_content", None) or ""
                    if rc:
                        content = rc
                content = strip_dsml(content)
                content = strip_reasoning(content)
                logger.info("sub_agent.chat.ok", name=self.config.name, model=self.config.model, rounds=round_idx)
                result = content.strip()
                # 兜底：如果过滤后为空（如模型只输出推理泄露），返回提示
                if not result:
                    return f"{self.config.display_name}思考了一下，但还没有整理好回答，请稍等或换个问题问我吧～"
                return result

            # 构造 assistant 消息并加入 working
            working.append(self._build_assistant_msg(msg, extracted, is_dsml))

            # 统一执行工具调用
            await self._execute_round_tool_calls(extracted, working)

        # 达到最大轮次：让 LLM 基于已有工具结果做总结回复
        remaining = total_deadline - asyncio.get_running_loop().time()
        if remaining < 5:
            return f"{self.config.display_name}现在有点累了...等会儿再来吧！💤"
        return await self._summarize_after_tools(working, api_timeout, remaining)

    def _inject_dsml_if_needed(self, working: list[dict], tools: list[dict] | None,
                                is_reasoning: bool,
                                tool_names: list[str]) -> list[dict] | None:
        """推理模型注入 DSML 工具提示并禁用原生 tools; 非推理模型保持原样返回 tools"""
        if is_reasoning and tools:
            dsml_prompt = self._build_dsml_tool_prompt()
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
                # 为 agnes 模型读取全局 thinking 配置
                extra_body = None
                if self.config.provider == "agnes":
                    # 读取 ROUTE_TABLE 中 chat 任务的 thinking 配置（全局开关）
                    from model_router import ROUTE_TABLE
                    chat_config = ROUTE_TABLE.get("chat", {})
                    thinking_enabled = chat_config.get("thinking") is not None
                    extra_body = {"chat_template_kwargs": {"enable_thinking": thinking_enabled}}
                response = await asyncio.wait_for(
                    self._client.chat.completions.create(
                        model=self.config.model,
                        messages=working,
                        max_tokens=1024 if tools else 800,
                        temperature=0.9,
                        tools=tools,
                        tool_choice="auto" if tools else None,
                        extra_body=extra_body,
                    ),
                    timeout=cur_timeout,
                )
                elapsed = loop.time() - t0
                logger.info("sub_agent.api_ok", name=self.config.name,
                            round=round_idx, attempt=attempt, elapsed=f"{elapsed:.1f}s",
                            thinking=extra_body.get("chat_template_kwargs", {}).get("enable_thinking") if extra_body else None)
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

    async def _execute_round_tool_calls(self, extracted: Any, working: list[dict]) -> None:
        """并行执行本轮工具调用, 将结果 (含错误) 追加到 working"""
        tool_results = await asyncio.gather(
            *[self._exec_one_tool_call(tc) for tc in extracted],
            return_exceptions=True,
        )
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

    async def _exec_one_tool_call(self, tc: Any) -> dict:
        """执行单个工具调用：风暴检测 → 截断修复 → 护栏检查 → 执行 → 后处理。"""
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

        # 过滤被禁止的工具
        if tool_name in DELEGATE_BLOCKED_TOOLS:
            tool_result_content = json.dumps({
                "error": f"工具 {tool_name} 在子代理中被禁止使用"
            }, ensure_ascii=False)
            return {"tool_call_id": tc.id, "content": tool_result_content}

        # 子代理专属工具：submit_memory（实例方法拦截，不走全局 executor）
        if tool_name == "submit_memory":
            try:
                result_text = await self.submit_memory(**args)
            except (OSError, ValueError, RuntimeError) as e:
                logger.warning("sub_agent.submit_memory_call_failed", error=str(e)[:200])
                result_text = f"错误: {e}"
            return {"tool_call_id": tc.id, "content": result_text}

        # 子代理专属工具：send_message_to_agent（实例方法拦截，不走全局 executor）
        if tool_name == "send_message_to_agent":
            try:
                result_text = await self.send_message_to_agent(**args)
            except (OSError, ValueError, RuntimeError) as e:
                logger.warning("sub_agent.send_message_to_agent_call_failed", error=str(e)[:200])
                result_text = f"错误: {e}"
            return {"tool_call_id": tc.id, "content": result_text}

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

    async def _summarize_after_tools(self, working: list[dict], api_timeout: int,
                                       remaining: float) -> str:
        """达到最大轮次后：若有未消化的 tool 结果，让 LLM 做一次总结回复。

        超时或异常时降级返回 tool 内容的前若干行，避免完全无响应。
        """
        last_tool = working[-1] if working else {}
        if isinstance(last_tool, dict) and last_tool.get("role") == "tool":
            working.append({
                "role": "system",
                "content": f"你已经调用了工具并拿到了结果。现在请基于工具返回的数据，用{self.config.display_name}的风格做一个完整、详细的总结回复。不要只复制原始数据，要用自然语言解释关键信息。如果数据有异常要指出。",
            })

        try:
            # 为 agnes 模型读取全局 thinking 配置
            extra_body = None
            if self.config.provider == "agnes":
                from model_router import ROUTE_TABLE
                chat_config = ROUTE_TABLE.get("chat", {})
                thinking_enabled = chat_config.get("thinking") is not None
                extra_body = {"chat_template_kwargs": {"enable_thinking": thinking_enabled}}
            response = await asyncio.wait_for(
                self._client.chat.completions.create(
                    model=self.config.model,
                    messages=working,
                    max_tokens=800,
                    temperature=0.7,
                    extra_body=extra_body,
                ),
                timeout=min(api_timeout, remaining),
            )
            reply = response.choices[0].message.content or ""
            rc = getattr(response.choices[0].message, "reasoning_content", None) or ""
            result = strip_reasoning(strip_dsml(reply or rc)).strip()
            # 兜底：如果过滤后为空（如模型只输出推理泄露），返回提示
            if not result:
                return f"{self.config.display_name}思考了一下，但还没有整理好回答，请稍等或换个问题问我吧～"
            return result
        except (TimeoutError, Exception):
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
            return f"{self.config.display_name}现在有点累了...等会儿再来吧！💤"
    async def submit_memory(self, key_points: list[str], importance: int = 3) -> str:
        """子代理向主记忆提交关键信息（受控写入）"""
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
            )
            # 同步写入向量索引（与 remember 工具保持一致）
            if getattr(mm, "vec", None) and memory_text:
                try:
                    await mm.vec.upsert(mem_id, memory_text)
                except (OSError, RuntimeError, ValueError) as ve:
                    logger.warning("sub_agent.submit_memory.vec_failed", error=str(ve)[:200])

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


# RouterEngine agent name → task_type 反向映射
# 用于 classify_task 委托 RouterEngine 后保持返回格式一致（task_type 字符串）
_AGENT_TO_TASK_TYPE = {
    "xiaolang": "security",
    "xiaoke": "debug",
    "xiaolian": "info_search",
    "xiaoli": "emotional",
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
        """关闭所有 SubAgent 的 AsyncOpenAI 客户端."""
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
        agent = self._agents.get(name)
        if not agent:
            logger.warning("dispatcher.agent_not_found", name=name)
            return None
        return await agent.chat(task, context=context, status_callback=status_callback, address_term=address_term, extra_system_prompt=extra_system_prompt)

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
        """刷新所有子 Agent 的客户端（Setup 保存新 Key 后调用）。

        清除降级标记，用最新环境变量中的 Key 重建客户端。
        返回成功刷新的子 Agent 数量。
        """
        count = 0
        for name, agent in self._agents.items():
            try:
                api_key = _read_env_key(agent.config.api_key_env)
                if api_key and agent.config.base_url:
                    agent._client = AsyncOpenAI(api_key=api_key, base_url=agent.config.base_url)
                    agent._initialized = True
                    agent._degraded = False  # 清除降级标记
                    count += 1
                    logger.info("sub_agent.client_refreshed", name=name)
            except (ValueError, OSError, RuntimeError) as e:
                logger.warning("sub_agent.client_refresh_failed", name=name, error=str(e)[:200])
        return count

    def route_task(self, task_type: str, input_text: str) -> str:
        """根据任务类型路由到对应子代理

        参考 Trae SOLO 模式：任务→代理 1:1 绑定

        :param task_type: 任务类型
            - "frontend" → 妮可（xiaoke，编程助手）
            - "backend" → 妮可（xiaoke）
            - "debug" → 妮可（xiaoke）
            - "security" → 小狼（xiaolang，系统管理）
            - "test" → 妮可（xiaoke）
            - "info_search" → 希里安（xiaolian，信息助手）
            - "hardware" → 小狼（xiaolang）
            - "emotional" → 小莉（xiaoli，萌系陪伴）
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
                pass  # work_record 不可用时使用默认路由
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
        try:
            engine = self._get_router_engine()
            decision = engine.decide(user_input)
            for agent in decision.agent_names:
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
