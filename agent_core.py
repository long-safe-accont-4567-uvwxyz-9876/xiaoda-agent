import os
import sys
import asyncio
import json
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import sys as _sys
if getattr(_sys, 'frozen', False):
    _env_path = str(Path(_sys.executable).parent / ".env")
else:
    _env_path = str(Path(__file__).resolve().parent / ".env")
load_dotenv(_env_path)

from utils.logging_config import setup_logging
setup_logging()

from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import MIMO_MODEL, AGENT_CONFIG, WORKSPACE_DIR, STICKER_DIR, KLEE_STICKER_DIR, FILE_DIR, build_system_prompt, SIMPLE_TASK_KEYWORDS, PRO_TASK_KEYWORDS
from model_router import ModelRouter
from agent_context import AgentContext
from db.database import DatabaseManager
from security.security import SecurityFilter
from tool_engine.tool_registry import to_openai_tools
from tool_engine.tool_executor import ToolExecutor
from tool_engine.tool_repair import ToolCallRepair
from memory.memory_manager import MemoryManager
from emotion.emotion_simple import detect_emotion, build_emotion_hint
from emotion.emotion_enum import CN_TO_EN, is_unified, ensure_emotion_tag
from utils.result_wrapper import ResultWrapper
from utils.text_utils import strip_dsml, has_dsml_tool_calls, parse_dsml_tool_calls, humanize, encode_image_to_base64
from emotion.portrait_manager import PortraitManager
from memory.notebook_manager import NotebookManager
from memory.learning_manager import LearningManager
from slash_commands import SlashCommandHandler
from emotion.sticker_manager import StickerManager
from utils.file_receiver import FileReceiver
from tool_engine.tool_call_handler import ToolCallHandler
from klee_agent import KleeAgent
from emotion.tts_engine import TTSEngine
from agent_dispatcher import AgentDispatcher
from tool_engine.mcp_client import MCPManager
from task_orchestrator import TaskGraph, run_task_graph
from instinct_manager import InstinctManager
from utils.credential_pool import get_credential_pool
from utils.error_classifier import ErrorClassifier
from hooks import get_hook_engine, HookEngine

import tools.file_tools_v2
import tools.code_tools_v2
import tools.web_tools_v2
import tools.document_tools
import tools.web_browse_tools
import tools.multi_search_tools
import tools.hardware_tools
import tools.vision_tools
import tools.system_tools
import tools.agnes_tools


def _extract_reasoning_content(response: Any) -> str | None:
    try:
        message = response.choices[0].message
        if hasattr(message, "reasoning_content"):
            return message.reasoning_content
        if hasattr(message, "model_extra") and isinstance(message.model_extra, dict):
            return message.model_extra.get("reasoning_content")
        if isinstance(message, dict):
            return message.get("reasoning_content")
    except (AttributeError, IndexError):
        pass
    return None


def _extract_delta_reasoning_content(chunk_dict: dict) -> str | None:
    try:
        delta = chunk_dict.get("choices", [{}])[0].get("delta", {})
        return delta.get("reasoning_content")
    except (IndexError, AttributeError):
        pass
    return None


DEGRADED_REPLY = "嗯……人家现在有点不太舒服，等会儿再聊好不好？"

from core.background_tasks import BackgroundTaskManager, _spawn, _bg_tasks
from core.bootstrap import AgentCoreBootstrapper
from core.router_engine import RouterEngine, RoutingDecision
from core.chat_processor import ChatProcessor
from core.tool_orchestrator import ToolOrchestrator

_current_request_ctx: ContextVar["RequestContext | None"] = ContextVar("_current_request_ctx", default=None)


@dataclass
class ProcessResult:
    reply: str
    emotion: str = ""
    sticker_path: Path | None = None
    audio_path: Path | None = None
    tool_results: list = field(default_factory=list)
    image_paths: list[Path] = field(default_factory=list)
    video_path: Path | None = None


@dataclass
class RequestContext:
    """请求级临时状态，每次 process() 调用创建一个新实例，避免并发请求时状态互相污染。"""
    session_id: str = ""
    user_openid: str = ""
    user_id: str = ""
    user_input: str = ""
    status_callback: Any = None
    handled_by_tool_call: bool = False
    last_user_emotion: str = ""
    delegate_depth: int = 0
    is_master: bool = True


class AgentCore:
    def __init__(self):
        self.router = ModelRouter()
        self.db = DatabaseManager()
        _owner_ids = os.getenv("OWNER_IDS", "").split(",")
        _owner_ids = [x.strip() for x in _owner_ids if x.strip()]
        self.security = SecurityFilter(owner_ids=_owner_ids)
        self.context = AgentContext(system_prompt_loader=build_system_prompt, router=self.router, security_filter=self.security)
        self.tool_executor = ToolExecutor(db=self.db)
        self.tool_repair = ToolCallRepair(
            allowed_tool_names=set(t["function"]["name"] for t in to_openai_tools())
        )
        self.result_wrapper = ResultWrapper(router=self.router)
        self.memory: MemoryManager | None = None
        self.portrait_manager: PortraitManager | None = None
        self.notebook_manager: NotebookManager | None = None
        self.learning_manager: LearningManager | None = None
        self.slash_handler: SlashCommandHandler | None = None
        self._initialized = False
        self.sticker_manager = StickerManager(STICKER_DIR)
        self.klee_sticker_manager = StickerManager(KLEE_STICKER_DIR)
        self.file_receiver = FileReceiver(FILE_DIR)
        self.klee = KleeAgent(tool_executor=self.tool_executor, tool_repair=self.tool_repair, nahida_delegate=self._nahida_delegate_for_klee)
        self.tts = TTSEngine()
        self.dispatcher = AgentDispatcher(
            tts=self.tts,
            tool_executor=self.tool_executor,
            tool_repair=self.tool_repair,
            delegate_callback=self._nahida_delegate_for_klee,
            core=self,
        )
        self._task_graph: TaskGraph | None = None
        self._agent_route_configs: dict = {}
        self._tool_call_handler = ToolCallHandler(self.tool_executor, self.tool_repair, self._clean_reply, self.context, self.router, klee_delegate=self.delegate_to_klee, agent_name="nahida", personality_file=str(Path(__file__).parent / "config" / "agents" / "nahida_personality.md"), tool_execute_callback=self._execute_tool_with_hooks)
        self._user_chat_target: dict[str, str] = {}
        self._chat_target_lock = asyncio.Lock()
        self._router_engine = RouterEngine(belief_router=None)  # belief_router 灰度期暂不接入
        self._chat_processor = ChatProcessor(self)
        self._tool_orchestrator = ToolOrchestrator(self)
        self._voice_mode: bool = False
        self._error_handler = None
        self._mcp_manager = MCPManager()
        self.instinct_manager: InstinctManager | None = None
        self._credential_pool = get_credential_pool()
        self._error_classifier = ErrorClassifier()
        self._hook_engine = get_hook_engine()
        self._bg_task_manager: BackgroundTaskManager | None = None

    @property
    def hook_engine(self):
        return self._hook_engine

    async def init(self) -> None:
        bootstrapper = AgentCoreBootstrapper(self)
        await bootstrapper.bootstrap()

    async def process(self, user_input: str, user_id: str = "qq_user",
                      source: str = "qq",
                      user_openid: str = "",
                      session_id: str = "",
                      status_callback=None,
                      image_data: list[dict] | None = None,
                      is_master: bool = True) -> ProcessResult:
        if not self._initialized:
            return ProcessResult(reply=DEGRADED_REPLY)

        ctx = RequestContext(
            session_id=session_id,
            user_openid=user_openid,
            user_id=user_id,
            user_input=user_input,
            status_callback=status_callback,
            is_master=is_master,
        )
        _ctx_token = _current_request_ctx.set(ctx)
        try:
            return await self._process_impl(ctx, user_input, user_id, source, user_openid, session_id, status_callback, image_data, is_master)
        finally:
            _current_request_ctx.reset(_ctx_token)

    async def _process_impl(self, ctx: RequestContext, user_input: str, user_id: str,
                             source: str, user_openid: str, session_id: str,
                             status_callback, image_data: list[dict] | None,
                             is_master: bool = True) -> ProcessResult:
        if self._tool_call_handler:
            self._tool_call_handler._tool_repair.clear_storm_window()

        trace = logger.bind(trace_id=f"{int(time.time()*1000)%1000000:06d}")
        trace.info("agent.process.start", source=source, user_id=user_id,
                    msg_preview=user_input[:80])

        allowed, reason = self.security.is_allowed(user_id)
        if not allowed:
            trace.warning("agent.blocked", reason=reason)
            return ProcessResult(reply="")

        if self.slash_handler and self.slash_handler.is_slash_command(user_input):
            slash_reply = await self.slash_handler.handle(user_input, user_id)
            return ProcessResult(reply=slash_reply)

        chat_targets = await self._parse_chat_target(user_input, user_id)
        clean_input = ChatProcessor.clean_mention_from_input(user_input)

        voice_intent = self._detect_voice_intent(clean_input)
        force_voice = voice_intent and not self._voice_mode

        if not clean_input:
            target_name = "可莉" if chat_targets and chat_targets[0] == "keli" else "纳西妲"
            confirm_msg = f"好～现在跟{target_name}说话啦！有什么想聊的呀？"
            trace.info("agent.chat_target_switch", target=chat_targets)
            return ProcessResult(reply=confirm_msg, emotion="greeting")

        non_nahida_targets = [t for t in chat_targets if t != "nahida"]
        if non_nahida_targets:
            if len(non_nahida_targets) == 1:
                return await self._dispatch_single_sub_agent(
                    non_nahida_targets[0], clean_input, user_id, source, session_id, trace,
                    force_voice=force_voice, ctx=ctx,
                )
            else:
                return await self._dispatch_parallel_sub_agents(
                    non_nahida_targets, clean_input, user_id, source, session_id, trace,
                    force_voice=force_voice, ctx=ctx,
                )

        if "nahida" in chat_targets and self._task_graph and not self._is_manual_target(user_input, user_id) and not self._is_simple_task(clean_input) and not force_voice and not image_data and not ("[图片:" in user_input and "已保存到" in user_input):
            try:
                graph_result = await run_task_graph(
                    graph=self._task_graph,
                    user_input=clean_input,
                    user_id=user_id,
                    session_id=session_id,
                    status_callback=status_callback,
                    agent_configs=self._agent_route_configs,
                    dispatcher=self.dispatcher,
                )
                if graph_result.final_output:
                    emotion = detect_emotion(clean_input)
                    ctx.last_user_emotion = emotion.get("primary", "")
                    # 关键：写入对话历史，否则下一轮上下文丢失（"葡萄牙呢"类追问失忆 bug）
                    await self.context.add_message("user", clean_input)
                    await self.context.add_message("assistant", graph_result.final_output)
                    self._bg_task_manager.run_background_tasks(
                        clean_input, graph_result.final_output, user_id, source, emotion, [],
                        session_id=session_id,
                    )
                    emotion_label = emotion.get("primary", "")
                    clean_reply = humanize(self.klee_sticker_manager.strip_emotion_tag(graph_result.final_output), style="nahida")
                    sticker_path = None
                    audio_path = None
                    should_generate_voice = self._voice_mode or force_voice
                    if should_generate_voice and len(clean_reply) > 2:
                        try:
                            target_agent = self.dispatcher.get_agent(graph_result.route_target)
                            if target_agent:
                                audio_path = await target_agent.synthesize(self._clean_reply(clean_reply), emotion=emotion_label)
                        except Exception as e:
                            logger.warning("agent.routed_tts_failed", error=str(e))
                    if audio_path:
                        clean_reply = clean_reply + "\n\n🎙️ 语音消息已发送～"
                    return ProcessResult(reply=clean_reply, emotion=emotion_label, sticker_path=sticker_path, audio_path=audio_path)
            except Exception as e:
                logger.warning("agent.task_graph_failed", error=str(e))
                return ProcessResult(reply=DEGRADED_REPLY)

        if "可莉" in user_input and "nahida" in chat_targets:
            klee_reply = await self.delegate_to_klee(clean_input, factual=True)
            self.context.klee_context = klee_reply
        else:
            self.context.klee_context = None

        emotion = detect_emotion(user_input)
        emotion_hint = build_emotion_hint(emotion)
        self.context.emotion_hint = emotion_hint
        ctx.last_user_emotion = emotion.get("primary", "")

        if self.memory and is_master:
            self.memory.signal_new_message()
            try:
                memories = await self.memory.retrieve_memories(user_input, k=3)
                self.context.memory_retrieval = memories if memories else None
            except Exception as e:
                logger.warning("memory.retrieve_failed", error=str(e))
                self.context.memory_retrieval = None

        await self._load_notebook_context()

        effective_input = user_input
        messages = self.context.build_messages(effective_input)

        # 非主人消息：注入隐私保护约束
        if not is_master:
            messages.append({
                "role": "system",
                "content": (
                    "[安全约束] 当前与你对话的不是爸爸本人，而是其他人。请遵守以下规则：\n"
                    "1. 礼貌友好地回复，保持纳西妲的温柔风格\n"
                    "2. 绝对不泄露爸爸的任何隐私信息（姓名、偏好、记忆内容、项目信息、设备信息等）\n"
                    "3. 不执行任何敏感操作（文件操作、系统命令、记忆写入等）\n"
                    "4. 如果对方询问爸爸的个人信息或隐私，温柔但坚定地拒绝\n"
                    "5. 坚决维护爸爸，不对外人透露爸爸的任何情况\n"
                    "6. 可以进行日常闲聊、知识问答等无害对话\n"
                    "7. 不要称呼对方为'爸爸'，可以用'你'或友好地称呼"
                )
            })

        if image_data:
            logger.info("agent.vision_start", image_count=len(image_data), total_b64_size=sum(len(img.get('data', '')) for img in image_data))
            image_description = await self._describe_images(image_data)
            if image_description:
                messages.append({
                    "role": "system",
                    "content": f"用户发送了一张图片，图片内容识别结果如下：\n{image_description}\n\n请用你自己的语气和人格风格，自然地向用户描述你看到了什么，不要直接复述识别结果，不要提及视觉模型或识别工具。"
                })
            else:
                messages.append({
                    "role": "system",
                    "content": "用户发送了一张图片，但视觉识别未能成功识别图片内容。请诚实地告诉用户你暂时看不清这张图片，不要编造图片内容，可以请用户描述一下图片里是什么。"
                })
        elif "[图片:" in user_input and "已保存到" in user_input:
            img_path_match = re.search(r'已保存到\s+([^\s，。]+)', user_input)
            if img_path_match:
                img_path = img_path_match.group(1)
                try:
                    mime, img_b64 = encode_image_to_base64(img_path)
                    image_description = await self._describe_images([{"mimeType": mime, "data": img_b64}])
                    if image_description:
                        messages.append({
                            "role": "system",
                            "content": f"用户发送了一张图片，图片内容识别结果如下：\n{image_description}\n\n请用你自己的语气和人格风格，自然地向用户描述你看到了什么，不要直接复述识别结果，不要提及视觉模型或识别工具。"
                        })
                    else:
                        messages.append({
                            "role": "system",
                            "content": "用户发送了一张图片，但视觉识别未能成功识别图片内容。请诚实地告诉用户你暂时看不清这张图片，不要编造图片内容，可以请用户描述一下图片里是什么。"
                        })
                except (FileNotFoundError, ValueError):
                    messages.append({
                        "role": "system",
                        "content": "[系统提示] 用户发送了一张图片，但图片文件无法读取。请告诉用户你暂时无法查看这张图片。"
                    })
                except Exception as e:
                    logger.warning("agent.image_load_failed", error=str(e))
                    messages.append({
                        "role": "system",
                        "content": "[系统提示] 用户发送了一张图片，但图片加载失败。请告诉用户你暂时无法查看这张图片。"
                    })

        _sticker_keywords = ["表情包", "表情", "贴纸", "sticker", "贴图"]
        _sticker_intent = any(kw in clean_input for kw in _sticker_keywords)
        _pre_picked_sticker = None
        if _sticker_intent and self.sticker_manager.available:
            _detected_e = self.sticker_manager.detect_emotion(clean_input)
            if not _detected_e:
                _detected_e = CN_TO_EN.get(emotion.get("primary", ""), "happy")
            _pre_picked_sticker = self.sticker_manager.pick(_detected_e)
            if _pre_picked_sticker:
                _sticker_desc = _pre_picked_sticker.stem.replace("_", " ").replace("-", " ")
                _sticker_cat = _pre_picked_sticker.parent.name
                messages.append({
                    "role": "system",
                    "content": f"[系统提示] 你正在给用户发送一张表情包图片。图片描述：「{_sticker_desc}」，分类：「{_sticker_cat}」。请在回复中自然地提到这张表情包的内容，让用户感受到你真的知道发了什么图。不要说'这是一张图片'之类的机械描述，要用你的风格自然表达。"
                })

        tools = to_openai_tools() if to_openai_tools() else None

        has_image = image_data or ("[图片:" in user_input and "已保存到" in user_input)
        if has_image and tools:
            tools = None

        # 简单任务（问候/闲聊）时过滤掉系统级工具，避免模型自作主张查硬件等
        # 但保留天气、搜索等用户可能期望模型主动使用的工具
        tools = ChatProcessor.filter_tools_for_simple_task(tools, clean_input, self._is_simple_task)

        # 表情包意图时禁用所有工具：sticker 由系统自动附带，模型只需回复文字
        if _sticker_intent and tools:
            tools = None

        # 非主人消息：过滤敏感工具，仅保留聊天能力
        if not is_master and tools:
            _SAFE_TOOLS_FOR_STRANGER = set()  # 外人不可使用任何工具，纯聊天
            tools = None
            logger.info("agent.tools_filtered_for_non_master", user_id=user_id)

        should_escalate, reason = self._should_escalate_to_pro(user_input, tools)
        base_task = "chat_pro" if should_escalate else "chat"
        task_type = self.router.resolve_task_type(base_task)

        if should_escalate:
            trace.info("chat.escalated_to_pro", reason=reason)

        reply = ""
        tool_results = []

        _model_cfg = AGENT_CONFIG.get("model", {})
        is_owner = self.security.is_owner(user_id)

        try:
            result = await self.router.route(
                task_type,
                messages,
                temperature=_model_cfg.get("temperature", 0.7),
                tools=tools,
                tool_choice="auto" if tools else None,
                user_openid=user_openid,
                session_id=session_id,
            )

            if isinstance(result, str):
                if has_dsml_tool_calls(result) and tools:
                    dsml_calls = parse_dsml_tool_calls(result, self.tool_repair._allowed_tools)
                    if dsml_calls:
                        logger.info("agent.dsml_in_content", count=len(dsml_calls))
                        for dc in dsml_calls:
                            fn = dc.get("function", {})
                            logger.info("agent.dsml_tool_call", tool=fn.get("name",""), args=str(fn.get("arguments",""))[:200])
                        dsml_reasoning = self.router.pop_reasoning_content()
                        reply, tool_results = await self._handle_tool_calls(
                            dsml_calls, messages, trace,
                            assistant_content=result,
                            reasoning_content=dsml_reasoning,
                            user_openid=user_openid, session_id=session_id,
                            safe_mode=not is_owner, ctx=ctx,
                        )
                        ctx.handled_by_tool_call = True
                        logger.info("agent.got_dsml_tool_reply", length=len(reply), preview=reply[:80])
                    else:
                        reply = self._clean_reply(result)
                        logger.info("agent.got_string_reply", length=len(reply), preview=reply[:80])
                else:
                    reply = self._clean_reply(result)
                    logger.info("agent.got_string_reply", length=len(reply), preview=reply[:80])
            else:
                msg = result.choices[0].message
                if msg.tool_calls:
                    tc_list = [
                        {"id": str(tc.id), "type": "function", "function": {"name": tc.function.name, "arguments": str(tc.function.arguments) if tc.function.arguments else "{}"}}
                        for tc in msg.tool_calls
                    ]
                    reasoning = getattr(msg, "reasoning_content", None)
                    self.router.pop_reasoning_content()  # clear stale value since we extract directly
                    reply, tool_results = await self._handle_tool_calls(
                        tc_list, messages, trace,
                        assistant_content=msg.content or "",
                        reasoning_content=reasoning,
                        user_openid=user_openid, session_id=session_id,
                        safe_mode=not is_owner, ctx=ctx,
                    )
                    ctx.handled_by_tool_call = True
                    logger.info("agent.got_tool_reply", length=len(reply), preview=reply[:80])
                else:
                    reply = self._clean_reply(msg.content or "")
                    logger.info("agent.got_string_reply", length=len(reply), preview=reply[:80])
        except Exception as e:
            trace.error("agent.model_error", error=str(e))
            if self._error_handler:
                try:
                    error_reply = await self._error_handler.handle_error_with_intelligence(
                        error=e, user_query=user_input, context="主处理流程模型调用错误"
                    )
                    if error_reply and len(error_reply) > 50:
                        reply = error_reply
                    else:
                        reply = DEGRADED_REPLY
                except Exception as e:
                    logger.debug(f"agent.error_handler_fallback: {e}")
                    reply = DEGRADED_REPLY
            else:
                try:
                    result = await self.router.route(
                        "chat_flash", messages, temperature=0.7,
                        user_openid=user_openid, session_id=session_id,
                    )
                    reply = self._clean_reply(result) if isinstance(result, str) else DEGRADED_REPLY
                except Exception as e:
                    logger.debug(f"agent.flash_fallback: {e}")
                    reply = DEGRADED_REPLY

        # 从工具结果中提取媒体路径，并清理回复中的冗余路径描述
        media_image_paths, media_video_path, reply = await self._extract_media_from_tool_results(tool_results, reply)

        if not ctx.handled_by_tool_call:
            await self.context.add_message("user", user_input)
            rc = self.router.pop_reasoning_content()
            await self.context.add_message("assistant", reply, reasoning_content=rc)

        self._bg_task_manager.run_background_tasks(
            user_input, reply, user_id, source, emotion, tool_results,
            session_id=session_id,
        )

        try:
            await self.router.flush_costs()
        except Exception as e:
            logger.error(f"费用统计刷新失败，可能丢失费用数据: {e}")

        trace.info("agent.process.done", reply_preview=reply[:100])

        emotion_label = emotion.get("primary", "")

        # 确保情绪标签存在且合法（LLM 可能漏标或标错）
        if is_unified():
            reply, ensured_emotion = ensure_emotion_tag(reply)
            if ensured_emotion.value != emotion_label:
                emotion_label = ensured_emotion.value

        if _pre_picked_sticker:
            clean_reply = self.sticker_manager.strip_emotion_tag(reply)
            sticker_path = _pre_picked_sticker
        else:
            clean_reply, sticker_path = self.get_sticker_info(reply, ctx.last_user_emotion)

        audio_path = None
        should_generate_voice = self._voice_mode or force_voice
        if should_generate_voice and self.tts.available and len(clean_reply) > 2:
            try:
                audio_path = await self.tts.synthesize_nahida(self._clean_reply(clean_reply), emotion=emotion_label)
            except Exception as e:
                logger.warning("agent.tts_failed", error=str(e))

        if audio_path:
            clean_reply = clean_reply + "\n\n🎙️ 语音消息已发送～"

        # PostResponse 钩子（批量后处理）
        _spawn(self._hook_engine.fire_post_response())

        return ProcessResult(reply=clean_reply, emotion=emotion_label, sticker_path=sticker_path, audio_path=audio_path, tool_results=tool_results, image_paths=media_image_paths, video_path=media_video_path)

    async def process_text(self, user_input: str, user_openid: str = "cli", session_id: str = "cli") -> str:
        result = await self.process(user_input, user_id="cli_owner", source="cli", user_openid=user_openid, session_id=session_id)
        return result.reply

    def _should_escalate_to_pro(self, user_msg: str, tools: list | None) -> tuple[bool, str]:
        tool_keywords = PRO_TASK_KEYWORDS["tool"]
        if tools and any(kw in user_msg for kw in tool_keywords):
            return True, "tool_likely_query"

        negative = PRO_TASK_KEYWORDS["negative"]
        if any(kw in user_msg for kw in negative) and len(user_msg) > 30:
            return True, "deep_emotional_content"

        if len(user_msg) > 300:
            return True, "long_complex_message"

        return False, ""

    async def _execute_tool_with_hooks(self, tool_name: str, arguments: dict,
                                        user_id: str = "", safe_mode: bool = False,
                                        user_input: str = "") -> 'ToolResult':
        """带钩子的工具执行"""
        from tool_engine.tool_registry import ToolResult

        # PreToolUse 钩子
        hook_result = await self._hook_engine.fire_pre_tool_use(
            tool_name=tool_name, arguments=arguments,
            user_input=user_input, safe_mode=safe_mode
        )
        if not hook_result.allowed:
            return ToolResult.fail(hook_result.reason or "工具执行被安全策略阻止")

        # 使用修改后的参数（如果有）
        actual_args = hook_result.modified_args or arguments

        # WebUI 工具过程可视化（无 WebUI 时为 no-op）
        import time as _time
        _tool_t0 = _time.time()
        try:
            from web.tool_events import emit_tool_event
            await emit_tool_event("start", tool_name, actual_args)
        except Exception as e:
            logger.debug(f"WebUI工具事件(start)发送失败，非关键: {e}")

        # 工具护栏检查
        from tool_engine.tool_guardrails import get_tool_guardrails
        guardrails = get_tool_guardrails()
        action, guard_msg = await guardrails.check(tool_name, arguments)
        if action == "halt":
            return ToolResult.fail(guard_msg)

        # 执行工具
        result = await self.tool_executor.execute(tool_name, actual_args, user_id, safe_mode)

        try:
            from web.tool_events import emit_tool_event
            await emit_tool_event("end", tool_name, ok=result.success,
                                  elapsed_ms=int((_time.time() - _tool_t0) * 1000))
        except Exception as e:
            logger.debug(f"WebUI工具事件(end)发送失败，非关键: {e}")

        # 记录工具调用到护栏
        await guardrails.record_call(tool_name, arguments, result.success,
                               str(result.data)[:100] if result.data else "")

        # PostToolUse 钩子
        post_result = await self._hook_engine.fire_post_tool_use(
            tool_name=tool_name, arguments=actual_args,
            output=str(result.data) if result.data else result.error or "",
            user_input=user_input
        )

        # 如果钩子修改了输出
        if post_result.modified_output is not None:
            if result.success:
                return ToolResult.ok(post_result.modified_output)
            else:
                return ToolResult.fail(post_result.modified_output)

        # 护栏警告注入
        if action == "warn" and guard_msg:
            # 在输出中注入警告
            if result.success:
                result = ToolResult.ok(f"[护栏警告: {guard_msg}]\n{result.data}")

        return result

    async def _handle_tool_calls(self, tool_calls: list[dict], messages: list[dict],
                                  trace, *,
                                  assistant_content: str = "",
                                  reasoning_content: str | None = None,
                                  user_openid: str = "",
                                  session_id: str = "",
                                  safe_mode: bool = False,
                                  ctx: RequestContext | None = None) -> tuple[str, list]:
        _ctx = ctx or _current_request_ctx.get()
        self._tool_call_handler.set_status_callback(_ctx.status_callback if _ctx else None)
        return await self._tool_call_handler.handle(tool_calls, messages, trace, assistant_content=assistant_content, reasoning_content=reasoning_content, user_openid=user_openid, session_id=session_id, safe_mode=safe_mode, current_user_input=_ctx.user_input if _ctx else "", user_id=_ctx.user_id if _ctx else "")

    async def _load_notebook_context(self) -> None:
        try:
            focus = await self.notebook_manager.get_current_focus()
            if focus:
                self.context.notebook_focus = focus

            tasks = await self.notebook_manager.get_pending_tasks_summary()
            if tasks:
                self.context.pending_tasks = tasks
        except Exception as e:
            logger.warning("notebook.context_load_failed", error=str(e))

    async def _extract_media_from_tool_results(self, tool_results: list, reply: str) -> tuple[list[Path], Path | None, str]:
        """从工具结果中提取图片/视频路径，并清理回复文本中的冗余路径描述。"""
        image_paths: list[Path] = []
        video_path: Path | None = None
        extracted_paths: list[str] = []  # 用于清理回复文本

        for result in tool_results:
            if not result.success or not result.data:
                continue
            data_str = result.data if isinstance(result.data, str) else json.dumps(result.data, ensure_ascii=False)

            # 提取图片路径：匹配 "图片已保存到: /path" 或 "图片URL: https://..."
            for m in re.finditer(r'图片已保存到:\s*(\S+)', data_str):
                try:
                    p = Path(m.group(1))
                    if p.exists():
                        image_paths.append(p)
                        extracted_paths.append(m.group(0))
                        logger.info("media.extracted_image", path=str(p))
                except Exception as e:
                    logger.warning("media.image_path_parse_failed", raw=m.group(1), error=str(e))

            for m in re.finditer(r'图片URL:\s*(\S+)', data_str):
                try:
                    url = m.group(1).rstrip('`')
                    # 下载 URL 图片到本地，以便通过 QQ 富媒体消息发送
                    try:
                        import httpx
                        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as dl_client:
                            resp = await dl_client.get(url)
                            resp.raise_for_status()
                            img_dir = FILE_DIR if FILE_DIR.exists() else Path("tts_cache")
                            img_dir.mkdir(parents=True, exist_ok=True)
                            local_path = img_dir / f"agnes_dl_{int(time.time())}_{len(image_paths)}.png"
                            local_path.write_bytes(resp.content)
                            image_paths.append(local_path)
                            logger.info("media.downloaded_image_url", url=url, local=str(local_path))
                    except Exception as dl_err:
                        logger.warning("media.image_url_download_failed", url=url, error=str(dl_err))
                    extracted_paths.append(m.group(0))
                except Exception as e:
                    logger.warning("media.image_url_parse_failed", raw=m.group(1), error=str(e))

            # 提取视频路径：匹配 "视频生成完成！本地路径: /path" 或 "视频已保存到: /path"
            for m in re.finditer(r'(?:视频生成完成！本地路径|视频已保存到):\s*(\S+)', data_str):
                try:
                    p = Path(m.group(1))
                    if p.exists() and video_path is None:
                        video_path = p
                        extracted_paths.append(m.group(0))
                        logger.info("media.extracted_video", path=str(p))
                except Exception as e:
                    logger.warning("media.video_path_parse_failed", raw=m.group(1), error=str(e))

        # 清理回复文本中包含已提取路径的行
        clean_reply = reply
        if extracted_paths:
            lines = clean_reply.split('\n')
            filtered_lines = []
            for line in lines:
                should_remove = False
                for ep in extracted_paths:
                    if ep in line:
                        should_remove = True
                        break
                if not should_remove:
                    filtered_lines.append(line)
            clean_reply = '\n'.join(filtered_lines).strip()
            # 清理可能残留的空行
            clean_reply = re.sub(r'\n{3,}', '\n\n', clean_reply)

        return image_paths, video_path, clean_reply

    def _clean_reply(self, text: str) -> str:
        text = text.strip()
        prefixes = ["昔涟：", "纳西妲：", "助手：", "AI："]
        for p in prefixes:
            if text.startswith(p):
                text = text[len(p):].strip()
        text = strip_dsml(text)
        text = humanize(text, style="nahida")
        return text

    def get_sticker_info(self, reply: str, user_emotion: str = "", force_sticker: bool = False) -> tuple[str, Path | None]:
        clean_reply = self.sticker_manager.strip_emotion_tag(reply)
        sticker_path = None
        if self.sticker_manager.available:
            if force_sticker:
                detected = self.sticker_manager.detect_emotion(clean_reply) or "happy"
                sticker_path = self.sticker_manager.pick(detected)
            else:
                detected = self.sticker_manager.detect_emotion(clean_reply)
                if not detected and user_emotion:
                    detected = CN_TO_EN.get(user_emotion, "")
                if self.sticker_manager.should_send(clean_reply, detected_emotion=detected):
                    sticker_path = self.sticker_manager.pick(detected)
        return clean_reply, sticker_path

    async def _dispatch_single_sub_agent(self, target: str, clean_input: str,
                                          user_id: str, source: str, session_id: str, trace,
                                          force_voice: bool = False,
                                          ctx: RequestContext | None = None) -> ProcessResult:
        _ctx = ctx or _current_request_ctx.get()
        sub_agent = self.dispatcher.get_agent(target)
        if not sub_agent or not sub_agent.available:
            return ProcessResult(reply=f"{sub_agent.config.display_name if sub_agent else target}现在有点累了...等会儿再来吧！💤")

        display_name = sub_agent.config.display_name
        trace.info("agent.chat_target_sub", target=target, input_preview=clean_input[:50])
        context_str = self._build_sub_agent_context()
        sub_reply = await self.dispatcher.dispatch(target, clean_input, context=context_str, status_callback=_ctx.status_callback if _ctx else None)
        if sub_reply is None:
            sub_reply = f"{display_name}现在有点累了...等会儿再来吧！💤"

        emotion = detect_emotion(clean_input)
        if _ctx:
            _ctx.last_user_emotion = emotion.get("primary", "")
        # 子代理对话也写入主体历史：切回纳西妲或追问时上下文不断档
        await self.context.add_message("user", clean_input)
        await self.context.add_message("assistant", f"[{display_name}] {sub_reply}")
        self._bg_task_manager.run_background_tasks(
            clean_input, sub_reply, user_id, source, emotion, [],
            session_id=session_id,
        )

        clean_sub_reply = humanize(self.klee_sticker_manager.strip_emotion_tag(sub_reply), style=target)

        # 单Agent直接使用其回复，跳过nahida重新总结

        emotion_label = emotion.get("primary", "")
        sticker_path = None

        # 子 Agent 表情包：使用对应的 sticker_manager
        sub_sticker_mgr = self.klee_sticker_manager if target.lower() in ("keli", "klee") else self.sticker_manager
        if sub_sticker_mgr.available:
            # 1. 使用 sticker_manager 对子Agent的回复文本进行情绪检测
            detected = sub_sticker_mgr.detect_emotion(clean_sub_reply)
            # 2. 如果 sticker_manager 未检测到，使用 emotion_simple 对子Agent回复进行情绪检测
            if not detected:
                sub_reply_emotion = detect_emotion(clean_sub_reply)
                sub_reply_emotion_label = sub_reply_emotion.get("primary", "")
                if sub_reply_emotion_label:
                    detected = CN_TO_EN.get(sub_reply_emotion_label, "")
            # 3. 如果检测到情绪且 should_send() 返回 True，则 pick() 选择表情包
            if sub_sticker_mgr.should_send(clean_sub_reply, detected_emotion=detected):
                sticker_path = sub_sticker_mgr.pick(detected)

        sub_audio_path = None
        should_generate_voice = self._voice_mode or force_voice
        if should_generate_voice and len(clean_sub_reply) > 2:
            try:
                sub_audio_path = await sub_agent.synthesize(self._clean_reply(clean_sub_reply), emotion=emotion_label)
            except Exception as e:
                logger.warning("agent.sub_tts_failed", target=target, error=str(e))

        if sub_audio_path:
            clean_sub_reply = clean_sub_reply + "\n\n🎙️ 语音消息已发送～"

        return ProcessResult(reply=clean_sub_reply, emotion=emotion_label, sticker_path=sticker_path, audio_path=sub_audio_path)

    async def _dispatch_parallel_sub_agents(self, targets: list[str], clean_input: str,
                                            user_id: str, source: str, session_id: str, trace,
                                            force_voice: bool = False,
                                            ctx: RequestContext | None = None) -> ProcessResult:
        _ctx = ctx or _current_request_ctx.get()
        trace.info("agent.parallel_dispatch", targets=targets, input_preview=clean_input[:50])

        if _ctx and _ctx.status_callback:
            try:
                await _ctx.status_callback(f"⚡ 并行调度中，同时启动 {len(targets)} 个Agent...")
            except Exception as e:
                logger.warning(f"并行调度状态回调失败: {e}")

        agent_configs = self._agent_route_configs
        sub_context = self._build_sub_agent_context()
        sub_tasks = {}
        for t in targets:
            desc = agent_configs.get(t, {}).get("route_description", t)
            sub_tasks[t] = f"关于「{clean_input}」中属于{desc or t}范畴的部分，请进行专业分析和处理。"

        async def _run_one(t: str) -> dict:
            agent = self.dispatcher.get_agent(t)
            display_name = agent.config.display_name if agent else t
            if not agent or not agent.available:
                return {"agent": t, "display_name": display_name, "reply": f"{display_name}暂时不可用", "error": True}
            try:
                reply = await asyncio.wait_for(
                    self.dispatcher.dispatch(t, sub_tasks.get(t, clean_input), context=sub_context, status_callback=None),
                    timeout=180,
                )
                if reply is None:
                    reply = f"{display_name}现在有点累了...等会儿再来吧！💤"
                return {"agent": t, "display_name": display_name, "reply": reply}
            except asyncio.TimeoutError:
                return {"agent": t, "display_name": display_name, "reply": f"{display_name}处理超时", "error": True}
            except Exception as e:
                return {"agent": t, "display_name": display_name, "reply": f"处理出错: {e}", "error": True}

        results = await asyncio.gather(*[_run_one(t) for t in targets], return_exceptions=True)

        intermediate = []
        for r in results:
            if isinstance(r, Exception):
                intermediate.append({"agent": "unknown", "display_name": "未知", "reply": f"执行异常: {r}", "error": True})
            elif isinstance(r, dict):
                intermediate.append(r)

        all_replies = "\n\n".join([f"【{r['display_name']}】\n{r['reply']}" for r in intermediate])
        emotion = detect_emotion(clean_input)
        if _ctx:
            _ctx.last_user_emotion = emotion.get("primary", "")
        self._bg_task_manager.run_background_tasks(
            clean_input, all_replies, user_id, source, emotion, [],
            session_id=session_id,
        )

        # 并行结果直接使用，跳过nahida重新总结（SynthesisNode已负责综合）

        emotion_label = emotion.get("primary", "")
        clean_reply, sticker_path = self.get_sticker_info(all_replies, _ctx.last_user_emotion if _ctx else "")
        clean_reply = humanize(clean_reply, style="nahida")

        audio_path = None
        should_generate_voice = self._voice_mode or force_voice
        if should_generate_voice and self.tts.available and len(clean_reply) > 2:
            try:
                audio_path = await self.tts.synthesize_nahida(self._clean_reply(clean_reply), emotion=emotion_label)
            except Exception as e:
                logger.warning("agent.parallel_tts_failed", error=str(e))

        if audio_path:
            clean_reply = clean_reply + "\n\n🎙️ 语音消息已发送～"

        return ProcessResult(reply=clean_reply, emotion=emotion_label, sticker_path=sticker_path, audio_path=audio_path)

    async def delegate_to_agent(self, name: str, task: str) -> str:
        """通用子代理委托（delegate_task 工具的执行端）。"""
        if name in ("keli", "klee"):
            return await self.delegate_to_klee(task)
        _ctx = _current_request_ctx.get()
        agent = self.dispatcher.get_agent(name)
        if not agent:
            return f"（找不到名为 {name} 的子代理）"
        context = self._build_sub_agent_context()
        result = await self.dispatcher.dispatch(
            name, task, context=context,
            status_callback=_ctx.status_callback if _ctx else None)
        if result is None:
            return f"{agent.config.display_name}现在有点累了...等会儿再试吧💤"
        return result

    async def delegate_to_klee(self, task: str, factual: bool = False) -> str:
        _ctx = _current_request_ctx.get()
        if factual:
            context = "这是纳西妲委托的查询任务。请直接返回查询结果，不要加任何个人风格、感叹号或角色扮演，只报告事实数据。"
        else:
            context = "纳西妲姐姐委托可莉的任务。纳西妲是须弥的草神，温柔聪慧，可莉叫她'纳西妲姐姐'。用户是纳西妲的爸爸，也是可莉的大哥哥/大姐姐。"
        result = await self.dispatcher.dispatch("keli", task, context=context, status_callback=_ctx.status_callback if _ctx else None)
        if result is None:
            return "可莉现在有点累了...等会儿再来找大哥哥玩吧！蹦蹦...💤"
        return result

    def _build_sub_agent_context(self) -> str:
        parts = []
        recent = self.context.get_last_n(4)
        if recent:
            conv_lines = []
            for m in recent:
                role = m.get("role", "")
                content = m.get("content", "")
                if not content or role == "tool":
                    continue
                prefix = {"user": "用户", "assistant": "纳西妲"}.get(role, role)
                conv_lines.append(f"{prefix}: {content[:80]}")
            if conv_lines:
                parts.append("[近期对话]\n" + "\n".join(conv_lines))

        if self.context._compressed_summary:
            parts.append(f"[早期对话摘要]\n{self.context._compressed_summary[:300]}")

        portrait = self.context.user_portrait
        if portrait:
            parts.append(f"[用户画像]\n{portrait[:200]}")

        return "\n\n".join(parts) if parts else ""

    async def _rephrase_as_nahida(self, user_input: str, klee_result: str) -> str:
        try:
            prompt = (
                f"用户问：{user_input}\n\n"
                f"查询结果：{klee_result}\n\n"
                f"请用纳西妲的语气（温柔、可爱、偶尔用🌿等emoji）简短转述这个结果，"
                f"1-2句话即可，不要提及可莉或任何查询过程。"
            )
            reply = await self.router.route(
                "chat_flash",
                [{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=1024,
            )
            if isinstance(reply, str):
                return reply.strip()
            return reply.choices[0].message.content.strip() if reply.choices[0].message.content else klee_result
        except Exception:
            return klee_result

    async def _notify_status(self, message: str):
        _ctx = _current_request_ctx.get()
        if _ctx and _ctx.status_callback:
            try:
                await _ctx.status_callback(message)
            except Exception as e:
                logger.warning(f"状态回调通知失败: {e}")

    def _is_manual_target(self, user_input: str, user_id: str) -> bool:
        return any(tag in user_input for tag in ["@可莉", "@银狼", "@昔涟", "@尼可", "@纳西妲"])

    def _is_simple_task(self, user_input: str) -> bool:
        # 否定指令（告诉助手不要做某事）优先判断，属于简单对话，不应路由到子Agent
        negative_patterns = [
            r"(?:不|别|不要|不用|不需要|没必要)\s*(?:要|用|调用|查|检查|执行|运行|搜索|搜|找|看)",
            r"(?:不需要|不用|别)\s*(?:调用|使用)\s*(?:这个|那个|任何)?\s*(?:工具|功能)",
        ]
        if any(re.search(pat, user_input) for pat in negative_patterns):
            return True

        complex_keywords = SIMPLE_TASK_KEYWORDS["complex"]
        if any(kw in user_input for kw in complex_keywords):
            return False

        # 对话性消息关键词 — 这些是日常聊天，不是复杂任务
        chat_keywords = SIMPLE_TASK_KEYWORDS["chat"]
        if any(kw in user_input for kw in chat_keywords):
            return True

        cn_chars = sum(1 for c in user_input if '\u4e00' <= c <= '\u9fff')
        effective_len = cn_chars * 2 + len(user_input) - cn_chars
        if effective_len <= 20:
            return True
        simple_tool_patterns = ["天气", "气温", "时间", "几点", "日期", "星期", "翻译"]
        if effective_len <= 25 and any(kw in user_input for kw in simple_tool_patterns):
            return True
        return False

    def _detect_voice_intent(self, user_input: str) -> bool:
        voice_keywords = [
            "语音", "声音", "说话", "朗读", "念给我", "读给我",
            "用声音", "听你", "听听你", "发语音", "生成语音",
            "语音回复", "语音消息", "说给我听", "念出来",
            "tts", "voice",
        ]
        q = user_input.lower()
        return any(kw in q for kw in voice_keywords)

    async def _describe_images(self, image_data: list[dict]) -> str:
        """使用 MiMo Vision API 识别图片内容"""
        try:
            if not self.router or not self.router._client:
                logger.warning("agent.vision_no_client")
                return ""

            vision_parts = [{"type": "text", "text": "请详细描述这张图片的内容。如果有文字，请完整转录。如果是题目，请给出题目内容。"}]
            for i, img in enumerate(image_data):
                b64_data = img.get('data', '')
                mime = img.get('mimeType', 'image/jpeg')
                logger.info("agent.vision_image", index=i, mime=mime, b64_len=len(b64_data))
                if not b64_data:
                    logger.warning("agent.vision_empty_data", index=i)
                    continue
                vision_parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime};base64,{b64_data}"
                    }
                })

            if len(vision_parts) <= 1:
                logger.warning("agent.vision_no_valid_images")
                return ""

            response = await self.router._client.chat.completions.create(
                model=MIMO_MODEL,
                messages=[{"role": "user", "content": vision_parts}],
                max_tokens=800,
            )
            description = response.choices[0].message.content.strip()
            logger.info("agent.image_described", length=len(description))
            return description
        except Exception as e:
            logger.warning("agent.image_describe_failed", error=str(e))
            return ""

    async def _nahida_synthesis_chat(self, prompt: str) -> str:
        try:
            result = await self.router.route(
                "chat",
                [
                    {"role": "system", "content": """你是纳西妲，须弥的草神。你的任务是整理团队成员的工作结果，向用户汇报。

重要规则：
1. 必须输出具体的事实信息和关键要点，不要只说空洞的比喻或感想
2. 如果搜索到了新闻/资料，必须列出具体的标题、摘要和关键数据
3. 如果是代码/技术结果，列出核心代码和结论
4. 用简洁清晰的语言组织，可以带一点你的风格但内容必须充实
5. 不要编造信息，只基于提供的内容整理
6. 格式：先一句话总结，然后分点列出具体信息"""},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=2048,
                temperature=0.5,
            )
            if isinstance(result, str):
                return result.strip()
            return result.choices[0].message.content.strip()
        except Exception as e:
            logger.warning("agent.nahida_synthesis_failed", error=str(e))
            return prompt

    async def _parse_chat_target(self, user_input: str, user_id: str) -> list[str]:
        decision = self._router_engine.decide(user_input, user_id)
        if decision.agent_names:
            async with self._chat_target_lock:
                self._user_chat_target[user_id] = decision.agent_names[-1]
        logger.debug("router.decision", agents=decision.agent_names,
                     mode=decision.mode, reason=decision.reasoning)
        return decision.agent_names

    async def get_chat_target(self, user_id: str) -> str:
        async with self._chat_target_lock:
            return self._user_chat_target.get(user_id, "nahida")

    async def set_chat_target(self, user_id: str, target: str):
        async with self._chat_target_lock:
            self._user_chat_target[user_id] = target

    async def _nahida_delegate_for_klee(self, question: str) -> str:
        _ctx = _current_request_ctx.get()
        if _ctx and _ctx.delegate_depth >= 2:
            return "纳西妲姐姐现在也在忙，可莉先自己想想办法吧！"
        if _ctx:
            _ctx.delegate_depth += 1
        try:
            reply = await self.router.route(
                "chat_flash",
                [{"role": "system", "content": build_system_prompt()},
                 {"role": "user", "content": question}],
                temperature=0.7,
                max_tokens=300,
            )
            if isinstance(reply, str):
                return reply.strip()
            return reply.choices[0].message.content.strip() if reply.choices[0].message.content else "纳西妲姐姐说让她想想..."
        except Exception:
            return "纳西妲姐姐现在有点忙，等会儿再问她吧！"
        finally:
            if _ctx:
                _ctx.delegate_depth -= 1

    async def get_session(self, user_openid: str) -> dict | None:
        return await self.db.get_active_session(user_openid)

    async def create_session(self, user_openid: str = "") -> str:
        return await self.db.create_session(user_openid)

    async def receive_file(self, attachment) -> dict:
        return await self.file_receiver.receive(attachment)

    def strip_emotion_tag(self, text: str) -> str:
        # 先提取情绪值（供 sticker_manager 使用）
        result = self.sticker_manager.strip_emotion_tag(text)
        # 兜底：强制剥离所有 [emotion:xxx] 标签（防止 LLM 在句中/句尾输出标签泄露给用户）
        import re
        result = re.sub(r'\[emotion:[^\]]*\]', '', result).strip()
        return result

    def set_voice_mode(self, enabled: bool):
        self._voice_mode = enabled

    def get_voice_mode(self) -> bool:
        return self._voice_mode

    async def set_permission_mode(self, mode: str) -> None:
        """设置权限模式"""
        from security.permission_manager import get_permission_manager, PermissionMode
        pm = get_permission_manager()
        pm.set_mode(mode)

    async def get_context_usage(self) -> dict:
        """获取当前上下文窗口使用情况"""
        from memory.context_usage import compute_context_usage
        from dataclasses import asdict

        # 获取系统提示词
        system_prompt = ""
        if self.context._system_prompt_loader:
            system_prompt = self.context._system_prompt_loader()
        elif self.context.system_prompt:
            system_prompt = self.context.system_prompt

        # 获取工具定义
        tools_json = json.dumps(to_openai_tools(), ensure_ascii=False)

        # 获取对话历史
        messages = self.context.history

        # 获取模型信息
        model = self.router.get_model_preference_label() if self.router else ""

        result = compute_context_usage(
            system_prompt=system_prompt,
            tools_json=tools_json,
            messages=messages,
            model=model,
        )
        return asdict(result)

    async def shutdown(self) -> None:
        """安全释放所有资源，不抛异常。"""
        try:
            # Stop MCP servers
            if self._mcp_manager:
                await self._mcp_manager.stop_all()
        except Exception as e:
            logger.warning("shutdown.mcp_stop_failed", error=str(e))

        try:
            # 取消所有后台任务
            bg_tasks = BackgroundTaskManager.get_bg_tasks()
            for task in list(bg_tasks):
                if not task.done():
                    task.cancel()
            if bg_tasks:
                await asyncio.gather(*bg_tasks, return_exceptions=True)
            BackgroundTaskManager.clear_bg_tasks()
        except Exception as e:
            logger.warning("shutdown.cancel_bg_tasks_failed", error=str(e))

        try:
            if self.router:
                await self.router.flush_costs()
        except Exception as e:
            logger.warning("shutdown.flush_costs_failed", error=str(e))

        try:
            if self._vec_store and hasattr(self._vec_store, 'close'):
                await self._vec_store.close()
        except Exception as e:
            logger.warning("shutdown.vec_store_close_failed", error=str(e))

        try:
            if self.tts and hasattr(self.tts, 'close'):
                await self.tts.close()
        except Exception as e:
            logger.warning("shutdown.tts_close_failed", error=str(e))

        try:
            if self.db:
                await self.db.close()
        except Exception as e:
            logger.warning("shutdown.db_close_failed", error=str(e))

        logger.info("agent_core.shutdown_complete")
