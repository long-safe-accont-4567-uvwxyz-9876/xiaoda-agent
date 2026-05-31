import os
import json
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from openai import AsyncOpenAI

from loguru import logger
from tool_registry import to_openai_tools
from tool_executor import ToolExecutor, ToolResult
from tool_repair import ToolCallRepair
from text_utils import has_dsml_tool_calls, parse_dsml_tool_calls, strip_dsml
from tts_engine import TTSEngine
from emoji_config import get_status_msg


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
                 delegate_callback=None):
        self.config = config
        self._tts = tts
        self._tool_executor = tool_executor
        self._tool_repair = tool_repair
        self._delegate_callback = delegate_callback
        self._client: AsyncOpenAI | None = None
        self._personality: str = ""
        self._initialized = False

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

        self._initialized = self._client is not None
        if self._initialized:
            logger.info("sub_agent.initialized", name=self.config.name, provider=self.config.provider, model=self.config.model)

    @property
    def available(self) -> bool:
        return self._initialized and self._client is not None

    def _filtered_tools(self) -> list[dict] | None:
        if not self._tool_executor:
            return None
        all_tools = to_openai_tools()
        excluded = self.config.excluded_tools
        filtered = [t for t in all_tools if t["function"]["name"] not in excluded]
        return filtered if filtered else None

    def _filtered_tool_names(self) -> set[str]:
        if not self._tool_executor:
            return set()
        excluded = self.config.excluded_tools
        return {t["function"]["name"] for t in to_openai_tools() if t["function"]["name"] not in excluded}

    async def chat(self, message: str, context: str = "", status_callback=None) -> str:
        if not self.available:
            return f"{self.config.display_name}现在有点累了...等会儿再来吧！\u0001f4a4"

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

        return f"{self.config.display_name}现在有点累了...等会儿再来吧！\u0001f4a4"

    async def _handle_tool_result(self, tool_name: str, result: ToolResult) -> str:
        result_text = ""
        if result.success and isinstance(result.data, str) and result.data.startswith("[NAHIDA_PENDING]"):
            question = result.data[len("[NAHIDA_PENDING]"):]
            if self._delegate_callback:
                delegate_reply = await self._delegate_callback(question)
                result_text = f"[主Agent的回答（{self.config.display_name}需要用自己的话转述，不要直接复制原话）]\n{delegate_reply}"
            else:
                result_text = "主Agent现在不在...先自己想想办法吧！"
        elif result.success:
            result_text = json.dumps(result.data, ensure_ascii=False) if not isinstance(result.data, str) else result.data
        else:
            result_text = f"错误: {result.error}"
        return result_text[:2000]

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
        lines.append("""\n调用格式示例:\n<\uff5c\uff5cDSML\uff5c\uff5ctool_calls>
<\uff5c\uff5cDSML\uff5c\uff5cinvoke name="web_search">
<\uff5c\uff5cDSML\uff5c\uff5cparameter name="query">搜索关键词</\uff5c\uff5cDSML\uff5c\uff5cparameter>
</\uff5c\uff5cDSML\uff5c\uff5cinvoke>
</\uff5c\uff5cDSML\uff5c\uff5ctool_calls>

重要：需要调用工具时必须使用上述DSML格式，不要用其他格式。不需要调用工具时直接回复即可。""")
        return "\n".join(lines)

    async def _chat_loop(self, messages: list[dict], tools: list[dict] | None) -> str:
        max_rounds = 5
        working = list(messages)
        tool_names = self._filtered_tool_names()
        api_timeout = 60
        total_deadline = asyncio.get_event_loop().time() + 150
        is_reasoning = self._is_reasoning_model()

        if is_reasoning and tools:
            dsml_prompt = self._build_dsml_tool_prompt()
            if dsml_prompt and working and working[0]["role"] == "system":
                working[0] = {
                    "role": "system",
                    "content": working[0]["content"] + "\n\n" + dsml_prompt,
                }
            tools = None

        for round_idx in range(max_rounds):
            if asyncio.get_event_loop().time() > total_deadline:
                logger.warning("sub_agent.total_timeout", name=self.config.name)
                return f"{self.config.display_name}处理超时了，请稍后再试吧～"

            remaining = total_deadline - asyncio.get_event_loop().time()
            if remaining < 10:
                logger.warning("sub_agent.time_exhausted", name=self.config.name)
                return f"{self.config.display_name}处理超时了，请稍后再试吧～"

            try:
                t0 = asyncio.get_event_loop().time()
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
                elapsed = asyncio.get_event_loop().time() - t0
                logger.info("sub_agent.api_ok", name=self.config.name, round=round_idx, elapsed=f"{elapsed:.1f}s")
            except asyncio.TimeoutError:
                logger.warning("sub_agent.api_timeout", name=self.config.name, round=round_idx)
                return f"{self.config.display_name}思考时间太长了，请稍后再试吧～"

            msg = response.choices[0].message

            if not msg.tool_calls:
                content = msg.content or ""
                if not content:
                    rc = getattr(msg, "reasoning_content", None) or ""
                    if rc:
                        content = rc

                if tools and self._tool_executor and has_dsml_tool_calls(content):
                    dsml_calls = parse_dsml_tool_calls(content, tool_names)
                    if dsml_calls:
                        clean_content = strip_dsml(content)
                        msg_rc = getattr(msg, "reasoning_content", None) or ""
                        assistant_msg = {
                            "role": "assistant",
                            "content": clean_content,
                            "tool_calls": dsml_calls,
                        }
                        if msg_rc:
                            assistant_msg["reasoning_content"] = msg_rc
                        working.append(assistant_msg)

                        for tc in dsml_calls:
                            tool_name = tc["function"]["name"]
                            args_str = tc["function"]["arguments"]

                            if self._tool_repair:
                                repaired = self._tool_repair.repair_truncation(args_str)
                                if repaired:
                                    args_str = repaired

                            try:
                                args = json.loads(args_str)
                            except json.JSONDecodeError:
                                args = {}

                            result = await self._tool_executor.execute(tool_name, args)
                            result_text = await self._handle_tool_result(tool_name, result)

                            working.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": result_text,
                            })

                        continue

                logger.info("sub_agent.chat.ok", name=self.config.name, model=self.config.model, rounds=round_idx)
                return content.strip()

            msg_rc = getattr(msg, "reasoning_content", None) or ""
            assistant_msg = {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
            if msg_rc:
                assistant_msg["reasoning_content"] = msg_rc
            working.append(assistant_msg)

            for tc in msg.tool_calls:
                tool_name = tc.function.name
                args_str = tc.function.arguments

                if self._tool_repair:
                    repaired = self._tool_repair.repair_truncation(args_str)
                    if repaired:
                        args_str = repaired

                try:
                    args = json.loads(args_str)
                except json.JSONDecodeError:
                    args = {}

                result = await self._tool_executor.execute(tool_name, args)
                result_text = await self._handle_tool_result(tool_name, result)

                working.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_text,
                })

        remaining = total_deadline - asyncio.get_event_loop().time()
        if remaining < 5:
            return f"{self.config.display_name}现在有点累了...等会儿再来吧！\u0001f4a4"

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
            return f"{self.config.display_name}现在有点累了...等会儿再来吧！\u0001f4a4"

    async def synthesize(self, text: str, style: str = "") -> Path | None:
        if not self.config.voice_ref:
            return None
        return await self._tts.synthesize(text, voice=self.config.voice_ref, style=style)


class AgentDispatcher:
    def __init__(self, tts: TTSEngine,
                 tool_executor: ToolExecutor | None = None,
                 tool_repair: ToolCallRepair | None = None,
                 delegate_callback=None):
        self._tts = tts
        self._tool_executor = tool_executor
        self._tool_repair = tool_repair
        self._delegate_callback = delegate_callback
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