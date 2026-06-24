"""AgentCore 核心模块 —— 拆分自原 agent_core.py。

定义 AgentCore 基类（组合各 Mixin）以及模块级常量、dataclass 与辅助函数。
模块级常量（DEGRADED_REPLY / _current_request_ctx / ProcessResult / RequestContext /
UserIdentity）必须在本文件导入各 Mixin 之前定义，以避免循环导入。
"""
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

from loguru import logger

from config import (MIMO_MODEL, AGENT_CONFIG, WORKSPACE_DIR, STICKER_DIR, KLEE_STICKER_DIR, FILE_DIR,
                    build_system_prompt, build_safe_system_prompt, SIMPLE_TASK_KEYWORDS,
                    PRO_TASK_KEYWORDS, TTS_ASYNC_MODE, SIMPLE_CHAT_FASTPATH)
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
from utils.text_utils import (strip_dsml, has_dsml_tool_calls, parse_dsml_tool_calls,
                              humanize, encode_image_to_base64)
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

from core.background_tasks import BackgroundTaskManager, _spawn, _bg_tasks
from core.bootstrap import AgentCoreBootstrapper
from core.router_engine import RouterEngine, RoutingDecision
from core.chat_processor import ChatProcessor
from core.tool_orchestrator import ToolOrchestrator
from core.circuit_breaker import CognitiveState, CircuitBreaker, CircuitState
from core.failure_trigger import FailureTrigger
from utils.smart_error_handler import SmartErrorHandler


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
    # Task 6: TTS 异步化标记。为 True 时 audio_path 为空，需由调用方在后台合成并推送
    tts_pending: bool = False
    tts_text: str = ""


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
    identity: Any = None  # UserIdentity 运行时身份解析结果


@dataclass
class UserIdentity:
    """运行时用户身份解析结果。基于 openID/UID 稳定标识，不依赖消息内容。"""
    is_owner: bool
    display_name: str
    address_term: str  # 称谓：主人→"爸爸"，其他→"用户"

    @staticmethod
    def default_owner() -> "UserIdentity":
        return UserIdentity(is_owner=True, display_name="爸爸", address_term="爸爸")

    @staticmethod
    def default_guest() -> "UserIdentity":
        return UserIdentity(is_owner=False, display_name="用户", address_term="用户")


# 重要：模块级常量与 dataclass 必须在导入各 Mixin 之前定义完毕，
# 否则 Mixin 文件中 `from agent_core.core import ...` 会因名称尚未定义而失败（循环导入）。
from agent_core.message_processor import MessageProcessorMixin
from agent_core.tool_executor import ToolExecutorMixin
from agent_core.sub_agent_manager import SubAgentManagerMixin


class AgentCore(MessageProcessorMixin, ToolExecutorMixin, SubAgentManagerMixin):
    def __init__(self):
        self.router = ModelRouter()
        self.db = DatabaseManager()
        _owner_ids = os.getenv("OWNER_IDS", "").split(",")
        _owner_ids = [x.strip() for x in _owner_ids if x.strip()]
        # 合并 MASTER_QQ_OPENID（QQ 场景下的主人标识，由 qq_bot_adapter 自动绑定）
        # 确保 _resolve_identity 能正确识别主人，即使 OWNER_IDS 未配置
        _master_qq = os.getenv("MASTER_QQ_OPENID", "").split(",")
        _master_qq = [x.strip() for x in _master_qq if x.strip()]
        _owner_ids = list(dict.fromkeys(_owner_ids + _master_qq))  # 去重保序
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
        self._tool_call_handler = ToolCallHandler(self.tool_executor, self.tool_repair, self._clean_reply, self.context, self.router, klee_delegate=self.delegate_to_klee, agent_name="nahida", personality_file=str(Path(__file__).parent.parent / "config" / "agents" / "nahida_personality.md"), tool_execute_callback=self._execute_tool_with_hooks)
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
        self._cognitive_state = CognitiveState()
        self._circuit_breaker = CircuitBreaker()
        # 启用 SmartErrorHandler + FailureTrigger（失败触发器与反思闭环）
        self._smart_error_handler = SmartErrorHandler(db=self.db, dispatcher=self.dispatcher)
        self._failure_trigger = FailureTrigger(
            memory_db=self.memory.memory if self.memory else None,
            learning_manager=self._smart_error_handler,
        )
        # 将失败触发器注入钩子引擎，供 fire_post_tool_use_failure 使用
        self._hook_engine._failure_trigger = self._failure_trigger

    @property
    def hook_engine(self):
        return self._hook_engine

    async def init(self, reinit: bool = False) -> None:
        bootstrapper = AgentCoreBootstrapper(self)
        await bootstrapper.bootstrap(reinit=reinit)

    def _resolve_identity(self, user_id: str, user_openid: str = "",
                          source: str = "") -> UserIdentity:
        """运行时身份解析：基于 openID/UID 稳定标识判断用户身份，不依赖消息内容。

        身份判定规则：
        - QQ 群聊（source == "qq_group"）：严格按 owner_ids 判断，区分主人/非主人
        - 其他来源（web、cli、qq_c2c 等）：默认主人，使用完整提示词
        """
        # 非 QQ 群聊场景默认主人（webui/cli/单聊等均为爸爸本人使用）
        if source != "qq_group":
            return UserIdentity.default_owner()
        # QQ 群聊场景：基于 openID 严格判断
        check_id = user_openid or user_id
        if not check_id:
            return UserIdentity.default_owner()
        is_owner = self.security.is_owner(check_id)
        if is_owner:
            return UserIdentity(is_owner=True, display_name="爸爸", address_term="爸爸")
        return UserIdentity(is_owner=False, display_name="用户", address_term="用户")

    async def process(self, user_input: str, user_id: str = "qq_user",
                      source: str = "qq",
                      user_openid: str = "",
                      session_id: str = "",
                      status_callback=None,
                      image_data: list[dict] | None = None,
                      is_master: bool = True) -> ProcessResult:
        if not self._initialized:
            return ProcessResult(reply=DEGRADED_REPLY)

        # 运行时身份解析：基于稳定标识决定称谓，不依赖消息内容
        identity = self._resolve_identity(user_id, user_openid, source=source)
        # 用身份解析结果覆盖 is_master（更准确，兼容旧调用方仍传 is_master）
        is_master = identity.is_owner
        # 设置上下文的动态称谓
        self.context.current_address_term = identity.address_term

        ctx = RequestContext(
            session_id=session_id,
            user_openid=user_openid,
            user_id=user_id,
            user_input=user_input,
            status_callback=status_callback,
            is_master=is_master,
        )
        ctx.identity = identity
        _ctx_token = _current_request_ctx.set(ctx)
        try:
            return await self._process_impl(ctx, user_input, user_id, source, user_openid, session_id, status_callback, image_data, is_master)
        finally:
            _current_request_ctx.reset(_ctx_token)

    async def process_text(self, user_input: str, user_openid: str = "cli", session_id: str = "cli") -> str:
        result = await self.process(user_input, user_id="cli_owner", source="cli", user_openid=user_openid, session_id=session_id)
        return result.reply

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
