"""AgentCore 启动引导器 — 从 agent_core.py 提取的初始化逻辑。

职责：
- 基础设施初始化（数据库、向量存储）
- 认知系统初始化（记忆、知识图谱、笔记本、学习、画像、本能）
- 子代理注册与任务图构建
- 交互层初始化（错误处理、上下文恢复、斜杠命令）
- MCP 服务器启动
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from config import MIMO_API_KEY, MIMO_BASE_URL
from core.background_tasks import BackgroundTaskManager

if TYPE_CHECKING:
    from agent_core import AgentCore


class AgentCoreBootstrapper:
    """将 AgentCore 的异步初始化流程封装为独立类。

    用法::

        core = AgentCore()
        bootstrapper = AgentCoreBootstrapper(core)
        await bootstrapper.bootstrap()
    """

    def __init__(self, core: AgentCore):
        self.core = core

    async def bootstrap(self) -> None:
        """执行完整的初始化流程。缺少 API Key 时降级启动，仅提供 WebUI 设置页面。"""
        from config import MIMO_API_KEY as _mimo_key
        if not _mimo_key or not _mimo_key.strip():
            logger.warning("agent_core.degraded_mode reason=no_mimo_api_key")
            # 仅初始化数据库等不依赖 API Key 的基础设施
            try:
                await self._init_infrastructure()
                await self._init_cognitive()
            except Exception as e:
                logger.warning("agent_core.degraded_init_partial_error error={}", str(e))
            # _initialized 保持 False → process() 返回降级回复
            return

        await self._init_infrastructure()
        await self._init_cognitive()
        await self.core.klee.init()
        await self.core.tts.init()
        await self._register_sub_agents()
        await self._build_task_graph()
        await self._init_interaction()
        await self._init_mcp()
        self.core._initialized = True
        logger.info("agent_core.initialized")

    # ── 基础设施 ──────────────────────────────────────────

    async def _init_infrastructure(self) -> None:
        from memory.vector_store import VectorStore

        core = self.core
        await core.db.init()
        core.router.set_db(core.db, analytics=core.db.analytics)
        embed_api_key = os.getenv("EMBED_API_KEY", "")
        embed_base_url = os.getenv("EMBED_BASE_URL", "https://api.siliconflow.cn/v1")
        core._vec_store = None
        if embed_api_key:
            try:
                core._vec_store = VectorStore(
                    db_path=str(core.db.db_path).replace(".db", "_vec.db"),
                    embed_api_key=embed_api_key,
                    embed_base_url=embed_base_url,
                )
                await core._vec_store.init()
                logger.info("vector_store.enabled")
            except Exception as e:
                logger.warning(f"vector_store.init_failed: {e}")
                core._vec_store = None

    # ── 认知系统 ──────────────────────────────────────────

    async def _init_cognitive(self) -> None:
        from memory.memory_manager import MemoryManager
        from memory.knowledge_graph import KnowledgeGraph
        from memory.notebook_manager import NotebookManager
        from memory.learning_manager import LearningManager
        from memory.reranker import Reranker
        from memory.query_transform import QueryTransformer
        from emotion.portrait_manager import PortraitManager
        from instinct_manager import InstinctManager
        import config

        core = self.core

        # 初始化 Reranker（SiliconFlow 免费常驻）
        reranker = None
        if getattr(config, "RERANKER_ENABLED", True):
            rerank_api_key = config.RERANKER_API_KEY or os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("EMBED_API_KEY", "")
            if rerank_api_key:
                reranker = Reranker(
                    api_key=rerank_api_key,
                    base_url=config.RERANKER_BASE_URL,
                    model=config.RERANKER_MODEL,
                )
                logger.info("reranker.enabled", model=config.RERANKER_MODEL)
            else:
                logger.info("reranker.disabled_no_api_key")
        else:
            logger.info("reranker.disabled_by_config")

        # 初始化 QueryTransformer（使用硅基流动免费模型，不占用主模型配额）
        query_transformer = None
        if getattr(config, "QUERY_TRANSFORM_ENABLED", True):
            qt_api_key = os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("EMBED_API_KEY", "")
            if qt_api_key:
                query_transformer = QueryTransformer(
                    api_key=qt_api_key,
                    base_url="https://api.siliconflow.cn/v1",
                )
                logger.info("query_transformer.enabled", model="Qwen/Qwen3-8B (free)")
            else:
                logger.info("query_transformer.disabled_no_api_key")
        else:
            logger.info("query_transformer.disabled_by_config")

        core.memory = MemoryManager(
            db=core.db,
            memory=core.db.memory,
            vector_store=core._vec_store,
            router=core.router,
            reranker=reranker,
            query_transformer=query_transformer,
        )
        core.knowledge_graph = KnowledgeGraph(db=core.db, knowledge_db=core.db.knowledge, router=core.router)
        # 知识图谱提取改用硅基流动免费模型
        sf_key = os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("EMBED_API_KEY", "")
        if sf_key:
            core.knowledge_graph.set_free_model_client(
                api_key=sf_key,
                base_url="https://api.siliconflow.cn/v1",
                model="Qwen/Qwen3-8B",
            )
        core.memory.set_knowledge_graph(core.knowledge_graph)
        # 注入 MemoryManager 到 memory_tool，修复记忆工具不可用问题
        from tools import memory_tool
        memory_tool.bind(core.memory)
        core.notebook_manager = NotebookManager(db=core.db, notebook=core.db.notebook, router=core.router)
        core.learning_manager = LearningManager(db=core.db, learning=core.db.learning, router=core.router)
        core.portrait_manager = PortraitManager(db=core.db, memory=core.db.memory, router=core.router, notebook=core.db.notebook)
        core.instinct_manager = InstinctManager(db=core.db, router=core.router)
        await core.instinct_manager.init()
        # 加载 Instinct 提示到上下文
        instinct_prompt = await core.instinct_manager.build_instinct_prompt()
        if instinct_prompt:
            core.context.instinct_prompt = instinct_prompt
        logger.info("instinct_manager.initialized")

        # 初始化后台任务管理器
        core._bg_task_manager = BackgroundTaskManager(
            db=core.db,
            context=core.context,
            memory=core.memory,
            notebook_manager=core.notebook_manager,
            portrait_manager=core.portrait_manager,
            learning_manager=core.learning_manager,
            instinct_manager=core.instinct_manager,
        )

    # ── 子代理注册 ────────────────────────────────────────

    async def _register_sub_agents(self) -> None:
        from agent_dispatcher import SubAgentConfig

        core = self.core
        keli_config = SubAgentConfig(
            name="keli",
            display_name="可莉",
            provider="mimo",
            model="mimo-v2.5-pro",
            personality_file=str(Path(__file__).resolve().parent.parent / "config" / "agents" / "klee_personality.md"),
            voice_ref="keli",
            excluded_tools={"call_klee", "shell_command", "python_executor", "write_file", "search_files", "read_file", "list_files", "web_browse", "document_reader", "multi_search", "wolfram_query"},
            base_url="https://api.xiaomimimo.com/v1",
            api_key_env="MIMO_API_KEY",
            capabilities=["chat", "play", "fun"],
            route_description="日常聊天、玩耍、轻松有趣的对话",
        )
        await core.dispatcher.register(keli_config)
        yinlang_config = SubAgentConfig(
            name="yinlang",
            display_name="银狼",
            provider="mimo",
            model="mimo-v2.5-pro",
            personality_file=str(Path(__file__).resolve().parent.parent / "config" / "agents" / "yinlang_personality.md"),
            voice_ref=None,
            excluded_tools={"call_klee", "call_nahida"},
            base_url="https://api.xiaomimimo.com/v1",
            api_key_env="MIMO_API_KEY",
            capabilities=["coding", "debug", "script", "programming", "hardware", "system", "devops"],
            route_description="编程、代码编写、调试、技术问题、硬件控制、系统运维、开发辅助",
            mcp_servers=["git", "github"],
        )
        await core.dispatcher.register(yinlang_config)
        xilian_config = SubAgentConfig(
            name="xilian",
            display_name="昔涟",
            provider="mimo",
            model="mimo-v2.5-pro",
            personality_file=str(Path(__file__).resolve().parent.parent / "config" / "agents" / "xilian_personality.md"),
            voice_ref=None,
            excluded_tools={"call_klee", "call_nahida", "shell_command", "python_executor", "write_file"},
            base_url="https://api.xiaomimimo.com/v1",
            api_key_env="MIMO_API_KEY",
            capabilities=["search", "lookup", "query", "explore", "discover"],
            route_description="搜索信息、查询资料、探索发现",
        )
        await core.dispatcher.register(xilian_config)
        nike_config = SubAgentConfig(
            name="nike",
            display_name="尼可",
            provider="mimo",
            model="mimo-v2.5-pro",
            personality_file=str(Path(__file__).resolve().parent.parent / "config" / "agents" / "nike_personality.md"),
            voice_ref=None,
            excluded_tools={"call_klee", "call_nahida", "shell_command", "write_file"},
            base_url="https://api.xiaomimimo.com/v1",
            api_key_env="MIMO_API_KEY",
            capabilities=["research", "analysis", "study", "academic"],
            route_description="研究分析、学术思考、深度解读",
        )
        await core.dispatcher.register(nike_config)

        # 收集路由配置
        for name, agent in core.dispatcher._agents.items():
            core._agent_route_configs[name] = {
                "display_name": agent.config.display_name,
                "capabilities": agent.config.capabilities,
                "route_description": agent.config.route_description,
            }

        self._register_delegate_tool()

    def _register_delegate_tool(self) -> None:
        """注册通用 delegate_task 工具，描述动态嵌入各子代理的 route_description。"""
        from tool_engine.tool_registry import register_tool_direct, ToolPermission

        core = self.core
        roster = "；".join(
            f"{name}（{cfg['display_name']}）：{cfg['route_description']}"
            for name, cfg in core._agent_route_configs.items()
            if cfg.get("route_description"))

        async def delegate_task(agent: str, task: str):
            from tool_engine.tool_executor import ToolResult
            reply = await core.delegate_to_agent(agent.strip().lower(), task)
            return ToolResult.ok(reply)

        register_tool_direct(
            name="delegate_task",
            description=(
                "把任务委托给一位子代理独立完成并返回结果。可选子代理及各自擅长领域："
                f"{roster}。"
                "【严格规则】以下情况绝对不要委托，必须自己回答："
                "1. 日常闲聊、问候、寒暄（如'你好'、'在吗'、'今天怎么样'）；"
                "2. 表情包、情感表达、陪伴对话；"
                "3. 关于你自己的问题（如'你是谁'、'你喜欢什么'）；"
                "4. 简单问答、常识问题；"
                "5. 用户没有明确指定子代理的对话。"
                "只有当任务明确属于某个子代理的专长领域时才委托。"
                "有疑问时不要委托，自己回答。"),
            func=delegate_task,
            parameters={
                "properties": {
                    "agent": {"type": "string",
                              "description": "子代理标识名，如 keli / yinlang / xilian / nike",
                              "enum": list(core._agent_route_configs.keys())},
                    "task": {"type": "string", "description": "委托的任务描述，包含必要上下文"},
                },
                "required": ["agent", "task"],
            },
            permission=ToolPermission.READ_ONLY,
            category="fun",
        )

    # ── 任务图构建 ────────────────────────────────────────

    async def _build_task_graph(self) -> None:
        from openai import AsyncOpenAI as _AOI
        from task_orchestrator import build_task_graph

        core = self.core
        route_client = _AOI(
            api_key=MIMO_API_KEY,
            base_url=MIMO_BASE_URL,
        )
        core._task_graph = build_task_graph(
            dispatcher=core.dispatcher,
            agent_configs=core._agent_route_configs,
            route_client=route_client,
            nahida_chat_callback=core._nahida_synthesis_chat,
        )

    # ── 交互层 ────────────────────────────────────────────

    async def _init_interaction(self) -> None:
        from utils.smart_error_handler import get_error_handler
        from slash_commands import SlashCommandHandler

        core = self.core
        core._error_handler = get_error_handler(
            db=core.db,
            dispatcher=core.dispatcher,
        )
        learning_additions = await core.learning_manager.get_system_prompt_additions()
        if learning_additions:
            core.context.learned_rules = learning_additions
        if core.instinct_manager:
            instinct_prompt = await core.instinct_manager.build_instinct_prompt()
            if instinct_prompt:
                core.context.instinct_prompt = instinct_prompt
        portrait = await core.portrait_manager.get_current_portrait()
        if portrait and portrait.get("content"):
            core.context.user_portrait = portrait["content"]
            logger.info("portrait.loaded", version=portrait.get("version"))
        await core._load_notebook_context()
        await core.context.restore_from_db(core.db)
        core.slash_handler = SlashCommandHandler(
            db=core.db,
            router=core.router,
            context=core.context,
            memory=core.memory,
            learning_manager=core.learning_manager,
            notebook_manager=core.notebook_manager,
            security=core.security,
            agent=core,
        )

    # ── MCP ───────────────────────────────────────────────

    async def _init_mcp(self) -> None:
        from config import MCP_SERVERS

        core = self.core
        if MCP_SERVERS:
            try:
                await core._mcp_manager.start_all(MCP_SERVERS)
                logger.info("mcp.servers_started", count=len(core._mcp_manager._clients))
            except Exception as e:
                logger.warning("mcp.start_failed", error=str(e))
