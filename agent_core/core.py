"""AgentCore 核心模块 —— 拆分自原 agent_core.py。

定义 AgentCore 基类（组合各 Mixin）以及模块级常量、dataclass 与辅助函数。
模块级常量（DEGRADED_REPLY / _current_request_ctx / ProcessResult / RequestContext /
UserIdentity）已抽取到 agent_core._shared, 由各子模块共享导入, 避免循环导入。
本模块通过 re-export 保持向后兼容 (from agent_core.core import ProcessResult 仍可用).
"""
from __future__ import annotations

import os
import asyncio
import json
import re
from pathlib import Path
from typing import Any, TYPE_CHECKING

from loguru import logger

# 共享常量与数据类型 — 从 _shared 导入并 re-export, 保持向后兼容
from agent_core._shared import (
    DEGRADED_REPLY,
    _current_request_ctx,
    ProcessResult,
    RequestContext,
    UserIdentity,
)

from config import (AGENT_CONFIG, WORKSPACE_DIR, STICKER_DIR, XIAOLI_STICKER_DIR, FILE_DIR,
                    build_system_prompt, build_safe_system_prompt, SIMPLE_TASK_KEYWORDS,
                    PRO_TASK_KEYWORDS, TTS_ASYNC_MODE, SIMPLE_CHAT_FASTPATH)
from model_router import ModelRouter
from agent_context import AgentContext
from agent_core.shared_blackboard import SharedBlackboard
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
from core.lazy_loader import LazyLoader
from tool_engine.tool_call_handler import ToolCallHandler
from xiaoli_agent import XiaoliAgent
from emotion.tts_engine import TTSEngine
from agent_dispatcher import AgentDispatcher
from tool_engine.mcp_client import MCPManager
from utils.credential_pool import get_credential_pool
from utils.error_classifier import ErrorClassifier
from hooks import get_hook_engine

# 内置工具改为懒注册：仅登记元数据（name/description/schema/permission/category），
# 不 import tools.* 子模块，避免冷启动把 httpx/selenium/PIL/primp 等重依赖拉进进程。
# 首次工具调用时由 tool_engine.tool_executor 经 resolve_tool_func 按需 import 实现。
from tool_engine.tool_registry import register_builtin_tools_lazy
register_builtin_tools_lazy()

if TYPE_CHECKING:
    from task_orchestrator import TaskGraph
    from instinct_manager import InstinctManager

from core.background_tasks import BackgroundTaskManager, _bg_tasks
from core.bootstrap import AgentCoreBootstrapper
from core.router_engine import RouterEngine
from core.chat_processor import ChatProcessor
from core.tool_orchestrator import ToolOrchestrator
from core.circuit_breaker import CognitiveState, CircuitBreaker
from core.failure_trigger import FailureTrigger
from utils.smart_error_handler import SmartErrorHandler


# 各 Mixin 从 agent_core._shared 导入共享类型, 不再依赖 agent_core.core 完成初始化,
# 因此可以安全导入 Mixin (不再有循环导入风险).
from agent_core.message_processor import MessageProcessorMixin
from agent_core.tool_executor import ToolExecutorMixin
from agent_core.sub_agent_manager import SubAgentManagerMixin


class AgentCore(MessageProcessorMixin, ToolExecutorMixin, SubAgentManagerMixin):
    def __init__(self) -> None:
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
        # 子代理 A2A 共享黑板（在 context 创建前初始化，并注入 context 供子代理访问）
        self._shared_blackboard = SharedBlackboard()
        # 后台周期清理任务引用（bootstrap 中创建，shutdown 中取消）
        self._shared_blackboard_cleanup_task = None
        self.context = AgentContext(system_prompt_loader=build_system_prompt, router=self.router, security_filter=self.security)
        self.context.shared_blackboard = self._shared_blackboard
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
        # LazyLoader: 延迟初始化重量级组件，首次访问时才加载
        # 冷启动优化：sticker 扫描目录和 file_receiver 初始化推迟到实际使用时
        self.sticker_manager = LazyLoader("emotion.sticker_manager.StickerManager", {"sticker_dir": STICKER_DIR})
        self.xiaoli_sticker_manager = LazyLoader("emotion.sticker_manager.StickerManager", {"sticker_dir": XIAOLI_STICKER_DIR})
        self.file_receiver = LazyLoader("utils.file_receiver.FileReceiver", {"base_dir": FILE_DIR})
        self.xiaoli = XiaoliAgent(tool_executor=self.tool_executor, tool_repair=self.tool_repair, xiaoda_delegate=self._xiaoda_delegate_for_xiaoli)
        self.tts = TTSEngine()
        self.dispatcher = AgentDispatcher(
            tts=self.tts,
            tool_executor=self.tool_executor,
            tool_repair=self.tool_repair,
            delegate_callback=self._xiaoda_delegate_for_xiaoli,
            core=self,
        )
        self._task_graph: TaskGraph | None = None
        self._agent_route_configs: dict = {}
        self._sticker_managers: dict = {}  # name → StickerManager (动态缓存)
        self._tool_call_handler = ToolCallHandler(self.tool_executor, self.tool_repair, self._clean_reply, self.context, self.router, xiaoli_delegate=self.delegate_to_xiaoli, agent_name="xiaoda", personality_file=self._get_xiaoda_personality_file(), tool_execute_callback=self._execute_tool_with_hooks)
        self._user_chat_target: dict[str, str] = {}
        self._chat_target_lock = asyncio.Lock()
        self._router_engine = RouterEngine(belief_router=None)  # belief_router 灰度期暂不接入
        self._chat_processor = ChatProcessor(self)
        self._tool_orchestrator = ToolOrchestrator(self)
        self._voice_mode: bool = False
        self._error_handler = None
        self._mcp_manager = MCPManager()
        self.instinct_manager: InstinctManager | None = None
        # P5: 失败经验→规则闭环（bootstrap 阶段注入，失败时保持 None）
        self.error_pipeline = None
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
    def hook_engine(self) -> Any:
        """返回已注册的钩子引擎实例."""
        return self._hook_engine

    def get_sticker_manager(self, name: str) -> Any:
        """获取指定智能体的表情包管理器。

        - xiaoda/空 → 主 sticker_manager
        - xiaoli → xiaoli_sticker_manager
        - 其他 → 从 _agent_route_configs 获取 sticker_dir 动态创建（LazyLoader 延迟加载）
        - 表情包目录为空时 available 返回 False，表情包不生效
        """
        name_lower = (name or "").lower()
        if name_lower in ("xiaoda", ""):
            return self.sticker_manager
        if name_lower == "xiaoli":
            return self.xiaoli_sticker_manager
        if name_lower in self._sticker_managers:
            return self._sticker_managers[name_lower]
        # 从路由配置获取 sticker_dir
        route_cfg = self._agent_route_configs.get(name_lower, {})
        sticker_dir = route_cfg.get("sticker_dir", "")
        if not sticker_dir:
            return self.sticker_manager
        loader = LazyLoader("emotion.sticker_manager.StickerManager", {"sticker_dir": sticker_dir})
        self._sticker_managers[name_lower] = loader
        return loader

    async def init(self, reinit: bool = False) -> None:
        """异步初始化 Agent 核心组件.

        Args:
            reinit: 是否强制重新初始化, 默认 False
        """
        bootstrapper = AgentCoreBootstrapper(self)
        await bootstrapper.bootstrap(reinit=reinit)
        # jieba 后台预热：在事件循环内 fire-and-forget 启动，避免首次对话加载
        # ~5MB 词典阻塞 1-2 秒（db/fts_utils.py / memory/memory_manager.py 的 lazy import）。
        # 预热失败不影响 AgentCore 初始化；幂等性由 prewarm_jieba 内部标志保证。
        try:
            from core.jieba_prewarm import prewarm_jieba
            asyncio.create_task(prewarm_jieba())
        except Exception as e:
            logger.warning(f"jieba.prewarm_schedule_failed error={e}")

    def _get_xiaoda_personality_file(self) -> str:
        """获取 xiaoda 人格文件路径（frozen 模式下使用用户目录）"""
        try:
            from config import AGENTS_CONFIG_DIR
            return str(AGENTS_CONFIG_DIR / "xiaoda_personality.md")
        except ImportError:
            return str(Path(__file__).parent.parent / "config" / "agents" / "xiaoda_personality.md")

    @staticmethod
    def _read_address_term_from_user_md() -> str | None:
        """从 USER.md 读取用户自定义称呼。

        匹配 "- 称呼：xxx" 或 "- 称呼: xxx"，过滤占位符文本。
        文件不存在或格式不正确时返回 None，由调用方兜底。
        """
        from config import WORKSPACE_DIR
        user_md = WORKSPACE_DIR / "USER.md"
        if not user_md.exists():
            return None
        try:
            content = user_md.read_text(encoding="utf-8-sig")
            # 匹配 "- 称呼：xxx" 或 "- 称呼: xxx"
            match = re.search(r'-\s*称呼[：:]\s*(.+)', content)
            if match:
                val = match.group(1).strip()
                # 过滤占位符文本（USER.md.tpl 中的提示语）
                if val and not val.startswith("（") and val not in ("待填写", "主人/朋友/你的名字"):
                    return val
        except Exception as e:
            logger.debug("core.read_address_term_failed", error=str(e))
        return None

    def _build_owner_identity(self) -> UserIdentity:
        """构建主人身份，address_term 从 USER.md 读取，兜底"爸爸"。"""
        addr = self._read_address_term_from_user_md() or "爸爸"
        return UserIdentity(is_owner=True, display_name="爸爸", address_term=addr)

    def _resolve_identity(self, user_id: str, user_openid: str = "",
                          source: str = "") -> UserIdentity:
        """运行时身份解析：基于 openID/UID 稳定标识判断用户身份，不依赖消息内容。

        身份判定规则：
        - QQ 群聊（source == "qq_group"）：严格按 owner_ids 判断，区分主人/非主人
        - 其他来源（web、cli、qq_c2c 等）：默认主人，使用完整提示词
        """
        # 非 QQ 群聊场景默认主人（webui/cli/单聊等均为爸爸本人使用）
        if source != "qq_group":
            return self._build_owner_identity()
        # QQ 群聊场景：基于 openID 严格判断
        check_id = user_openid or user_id
        if not check_id:
            return self._build_owner_identity()
        is_owner = self.security.is_owner(check_id)
        if is_owner:
            return self._build_owner_identity()
        return UserIdentity(is_owner=False, display_name="用户", address_term="用户")

    async def process(self, user_input: str, user_id: str = "qq_user",
                      source: str = "qq",
                      user_openid: str = "",
                      session_id: str = "",
                      status_callback: Any=None,
                      image_data: list[dict] | None = None,
                      is_master: bool = True) -> ProcessResult:
        """处理用户输入并返回回复结果 (统一入口, 含身份解析与上下文管理).

        Args:
            user_input: 用户输入文本
            user_id: 用户标识, 默认 'qq_user'
            source: 消息来源 (qq/web/cli 等), 默认 'qq'
            user_openid: 用户 openID, 用于身份解析
            session_id: 会话 ID
            status_callback: 状态回调函数
            image_data: 附带图片列表
            is_master: 是否主人 (将被身份解析结果覆盖)

        Returns:
            ProcessResult 包含回复文本与元数据
        """
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
        """处理纯文本输入并直接返回回复字符串 (CLI/Web 便捷入口)."""
        result = await self.process(user_input, user_id="cli_owner", source="cli", user_openid=user_openid, session_id=session_id)
        return result.reply

    async def get_session(self, user_openid: str) -> dict | None:
        """获取指定用户的活跃会话, 不存在返回 None."""
        return await self.db.get_active_session(user_openid)

    async def create_session(self, user_openid: str = "") -> str:
        """为用户创建新会话, 返回会话 ID."""
        return await self.db.create_session(user_openid)

    async def receive_file(self, attachment: Any) -> dict:
        """接收并处理附件文件, 返回处理结果字典."""
        return await self.file_receiver.receive(attachment)

    def strip_emotion_tag(self, text: str) -> str:
        """剥离文本中的 [emotion:xxx] 标签, 防止泄露给用户."""
        # 先提取情绪值（供 sticker_manager 使用）
        result = self.sticker_manager.strip_emotion_tag(text)
        # 兜底：强制剥离所有 [emotion:xxx] 标签（防止 LLM 在句中/句尾输出标签泄露给用户）
        result = re.sub(r'\[emotion:[^\]]*\]', '', result).strip()
        return result

    def set_voice_mode(self, enabled: bool) -> None:
        """开启或关闭语音模式."""
        self._voice_mode = enabled

    def get_voice_mode(self) -> bool:
        """返回当前语音模式是否开启."""
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
            # 取消共享黑板后台清理任务
            cleanup_task = getattr(self, "_shared_blackboard_cleanup_task", None)
            if cleanup_task and not cleanup_task.done():
                cleanup_task.cancel()
                await cleanup_task
        except Exception as e:
            logger.warning("shutdown.blackboard_cleanup_cancel_failed", error=str(e))

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

        # 关闭 AsyncOpenAI 客户端, 释放 TCP 连接
        try:
            if self.router and hasattr(self.router, 'close'):
                await self.router.close()
        except Exception as e:
            logger.warning("shutdown.router_close_failed", error=str(e))

        try:
            if self.xiaoli and hasattr(self.xiaoli, 'close'):
                await self.xiaoli.close()
        except Exception as e:
            logger.warning("shutdown.xiaoli_close_failed", error=str(e))

        try:
            if self.dispatcher and hasattr(self.dispatcher, 'close'):
                await self.dispatcher.close()
        except Exception as e:
            logger.warning("shutdown.dispatcher_close_failed", error=str(e))

        try:
            from tools.agnes_tools import close_agnes_clients
            await close_agnes_clients()
        except Exception as e:
            logger.warning("shutdown.agnes_close_failed", error=str(e))

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
