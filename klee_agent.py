from typing import Any, Optional
import os
import json
import asyncio
import time
from pathlib import Path
from openai import AsyncOpenAI

from loguru import logger
from tool_engine.tool_registry import to_openai_tools
from tool_engine.tool_executor import ToolExecutor, ToolResult
from tool_engine.tool_repair import ToolCallRepair
from utils.text_utils import has_dsml_tool_calls, parse_dsml_tool_calls, strip_dsml
from emotion.tts_engine import TTSEngine
from core.message import AgentMessage


PROVIDERS = [
    {
        "name": "mimo",
        "base_url": "https://api.xiaomimimo.com/v1",
        "api_key_env": "MIMO_API_KEY",
        "models": ["mimo-v2.5"],
    },
]

TIRED_MSG = "可莉现在有点累了...等会儿再来找大哥哥玩吧！蹦蹦...💤"

EXCLUDED_TOOLS = {"call_klee", "delegate_task"}


def _klee_tools() -> list[dict]:
    all_tools = to_openai_tools()
    return [t for t in all_tools if t["function"]["name"] not in EXCLUDED_TOOLS]


def _klee_tool_names() -> set[str]:
    return {t["function"]["name"] for t in _klee_tools()}


def _read_env_key(env_var: str) -> str:
    key = os.environ.get(env_var, "")
    if key:
        return key
    # frozen 模式下 .env 在用户目录 ~/.ai-agent/.env
    try:
        from config import ENV_PATH
        env_path = Path(ENV_PATH)
    except ImportError:
        env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith(f"{env_var}="):
                return line.split("=", 1)[1].strip()
    return ""


def _is_tool_unsupported_error(error_str: str) -> bool:
    lower = error_str.lower()
    keywords = ["tool", "function", "not support", "unsupported", "does not have"]
    return any(kw in lower for kw in keywords)


class KleeAgent:
    def __init__(self, tool_executor: ToolExecutor | None = None,
                 tool_repair: ToolCallRepair | None = None,
                 nahida_delegate: Optional[Any]=None) -> None:
        self._clients: list[tuple[str, AsyncOpenAI, list[str]]] = []
        self._personality: str = ""
        self._initialized = False
        self._tool_executor = tool_executor
        self._tool_repair = tool_repair
        self._preferred_provider: str = "mimo"
        self._nahida_delegate = nahida_delegate
        self.tts = TTSEngine()

    async def init(self) -> None:
        for provider in PROVIDERS:
            api_key = _read_env_key(provider["api_key_env"])
            if not api_key:
                logger.warning("klee.no_api_key", provider=provider["name"])
                continue

            client = AsyncOpenAI(
                api_key=api_key,
                base_url=provider["base_url"],
            )
            self._clients.append((provider["name"], client, provider["models"]))
            logger.info("klee.provider_ready", provider=provider["name"], models=len(provider["models"]))

        try:
            from config import AGENTS_CONFIG_DIR
            personality_path = AGENTS_CONFIG_DIR / "klee_personality.md"
        except ImportError:
            personality_path = Path(__file__).parent / "config" / "agents" / "klee_personality.md"
        if personality_path.exists():
            self._personality = personality_path.read_text(encoding="utf-8-sig")
        else:
            self._personality = "你是可莉，蒙德城的火花骑士！活泼可爱，称呼用户为大哥哥或大姐姐。"

        self._initialized = len(self._clients) > 0
        if self._initialized:
            logger.info("klee.initialized", providers=[c[0] for c in self._clients])

        await self.tts.init()

    @property
    def available(self) -> bool:
        return self._initialized and len(self._clients) > 0

    def set_preferred_provider(self, name: str) -> None:
        self._preferred_provider = name

    def get_preferred_provider(self) -> str:
        return self._preferred_provider

    async def chat(self, message: str, context: str = "",
                   status_callback: Optional[Any]=None) -> str:
        if not self.available:
            return TIRED_MSG

        if status_callback:
            try:
                await status_callback("可莉正在思考...")
            except Exception:
                pass

        system_prompt = self._personality
        if context:
            system_prompt += f"\n\n[背景信息]\n{context}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ]

        klee_tools = _klee_tools()
        tools = klee_tools if (self._tool_executor and klee_tools) else None

        ordered = self._ordered_clients()
        for provider_name, client, models in ordered:
            for model in models:
                try:
                    reply = await self._chat_loop(client, model, messages, tools, provider_name)
                    return reply
                except Exception as e:
                    error_str = str(e)
                    if "429" in error_str or "rate" in error_str.lower():
                        logger.warning("klee.rate_limited", provider=provider_name, model=model)
                        continue
                    if tools and _is_tool_unsupported_error(error_str):
                        logger.warning("klee.tools_not_supported", provider=provider_name, model=model)
                        try:
                            reply = await self._chat_loop(client, model, messages, None, provider_name)
                            return reply
                        except Exception as e2:
                            logger.warning("klee.fallback_failed", provider=provider_name, model=model, error=str(e2))
                            continue
                    logger.warning("klee.chat.error", provider=provider_name, model=model, error=error_str)
                    continue

        logger.warning("klee.all_providers_exhausted")
        return TIRED_MSG

    def _ordered_clients(self) -> list[tuple[str, AsyncOpenAI, list[str]]]:
        preferred = [c for c in self._clients if c[0] == self._preferred_provider]
        others = [c for c in self._clients if c[0] != self._preferred_provider]
        return preferred + others

    async def _handle_tool_result(self, tool_name: str, result: ToolResult) -> str:
        result_text = ""
        from core.delegation import DelegationRequest
        delegation_req = None
        if result.success and isinstance(result.data, DelegationRequest):
            delegation_req = result.data
        elif result.success and isinstance(result.data, AgentMessage) and result.data.is_delegate_request():
            # 优先用 AgentMessage 结构化协议识别
            delegation_req = DelegationRequest(
                type="nahida", question=result.data.content, delegator="klee"
            )
        elif result.success and isinstance(result.data, str) and result.data.startswith("[NAHIDA_PENDING]"):
            # fallback: 旧字符串匹配（过渡期保留）
            import logging
            logging.getLogger(__name__).warning(
                "使用废弃的 [NAHIDA_PENDING] 字符串匹配识别委托，请迁移到 AgentMessage 协议"
            )
            delegation_req = DelegationRequest(
                type="nahida", question=result.data[len("[NAHIDA_PENDING]"):], delegator="klee"
            )

        if delegation_req and delegation_req.type == "nahida":
            question = delegation_req.question
            if self._nahida_delegate:
                logger.info("klee.calling_nahida", question=question[:50])
                nahida_reply = await self._nahida_delegate(question)
                result_text = f"[纳西妲姐姐的回答（可莉必须用自己的话转述给大哥哥，不要直接复制纳西妲姐姐的原话，要加上可莉自己的感觉和语气）]\n{nahida_reply}"
            else:
                result_text = "纳西妲姐姐现在不在...可莉自己想想办法吧！"
        elif result.success:
            result_text = json.dumps(result.data, ensure_ascii=False) if not isinstance(result.data, str) else result.data
        else:
            result_text = f"错误: {result.error}"
        return result_text[:2000]

    async def _chat_loop(self, client: AsyncOpenAI, model: str,
                         messages: list[dict], tools: list[dict] | None,
                         provider_name: str) -> str:
        """主循环：调用 LLM → 提取工具调用 → 执行 → 反馈，最多 5 轮。"""
        max_rounds = 5
        api_timeout = 30                    # 单次 API 调用超时
        total_deadline = time.time() + 90   # Klee 总超时 90s
        working = list(messages)

        for round_idx in range(max_rounds):
            # 墙钟超时检查
            remaining = total_deadline - time.time()
            if remaining < 5:
                return f"可莉处理超时了，请稍后再试吧～"
            try:
                response = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=model,
                        messages=working,
                        max_tokens=1024 if tools else 300,
                        temperature=0.9,
                        tools=tools,
                        tool_choice="auto" if tools else None,
                    ),
                    timeout=min(api_timeout, remaining),
                )
            except asyncio.TimeoutError:
                logger.warning("klee.api_timeout", round=round_idx, model=model)
                return f"可莉思考时间太长了，请稍后再试吧～"

            msg = response.choices[0].message

            if not msg.tool_calls:
                content = msg.content or ""
                # 尝试 DSML 工具调用提取与执行
                if tools and self._tool_executor and has_dsml_tool_calls(content):
                    if await self._try_handle_dsml_calls(msg, content, working, provider_name, model, round_idx):
                        continue
                logger.info("klee.chat.ok", provider=provider_name, model=model,
                            tokens=response.usage.total_tokens if response.usage else 0,
                            rounds=round_idx, used_tools=round_idx > 0)
                return content.strip()

            # 标准 tool_calls：构造 assistant 消息并执行
            self._append_assistant_msg(msg, working)
            await self._exec_standard_tool_calls(msg, working, provider_name, model, round_idx)

        # 达到最大轮次：尝试总结
        return await self._summarize_klee_result(client, model, working, provider_name)

    async def _try_handle_dsml_calls(self, msg: Any, content: str, working: list[dict],
                                       provider_name: str, model: str, round_idx: int) -> bool:
        """DSML 工具调用提取与执行。成功处理返回 True，无 DSML 调用返回 False。"""
        dsml_calls = parse_dsml_tool_calls(content, _klee_tool_names())
        if not dsml_calls:
            return False
        logger.info("klee.dsml_tool_calls", count=len(dsml_calls), model=model)
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

            logger.info("klee.dsml_tool_executed", tool=tool_name, success=result.success,
                        provider=provider_name, model=model, round=round_idx)
        return True

    def _append_assistant_msg(self, msg: Any, working: list[dict]) -> None:
        """构造标准 assistant 消息（含 tool_calls）并追加到 working。"""
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

    async def _exec_standard_tool_calls(self, msg: Any, working: list[dict],
                                          provider_name: str, model: str, round_idx: int) -> None:
        """执行标准 tool_calls：截断修复 → 执行 → 后处理 → 追加 tool 消息。"""
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

            logger.info("klee.tool_executed", tool=tool_name, success=result.success,
                        provider=provider_name, model=model, round=round_idx)

    async def _summarize_klee_result(self, client: AsyncOpenAI, model: str,
                                      working: list[dict], provider_name: str) -> str:
        """达到最大轮次后：尝试让 LLM 做一次总结回复。

        若 working 末尾是 tool 消息则直接返回其内容；否则再调一次 LLM。
        """
        last_msg = working[-1] if working else {}
        if isinstance(last_msg, dict) and last_msg.get("role") == "tool":
            logger.info("klee.chat.direct_result", provider=provider_name, model=model)
            return last_msg.get("content", "").strip()[:3000]

        try:
            response = await client.chat.completions.create(
                model=model,
                messages=working,
                max_tokens=300,
                temperature=0.9,
            )
            reply = response.choices[0].message.content or ""
            rc = getattr(response.choices[0].message, "reasoning_content", None) or ""
            logger.info("klee.chat.max_rounds", provider=provider_name, model=model)
            return (reply or rc).strip()
        except Exception:
            return TIRED_MSG