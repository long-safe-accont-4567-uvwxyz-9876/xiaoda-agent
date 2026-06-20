import os
import json
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable
from openai import AsyncOpenAI

from loguru import logger
from tool_engine.tool_registry import to_openai_tools
from tool_engine.tool_executor import ToolExecutor, ToolResult
from tool_engine.tool_repair import ToolCallRepair
from utils.text_utils import has_dsml_tool_calls, parse_dsml_tool_calls, strip_dsml
from emotion.tts_engine import TTSEngine
from emotion.emoji_config import get_status_msg
from tool_engine.tool_guardrails import get_tool_guardrails
from utils.credential_pool import get_credential_pool, CredentialPool
from core.message import AgentMessage


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

    def extract(self, message) -> list[ExtractedToolCall] | None:
        """从 message 中提取工具调用。返回 None 表示无工具调用。"""
        ...


class StandardExtractor:
    """从标准 message.tool_calls 中提取工具调用。"""

    def extract(self, message) -> list[ExtractedToolCall] | None:
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

    def __init__(self, allowed_tools: set[str] | None = None):
        self._allowed_tools = allowed_tools

    def extract(self, message) -> list[ExtractedToolCall] | None:
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


def _read_env_key(env_var: str) -> str:
    key = os.environ.get(env_var, "")
    if key:
        return key
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith(f"{env_var}="):
                return line.split("=", 1)[1].strip()
    return ""


def _is_tool_unsupported_error(error_str: str) -> bool:
    lower = error_str.lower()
    keywords = ["tool", "function", "not support", "unsupported", "does not have"]
    return any(kw in lower for kw in keywords)


class SubAgent:
    def __init__(self, config: SubAgentConfig, tts: TTSEngine,
                 tool_executor: ToolExecutor | None = None,
                 tool_repair: ToolCallRepair | None = None,
                 delegate_callback=None,
                 core=None):
        self.config = config
        self._tts = tts
        self._tool_executor = tool_executor
        self._tool_repair = tool_repair
        self._delegate_callback = delegate_callback
        self._core = core
        self._client: AsyncOpenAI | None = None
        self._personality: str = ""
        self._initialized = False
        self._credential_pool: CredentialPool | None = None
        self._memory_submit_count = 0
        self._communicating_with: str | None = None

    async def init(self):
        api_key = _read_env_key(self.config.api_key_env)
        if api_key and self.config.base_url:
            self._client = AsyncOpenAI(api_key=api_key, base_url=self.config.base_url)

        if self.config.personality_file:
            p = Path(self.config.personality_file)
            if p.exists():
                self._personality = p.read_text(encoding="utf-8")

        if not self._personality:
            self._personality = f"你是{self.config.display_name}。"

        if self.config.effort:
            effort_hints = {
                "low": "请简洁回答，不需要深入分析。",
                "medium": "请适度分析后回答。",
                "high": "请深入思考和分析后给出详细回答。",
            }
            hint = effort_hints.get(self.config.effort, "")
            if hint:
                self._personality = f"{self._personality}\n\n{hint}"

        self._initialized = self._client is not None
        if self._initialized:
            probe_enabled = os.environ.get("SUBAGENT_PROBE_ENABLED", "on").lower() in ("on", "1", "true")
            if probe_enabled:
                try:
                    await asyncio.wait_for(
                        self._client.chat.completions.create(
                            model=self.config.model,
                            messages=[{"role": "user", "content": "hi"}],
                            max_tokens=1,
                        ),
                        timeout=15,
                    )
                    logger.info("sub_agent.probe_ok", name=self.config.name)
                except Exception as e:
                    logger.warning("sub_agent.probe_failed", name=self.config.name, error=str(e)[:200])
                    self._initialized = False
                    self._client = None
            else:
                logger.info("sub_agent.initialized", name=self.config.name, provider=self.config.provider, model=self.config.model)

    def set_credential_pool(self, pool: CredentialPool):
        self._credential_pool = pool

    @property
    def available(self) -> bool:
        return self._initialized and self._client is not None

    def _filtered_tools(self) -> list[dict] | None:
        if not self._tool_executor:
            return None
        all_tools = to_openai_tools()
        excluded = self.config.excluded_tools
        tools = [t for t in all_tools if t["function"]["name"] not in excluded]

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

        tools.append({
            "type": "function",
            "function": {
                "name": "send_message_to_agent",
                "description": "直接向另一个子代理发消息获取响应（无需通过纳西妲中转）",
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
        names.add("submit_memory")
        names.add("send_message_to_agent")
        return names

    async def chat(self, message: str, context: str = "", status_callback=None) -> str:
        if not self.available:
            return f"{self.config.display_name}现在有点累了...等会儿再来吧！💤"

        self._memory_submit_count = 0

        if status_callback:
            try:
                await status_callback(get_status_msg(self.config.name, "thinking", "", self.config.personality_file))
            except Exception:
                pass

        system_prompt = self._personality
        if context:
            system_prompt += f"\n\n[背景信息]\n{context}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ]

        tools = self._filtered_tools()

        try:
            reply = await self._chat_loop(messages, tools)
            return reply
        except Exception as e:
            logger.warning("sub_agent.chat_failed", name=self.config.name, error=str(e))
            if tools and _is_tool_unsupported_error(str(e)):
                try:
                    reply = await self._chat_loop(messages, None)
                    return reply
                except Exception as e2:
                    logger.warning("sub_agent.fallback_failed", name=self.config.name, error=str(e2))

        return f"{self.config.display_name}现在有点累了...等会儿再来吧！💤"

    async def _handle_tool_result(self, tool_name: str, result: ToolResult) -> str:
        result_text = ""
        from core.delegation import DelegationRequest
        delegation_req = None
        if result.success and isinstance(result.data, DelegationRequest):
            delegation_req = result.data
        elif result.success and isinstance(result.data, AgentMessage) and result.data.is_delegate_request():
            delegation_req = DelegationRequest(
                type="nahida", question=result.data.content, delegator=self.config.name
            )
        elif result.success and isinstance(result.data, str) and result.data.startswith("[NAHIDA_PENDING]"):
            import logging
            logging.getLogger(__name__).warning(
                "使用废弃的 [NAHIDA_PENDING] 字符串匹配识别委托，请迁移到 AgentMessage 协议"
            )
            delegation_req = DelegationRequest(
                type="nahida", question=result.data[len("[NAHIDA_PENDING]"):], delegator=self.config.name
            )

        if delegation_req and delegation_req.type == "nahida":
            question = delegation_req.question
            if self._delegate_callback:
                from agent_core import _current_request_ctx
                _ctx = _current_request_ctx.get()
                if _ctx and _ctx.delegate_depth >= 2:
                    logger.warning("delegate.depth_exceeded", depth=_ctx.delegate_depth, from_agent=self.config.name)
                    result_text = "纳西妲姐姐现在也在忙，先自己想想办法吧！"
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
        return any(kw in model for kw in ["v4-flash", "v4-pro", "v3", "reasoner", "r1"])

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
                f'{k}({", ".join(v.get("enum", []))})' if "enum" in v else k
                for k, v in params.items()
            )
            req_mark = "必填" if required else ""
            lines.append(f'- {f["name"]}({param_desc}) {req_mark}: {f.get("description", "")}')
        lines.append("""\n调用格式示例:\n<｜｜DSML｜｜tool_calls>\n<｜｜DSML｜｜invoke name="web_search">\n<｜｜DSML｜｜parameter name="query">搜索关键词</｜｜DSML｜｜parameter>\n</｜｜DSML｜｜invoke>\n</｜｜DSML｜｜tool_calls>\n\n重要：需要调用工具时必须使用上述DSML格式，不要用其他格式。不需要调用工具时直接回复即可。""")
        return "\n".join(lines)

    async def _chat_loop(self, messages: list[dict], tools: list[dict] | None) -> str:
        max_rounds = self.config.max_turns if self.config.max_turns is not None else 5
        working = list(messages)
        tool_names = self._filtered_tool_names()
        api_timeout = 60
        total_deadline = asyncio.get_running_loop().time() + 150
        is_reasoning = self._is_reasoning_model()

        standard_ext = StandardExtractor()
        dsml_ext = DsmlExtractor(allowed_tools=tool_names)

        if is_reasoning and tools:
            dsml_prompt = self._build_dsml_tool_prompt()
            if dsml_prompt and working and working[0]["role"] == "system":
                working[0] = {
                    "role": "system",
                    "content": working[0]["content"] + "\n\n" + dsml_prompt,
                }
            tools = None

        for round_idx in range(max_rounds):
            if asyncio.get_running_loop().time() > total_deadline:
                logger.warning("sub_agent.total_timeout", name=self.config.name)
                return f"{self.config.display_name}处理超时了，请稍后再试吧～"

            remaining = total_deadline - asyncio.get_running_loop().time()
            if remaining < 10:
                logger.warning("sub_agent.time_exhausted", name=self.config.name)
                return f"{self.config.display_name}处理超时了，请稍后再试吧～"

            try:
                t0 = asyncio.get_running_loop().time()
                response = await asyncio.wait_for(
                    self._client.chat.completions.create(
                        model=self.config.model,
                        messages=working,
                        max_tokens=1024 if tools else 800,
                        temperature=0.9,
                        tools=tools,
                        tool_choice="auto" if tools else None,
                    ),
                    timeout=min(api_timeout, remaining),
                )
                elapsed = asyncio.get_running_loop().time() - t0
                logger.info("sub_agent.api_ok", name=self.config.name, round=round_idx, elapsed=f"{elapsed:.1f}s")
            except asyncio.TimeoutError:
                logger.warning("sub_agent.api_timeout", name=self.config.name, round=round_idx)
                return f"{self.config.display_name}思考时间太长了，请稍后再试吧～"

            msg = response.choices[0].message

            extracted = standard_ext.extract(msg)
            is_dsml = False
            if extracted is None and self._tool_executor:
                extracted = dsml_ext.extract(msg)
                is_dsml = extracted is not None

            if extracted is None:
                content = msg.content or ""
                if not content:
                    rc = getattr(msg, "reasoning_content", None) or ""
                    if rc:
                        content = rc
                logger.info("sub_agent.chat.ok", name=self.config.name, model=self.config.model, rounds=round_idx)
                return content.strip()

            msg_rc = getattr(msg, "reasoning_content", None) or ""
            if is_dsml:
                clean_content = strip_dsml(msg.content or "")
                assistant_msg = {
                    "role": "assistant",
                    "content": clean_content,
                    "tool_calls": [
                        {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments_json}}
                        for tc in extracted
                    ],
                }
            else:
                assistant_msg = {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments_json}}
                        for tc in extracted
                    ],
                }
            if msg_rc:
                assistant_msg["reasoning_content"] = msg_rc
            working.append(assistant_msg)

            async def _exec_one(tc: ExtractedToolCall):
                tool_name = tc.name
                args_str = tc.arguments_json

                if self._tool_repair:
                    repaired = self._tool_repair.repair_truncation(args_str)
                    if repaired:
                        args_str = repaired

                args = tc.parse_arguments()

                if tool_name in DELEGATE_BLOCKED_TOOLS:
                    tool_result_content = json.dumps({
                        "error": f"工具 {tool_name} 在子代理中被禁止使用"
                    }, ensure_ascii=False)
                    return {"tool_call_id": tc.id, "content": tool_result_content}

                if tool_name == "submit_memory":
                    try:
                        result_text = await self.submit_memory(**args)
                    except Exception as e:
                        logger.warning("sub_agent.submit_memory_call_failed", error=str(e)[:200])
                        result_text = f"错误: {e}"
                    return {"tool_call_id": tc.id, "content": result_text}

                if tool_name == "send_message_to_agent":
                    try:
                        result_text = await self.send_message_to_agent(**args)
                    except Exception as e:
                        logger.warning("sub_agent.send_message_to_agent_call_failed", error=str(e)[:200])
                        result_text = f"错误: {e}"
                    return {"tool_call_id": tc.id, "content": result_text}

                guardrails = get_tool_guardrails()
                action, guard_msg = await guardrails.check(tool_name, args)
                if action == "halt":
                    return {"tool_call_id": tc.id, "content": f"错误: {guard_msg}"}

                result = await self._tool_executor.execute(tool_name, args)

                await guardrails.record_call(tool_name, args, result.success,
                                       str(result.data)[:100] if result.data else "")

                result_text = await self._handle_tool_result(tool_name, result)

                if action == "warn" and guard_msg and result.success:
                    result_text = f"[护栏警告: {guard_msg}]\n{result_text}"

                return {"tool_call_id": tc.id, "content": result_text}

            tool_results = await asyncio.gather(*[_exec_one(tc) for tc in extracted], return_exceptions=True)
            for tc, r in zip(extracted, tool_results):
                if isinstance(r, Exception):
                    logger.warning("sub_agent.tool_error", name=self.config.name, tool=tc.name, error=str(r))
                    working.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"错误: {r}",
                    })
                else:
                    working.append({
                        "role": "tool",
                        "tool_call_id": r["tool_call_id"],
                        "content": r["content"],
                    })

        remaining = total_deadline - asyncio.get_running_loop().time()
        if remaining < 5:
            return f"{self.config.display_name}现在有点累了...等会儿再来吧！💤"

        last_tool = working[-1] if working else {}
        if isinstance(last_tool, dict) and last_tool.get("role") == "tool":
            working.append({
                "role": "system",
                "content": f"你已经调用了工具并拿到了结果。现在请基于工具返回的数据，用{self.config.display_name}的风格做一个完整、详细的总结回复。不要只复制原始数据，要用自然语言解释关键信息。如果数据有异常要指出。",
            })

        try:
            response = await asyncio.wait_for(
                self._client.chat.completions.create(
                    model=self.config.model,
                    messages=working,
                    max_tokens=800,
                    temperature=0.7,
                ),
                timeout=min(api_timeout, remaining),
            )
            reply = response.choices[0].message.content or ""
            rc = getattr(response.choices[0].message, "reasoning_content", None) or ""
            return (reply or rc).strip()
        except (asyncio.TimeoutError, Exception):
            last_tool = working[-1] if working else {}
            if isinstance(last_tool, dict) and last_tool.get("role") == "tool":
                import re
                raw_content = last_tool.get("content", "").strip()
                lines = [l.strip() for l in raw_content.splitlines() if l.strip()]
                if len(lines) > 1:
                    formatted = "\n".join(lines[:15])
                    if len(lines) > 15:
                        formatted += f"\n...（共{len(lines)}行）"
                    return formatted
                return raw_content
            return f"{self.config.display_name}现在有点累了...等会儿再来吧！💤"

    async def submit_memory(self, key_points: list[str], importance: int = 3) -> str:
        if self._memory_submit_count >= 3:
            return "已达本次任务记忆提交上限（3次）"

        if importance > 4:
            importance = 4

        memory_text = f"[{self.config.display_name}观察] " + "; ".join(key_points)

        if not self._core or not hasattr(self._core, "memory") or self._core.memory is None:
            return "（记忆系统不可用）"

        try:
            mm = self._core.memory
            importance_float = importance / 4.0
            mem_id = await mm.memory.insert_episodic_memory(
                summary=memory_text,
                importance=importance_float,
                emotion_label="",
                source="sub_agent",
            )
            if getattr(mm, "vec", None) and memory_text:
                try:
                    await mm.vec.upsert(mem_id, memory_text)
                except Exception as ve:
                    logger.warning("sub_agent.submit_memory.vec_failed", error=str(ve)[:200])

            self._memory_submit_count += 1
            logger.info("sub_agent.submit_memory", name=self.config.name, count=self._memory_submit_count)
            return f"已记录：{memory_text[:50]}..."
        except Exception as e:
            logger.warning("sub_agent.submit_memory_failed", error=str(e)[:200])
            return "（记忆系统不可用）"

    async def send_message_to_agent(self, target_agent: str, message: str) -> str:
        if "send_message_to_agent" in message or self._communicating_with == target_agent:
            return "（避免循环通信）"

        if not self._core or not hasattr(self._core, "dispatcher") or self._core.dispatcher is None:
            return "（找不到通信渠道）"

        dispatcher = self._core.dispatcher

        target = None
        try:
            target = dispatcher.get_agent(target_agent)
        except Exception:
            target = None

        if target is None:
            agents_dict = getattr(dispatcher, "_agents", {}) or {}
            for _, agent in agents_dict.items():
                if getattr(agent.config, "display_name", "") == target_agent:
                    target = agent
                    break

        if target is None:
            return f"（找不到 {target_agent}）"

        context = f"这是{self.config.display_name}发来的消息：\n{message}"
        self._communicating_with = target_agent
        try:
            reply = await target.chat(message, context=context)
            return reply if reply else f"（{target_agent} 没有回应）"
        except Exception as e:
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


class AgentDispatcher:
    def __init__(self, tts: TTSEngine,
                 tool_executor: ToolExecutor | None = None,
                 tool_repair: ToolCallRepair | None = None,
                 delegate_callback=None,
                 core=None):
        self._tts = tts
        self._tool_executor = tool_executor
        self._tool_repair = tool_repair
        self._delegate_callback = delegate_callback
        self._core = core
        self._agents: dict[str, SubAgent] = {}

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

        if not agent.available:
            logger.warning("dispatcher.register_unavailable", name=config.name)
            return False

        self._agents[config.name] = agent
        logger.info("dispatcher.registered", name=config.name, display_name=config.display_name)
        return True

    def unregister(self, name: str) -> bool:
        if name not in self._agents:
            return False
        del self._agents[name]
        logger.info("dispatcher.unregistered", name=name)
        return True

    async def dispatch(self, name: str, task: str, context: str = "", status_callback=None) -> str | None:
        agent = self._agents.get(name)
        if not agent:
            logger.warning("dispatcher.agent_not_found", name=name)
            return None
        return await agent.chat(task, context=context, status_callback=status_callback)

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
