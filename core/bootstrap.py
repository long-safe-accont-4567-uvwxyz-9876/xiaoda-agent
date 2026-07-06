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
from typing import Any, TYPE_CHECKING

from loguru import logger

from config import MIMO_API_KEY, MIMO_BASE_URL, _ensure_workspace_template
from config import DEFAULT_PROVIDER as _DEFAULT_PROVIDER, PRO_MODEL_NAME as _PRO_MODEL
from config import MODEL_NAME as _MODEL_NAME, get_provider_config as _get_provider_config
from config import get_agent_display_name
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

    def __init__(self, core: AgentCore) -> None:
        self.core = core

    async def bootstrap(self, reinit: bool = False) -> None:
        """执行完整的初始化流程。缺少 API Key 时降级启动，仅提供 WebUI 设置页面。

        Args:
            reinit: 为 True 时跳过已初始化的基础设施和认知系统，
                    仅执行降级模式中未完成的步骤（xiaoli/tts/sub_agents 等）。
                    各步骤独立容错，单个步骤失败不会阻止核心聊天功能。
        """
        from config import MIMO_API_KEY as _mimo_key
        from utils.encrypted_credential import reveal_credential
        _mimo_key = reveal_credential(_mimo_key)
        if not _mimo_key or not _mimo_key.strip():
            logger.warning("agent_core.degraded_mode reason=no_mimo_api_key")
            if not reinit:
                # 首次降级启动：初始化基础设施
                try:
                    await self._init_infrastructure()
                    _ensure_workspace_template()
                    await self._init_cognitive()
                except Exception as e:
                    logger.warning("agent_core.degraded_init_partial_error error={}", str(e))
            # _initialized 保持 False → process() 返回降级回复
            return

        if reinit:
            # 降级模式恢复：跳过已初始化的基础设施和认知系统
            logger.info("agent_core.reinit_skipping_infrastructure")
        else:
            await self._init_infrastructure()
            _ensure_workspace_template()
            await self._init_cognitive()

        # 共享黑板后台清理任务（避免过期条目堆积，惰性清理之外的周期兜底）
        try:
            if self.core._shared_blackboard is not None:
                self.core._shared_blackboard_cleanup_task = await self.core._shared_blackboard.start_cleanup_task()
                logger.info("blackboard.cleanup_task_started")
        except Exception as e:
            logger.warning("agent_core.blackboard_cleanup_start_failed error={}", str(e))

        # 以下步骤各自独立容错：单个可选功能失败不应阻止核心聊天
        # xiaoli 子代理（可选）
        try:
            await self.core.xiaoli.init()
        except Exception as e:
            logger.warning("agent_core.reinit_xiaoli_failed error={}", str(e))

        # 参考音频和表情包（可选，仅复制文件）
        try:
            self._ensure_voice_refs()
        except Exception as e:
            logger.warning("agent_core.reinit_voice_refs_failed error={}", str(e))
        try:
            self._ensure_stickers()
        except Exception as e:
            logger.warning("agent_core.reinit_stickers_failed error={}", str(e))

        # TTS 引擎（可选）
        try:
            await self.core.tts.init()
        except Exception as e:
            logger.warning("agent_core.reinit_tts_failed error={}", str(e))

        # 子代理注册（可选，失败不影响主 Agent 聊天）
        try:
            await self._register_sub_agents()
        except Exception as e:
            logger.warning("agent_core.reinit_sub_agents_failed error={}", str(e))

        # 任务图（可选，复杂任务路由需要）
        try:
            await self._build_task_graph()
        except Exception as e:
            logger.warning("agent_core.reinit_task_graph_failed error={}", str(e))

        # 交互层（可选，包含斜杠命令、画像等）
        try:
            await self._init_interaction()
        except Exception as e:
            logger.warning("agent_core.reinit_interaction_failed error={}", str(e))

        # MCP 服务器（可选）
        try:
            await self._init_mcp()
        except Exception as e:
            logger.warning("agent_core.reinit_mcp_failed error={}", str(e))

        # 刷新 ToolCallRepair 的工具名快照（delegate_task 等动态注册的工具在 __init__ 之后才出现）
        try:
            from tool_engine.tool_registry import to_openai_tools
            self.core.tool_repair._allowed_tools = set(t["function"]["name"] for t in to_openai_tools())
        except Exception as e:
            logger.debug("agent_core.reinit_tool_repair_refresh_failed error={}", str(e))

        # 自动加载并启用插件（discover 已在 web/server.py 中完成）
        try:
            await self._auto_enable_plugins()
        except Exception as e:
            logger.warning("agent_core.reinit_plugins_failed error={}", str(e))

        self.core._initialized = True
        logger.info("agent_core.initialized" + (" (reinit)" if reinit else ""))

    # ── 基础设施 ──────────────────────────────────────────

    def _get_bundled_assets_dir(self) -> Path:
        """获取安装包内置 assets 目录"""
        try:
            import sys
            if getattr(sys, 'frozen', False):
                return Path(sys._MEIPASS) / "assets"
            else:
                return Path(__file__).resolve().parent.parent / "assets"
        except Exception:
            logger.debug("bootstrap.bundled_assets_dir_fallback: {}", exc_info=True)
            return Path(__file__).resolve().parent.parent / "assets"

    def _ensure_voice_refs(self) -> None:
        """首次运行时将参考音频从安装包复制到用户数据目录"""
        import shutil
        from emotion.tts_engine import KIOXIA_BASE
        bundled_dir = self._get_bundled_assets_dir() / "voice_refs"
        if not bundled_dir.exists():
            return
        KIOXIA_BASE.mkdir(parents=True, exist_ok=True)
        for filename in ("xiaoda_hq.wav", "xiaoda.wav", "xiaoli.mp3"):
            dest = KIOXIA_BASE / filename
            if not dest.exists():
                src = bundled_dir / filename
                if src.exists():
                    try:
                        shutil.copy2(src, dest)
                        logger.info("bootstrap.voice_ref_copied", file=filename)
                    except Exception as e:
                        logger.warning("bootstrap.voice_ref_copy_failed", file=filename, error=str(e))

    def _ensure_stickers(self) -> None:
        """首次运行时将表情包从安装包复制到用户数据目录"""
        import shutil
        from config import STICKER_DIR, XIAOLI_STICKER_DIR
        bundled_dir = self._get_bundled_assets_dir() / "stickers"
        if not bundled_dir.exists():
            return

        # 复制 xiaoda 表情包
        xiaoda_src = bundled_dir / "xiaoda"
        if xiaoda_src.exists() and xiaoda_src.is_dir():
            STICKER_DIR.mkdir(parents=True, exist_ok=True)
            for emotion_dir in xiaoda_src.iterdir():
                if emotion_dir.is_dir():
                    dest_emotion = STICKER_DIR / emotion_dir.name
                    if not dest_emotion.exists():
                        try:
                            shutil.copytree(emotion_dir, dest_emotion)
                            logger.info("bootstrap.stickers_copied", voice="xiaoda", emotion=emotion_dir.name)
                        except Exception as e:
                            logger.warning("bootstrap.stickers_copy_failed", voice="xiaoda", emotion=emotion_dir.name)

        # 复制 xiaoli 表情包
        xiaoli_src = bundled_dir / "xiaoli"
        if xiaoli_src.exists() and xiaoli_src.is_dir():
            XIAOLI_STICKER_DIR.mkdir(parents=True, exist_ok=True)
            for emotion_dir in xiaoli_src.iterdir():
                if emotion_dir.is_dir():
                    dest_emotion = XIAOLI_STICKER_DIR / emotion_dir.name
                    if not dest_emotion.exists():
                        try:
                            shutil.copytree(emotion_dir, dest_emotion)
                            logger.info("bootstrap.stickers_copied", voice="xiaoli", emotion=emotion_dir.name)
                        except Exception as e:
                            logger.warning("bootstrap.stickers_copy_failed", voice="xiaoli", emotion=emotion_dir.name)

    # 表情包情绪分类子目录（用户往这些目录放图片即可自动调用）
    _STICKER_EMOTION_DIRS = (
        "happy", "excited", "love", "shy",
        "sad", "angry", "surprised", "confused",
        "thinking", "playful", "moved", "neutral",
        "pout", "fear", "anxious",
    )

    def _ensure_agent_sticker_dirs(self, core) -> None:
        """为每个子智能体自动创建专属表情包目录。

        - 已配置 sticker_dir 的（如 xiaoli 复用 XIAOLI_STICKER_DIR）跳过自动推导
        - 未配置的自动推导为 {AGENT_STICKER_BASE}/{agent_name}/
        - 自动创建情绪分类子目录（空目录），用户往里放图片即可
        - 目录为空时 StickerManager.available 返回 False，表情包不生效
        """
        from config import AGENT_STICKER_BASE
        base = Path(AGENT_STICKER_BASE)
        for name, agent in core.dispatcher._agents.items():
            cfg = agent.config
            if not cfg.sticker_dir:
                sticker_path = base / name
                cfg.sticker_dir = str(sticker_path)
            sticker_path = Path(cfg.sticker_dir)
            if not sticker_path.exists():
                sticker_path.mkdir(parents=True, exist_ok=True)
                # 创建情绪分类子目录作为引导
                for emotion_dir in self._STICKER_EMOTION_DIRS:
                    (sticker_path / emotion_dir).mkdir(exist_ok=True)
                logger.info("bootstrap.agent_sticker_dir_created", agent=name, path=str(sticker_path))

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
                    db_path=str(core.db.db_path.parent / (core.db.db_path.stem + "_vec.db")),
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
        """初始化认知系统：Reranker、QueryTransformer、Memory、KG、Instinct、ErrorPipeline。"""
        from memory.memory_manager import MemoryManager
        from memory.knowledge_graph import KnowledgeGraph
        from memory.notebook_manager import NotebookManager
        from memory.learning_manager import LearningManager
        from emotion.portrait_manager import PortraitManager
        from instinct_manager import InstinctManager
        import config

        core = self.core

        # 1. Reranker + QueryTransformer（硅基流动免费模型）
        reranker = self._init_reranker(config)
        query_transformer = self._init_query_transformer(config)

        # 2. Memory + KnowledgeGraph
        core.memory = MemoryManager(
            db=core.db,
            memory=core.db.memory,
            vector_store=core._vec_store,
            router=core.router,
            reranker=reranker,
            query_transformer=query_transformer,
        )
        # ContextNest A2/A3: 注入上下文治理 (哈希链 + 审计追踪)
        try:
            from memory.context_governance import ContextGovernance
            governance = ContextGovernance(conn=core.db._conn)
            core.memory.set_governance(governance)
        except Exception as e:
            logger.warning("bootstrap.governance_init_failed", error=str(e))
        core.knowledge_graph = KnowledgeGraph(db=core.db, knowledge_db=core.db.knowledge, router=core.router)
        sf_key = os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("EMBED_API_KEY", "")
        if sf_key:
            core.knowledge_graph.set_free_model_client(
                api_key=sf_key,
                base_url="https://api.siliconflow.cn/v1",
                model="THUDM/GLM-4-9B-0414",
            )
        core.memory.set_knowledge_graph(core.knowledge_graph)
        # 注入 MemoryManager 到 memory_tool，修复记忆工具不可用问题
        from tools import memory_tool
        memory_tool.bind(core.memory)
        core.notebook_manager = NotebookManager(db=core.db, notebook=core.db.notebook, router=core.router)
        core.learning_manager = LearningManager(db=core.db, learning=core.db.learning, router=core.router)
        core.portrait_manager = PortraitManager(db=core.db, memory=core.db.memory, router=core.router, notebook=core.db.notebook)

        # 3. Instinct + ErrorRulePipeline（使用硅基流动免费模型）
        await self._init_instinct_and_pipeline(core, sf_key)

        # 4. 后台任务管理器
        core._bg_task_manager = BackgroundTaskManager(
            db=core.db,
            context=core.context,
            memory=core.memory,
            notebook_manager=core.notebook_manager,
            portrait_manager=core.portrait_manager,
            learning_manager=core.learning_manager,
            instinct_manager=core.instinct_manager,
        )

    @staticmethod
    def _init_reranker(config: Any) -> Any:
        """初始化 Reranker（SiliconFlow 免费常驻）。无 API Key 时返回 None。"""
        from memory.reranker import Reranker
        if not getattr(config, "RERANKER_ENABLED", True):
            logger.info("reranker.disabled_by_config")
            return None
        rerank_api_key = config.RERANKER_API_KEY or os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("EMBED_API_KEY", "")
        if not rerank_api_key:
            logger.info("reranker.disabled_no_api_key")
            return None
        logger.info("reranker.enabled", model=config.RERANKER_MODEL)
        return Reranker(
            api_key=rerank_api_key,
            base_url=config.RERANKER_BASE_URL,
            model=config.RERANKER_MODEL,
        )

    @staticmethod
    def _init_query_transformer(config: Any) -> Any:
        """初始化 QueryTransformer（硅基流动免费模型）。无 API Key 时返回 None。"""
        from memory.query_transform import QueryTransformer
        if not getattr(config, "QUERY_TRANSFORM_ENABLED", True):
            logger.info("query_transformer.disabled_by_config")
            return None
        qt_api_key = os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("EMBED_API_KEY", "")
        if not qt_api_key:
            logger.info("query_transformer.disabled_no_api_key")
            return None
        logger.info("query_transformer.enabled", model="Qwen/Qwen2.5-7B-Instruct (free)")
        return QueryTransformer(
            api_key=qt_api_key,
            base_url="https://api.siliconflow.cn/v1",
        )

    async def _init_instinct_and_pipeline(self, core: Any, sf_key: str) -> None:
        """初始化 InstinctManager 和 ErrorRulePipeline，并注入 ToolCallHandler。"""
        from instinct_manager import InstinctManager
        core.instinct_manager = InstinctManager(db=core.db, router=core.router)
        await core.instinct_manager.init()
        # Instinct 提取改用硅基流动免费模型
        if sf_key:
            core.instinct_manager.set_free_model_client(
                api_key=sf_key,
                base_url="https://api.siliconflow.cn/v1",
                model="Qwen/Qwen2.5-7B-Instruct",
            )
        # 加载 Instinct 提示到上下文
        instinct_prompt = await core.instinct_manager.build_instinct_prompt()
        if instinct_prompt:
            core.context.instinct_prompt = instinct_prompt
        logger.info("instinct_manager.initialized")

        # P5: 失败经验→规则闭环（可选组件，失败安全）
        try:
            from tool_engine.error_rule_pipeline import ErrorRulePipeline
            core.error_pipeline = ErrorRulePipeline(db=core.db, router=core.router)
            if sf_key:
                core.error_pipeline.set_free_model_client(
                    api_key=sf_key,
                    base_url="https://api.siliconflow.cn/v1",
                    model="Qwen/Qwen2.5-7B-Instruct",
                )
            # 注入到 ToolCallHandler（延后注入，避免构造期循环依赖）
            if getattr(core, "_tool_call_handler", None) is not None:
                core._tool_call_handler.set_error_pipeline(core.error_pipeline)
            logger.info("error_rule_pipeline.initialized")
        except Exception as e:
            core.error_pipeline = None
            logger.warning("error_rule_pipeline.init_failed", error=str(e))

    # ── 子代理注册 ────────────────────────────────────────

    async def _register_sub_agents(self) -> None:
        from agent_dispatcher import SubAgentConfig
        from config import XIAOLI_STICKER_DIR, AGENT_STICKER_BASE
        # frozen 模式下使用用户目录中的 agents 配置（_init_user_resources 已复制模板）
        try:
            from config import AGENTS_CONFIG_DIR as _agents_dir
        except ImportError:
            _agents_dir = Path(__file__).resolve().parent.parent / "config" / "agents"

        core = self.core
        _prov_cfg = _get_provider_config(_DEFAULT_PROVIDER)
        _agent_model = _PRO_MODEL or _MODEL_NAME

        xiaoli_config = SubAgentConfig(
            name="xiaoli",
            display_name=get_agent_display_name("xiaoli"),
            provider=_DEFAULT_PROVIDER,
            model=_agent_model,
            personality_file=str(_agents_dir / "xiaoli_personality.md"),
            voice_ref="xiaoli",
            excluded_tools={"call_xiaoli", "shell_command", "python_executor", "write_file", "search_files", "read_file", "list_files", "web_browse", "document_reader", "multi_search", "wolfram_query"},
            base_url=_prov_cfg["base_url"],
            api_key_env=_prov_cfg["api_key_env"],
            capabilities=["chat", "play", "fun"],
            route_description="日常聊天、玩耍、轻松有趣的对话",
            sticker_dir=str(XIAOLI_STICKER_DIR),
        )
        await core.dispatcher.register(xiaoli_config)
        xiaolang_config = SubAgentConfig(
            name="xiaolang",
            display_name=get_agent_display_name("xiaolang"),
            provider=_DEFAULT_PROVIDER,
            model=_agent_model,
            personality_file=str(_agents_dir / "xiaolang_personality.md"),
            voice_ref=None,
            excluded_tools={"call_xiaoli", "call_xiaoda"},
            base_url=_prov_cfg["base_url"],
            api_key_env=_prov_cfg["api_key_env"],
            capabilities=["coding", "debug", "script", "programming", "hardware", "system", "devops"],
            route_description="编程、代码编写、调试、技术问题、硬件控制、系统运维、开发辅助",
            mcp_servers=["git", "github"],
        )
        await core.dispatcher.register(xiaolang_config)
        xiaolian_config = SubAgentConfig(
            name="xiaolian",
            display_name=get_agent_display_name("xiaolian"),
            provider=_DEFAULT_PROVIDER,
            model=_agent_model,
            personality_file=str(_agents_dir / "xiaolian_personality.md"),
            voice_ref=None,
            excluded_tools={"call_xiaoli", "call_xiaoda", "shell_command", "python_executor", "write_file"},
            base_url=_prov_cfg["base_url"],
            api_key_env=_prov_cfg["api_key_env"],
            capabilities=["search", "lookup", "query", "explore", "discover"],
            route_description="搜索信息、查询资料、探索发现",
        )
        await core.dispatcher.register(xiaolian_config)
        xiaoke_config = SubAgentConfig(
            name="xiaoke",
            display_name=get_agent_display_name("xiaoke"),
            provider=_DEFAULT_PROVIDER,
            model=_agent_model,
            personality_file=str(_agents_dir / "xiaoke_personality.md"),
            voice_ref=None,
            excluded_tools={"call_xiaoli", "call_xiaoda", "shell_command", "write_file"},
            base_url=_prov_cfg["base_url"],
            api_key_env=_prov_cfg["api_key_env"],
            capabilities=["research", "analysis", "study", "academic"],
            route_description="研究分析、学术思考、深度解读",
        )
        await core.dispatcher.register(xiaoke_config)

        # 为每个子智能体自动创建表情包目录（含示例情绪分类子目录）
        self._ensure_agent_sticker_dirs(core)

        # 收集路由配置
        for name, agent in core.dispatcher._agents.items():
            core._agent_route_configs[name] = {
                "display_name": agent.config.display_name,
                "capabilities": agent.config.capabilities,
                "route_description": agent.config.route_description,
                "sticker_dir": agent.config.sticker_dir,
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

        async def delegate_task(agent: str, task: str,
                                 mode: str = "single", verifier: str = "") -> Any:
            """将任务委托给指定子代理完成并返回结果。

            mode=generate_verify 时，verifier 指定的子代理会独立审查结果。
            """
            from tool_engine.tool_executor import ToolResult
            reply = await core.delegate_to_agent(
                agent.strip().lower(), task, mode=mode, verifier=verifier)
            return ToolResult.ok(reply)

        register_tool_direct(
            name="delegate_task",
            description=(
                "把任务委托给一位子代理完成并返回结果。可选子代理及各自擅长领域："
                f"{roster}。"
                "【操作模式】mode=single（默认，直接执行）；"
                "mode=generate_verify（生成+交叉验证，需指定 verifier，"
                "适用于代码修改、安全分析等需要二次确认的任务）；"
                "mode=pipe（顺序管道，agent 用逗号分隔多个，如 'xiaolian,xiaoke'，"
                "前一个的输出作为后一个的输入，适用于搜索→分析→综合等场景）；"
                "mode=ensemble（集成模式，agent 用逗号分隔多个，"
                "多 agent 并行解决同一任务取最优结果，适用于创意/多解任务）；"
                "mode=retry_fallback（重试降级，agent 用逗号分隔按优先级，"
                "失败自动降级到下一个，适用于高可靠性任务）；"
                "mode=debate（辩论模式，agent 填两个辩论方，verifier 填综合者，"
                "正反方独立论证后综合，适用于分析/决策任务）。"
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
                              "description": "子代理标识名，如 xiaoli / xiaolang / xiaolian / xiaoke",
                              "enum": list(core._agent_route_configs.keys())},
                    "task": {"type": "string", "description": "委托的任务描述，包含必要上下文"},
                    "mode": {"type": "string",
                             "description": "操作模式：single(默认) / generate_verify(生成+验证) / pipe(顺序管道,agent用逗号分隔) / ensemble(集成,多agent并行取最优) / retry_fallback(重试降级,按优先级失败降级) / debate(辩论,正反方+综合者)",
                             "enum": ["single", "generate_verify", "pipe",
                                      "ensemble", "retry_fallback", "debate"],
                             "default": "single"},
                    "verifier": {"type": "string",
                                 "description": "验证子代理名（仅 mode=generate_verify 时使用）",
                                 "enum": list(core._agent_route_configs.keys()),
                                 "default": ""},
                },
                "required": ["agent", "task"],
            },
            permission=ToolPermission.READ_ONLY,
            category="fun",
        )

        self._register_sticker_tool()

    def _register_sticker_tool(self) -> None:
        """注册 list_stickers 工具，让 LLM 可以查看可用表情包及描述，从而精准选择。"""
        from tool_engine.tool_registry import register_tool_direct, ToolPermission

        core = self.core

        async def list_stickers(emotion: str = "") -> Any:
            """列出当前可用的表情包及描述。

            可以用 emotion 参数筛选特定情绪分类的表情包。
            返回的 name 字段可用于在回复中用 [sticker:name] 精准指定要发送的表情包。
            """
            mgr = core.sticker_manager
            if not mgr.available:
                return {"stickers": [], "hint": "当前没有可用的表情包"}
            stickers = mgr.list_stickers(emotion=emotion)
            return {"stickers": stickers, "total": len(stickers)}

        register_tool_direct(
            name="list_stickers",
            description=(
                "列出当前可用的表情包列表及每张表情包的描述。"
                "你可以在回复中用 [sticker:文件名] 标签精准指定要发送的表情包。"
                "emotion 参数可选，用于筛选特定情绪（如 happy/sad/angry/curious/shy/thinking/neutral/greeting）。"
                "不传 emotion 则列出全部。建议在需要发送表情包时先调用此工具查看可用选项。"
            ),
            func=list_stickers,
            parameters={
                "properties": {
                    "emotion": {
                        "type": "string",
                        "description": "情绪分类筛选（可选）：happy/sad/angry/curious/shy/thinking/neutral/greeting",
                        "enum": ["", "happy", "sad", "angry", "curious", "shy", "thinking", "neutral", "greeting"],
                        "default": "",
                    },
                },
                "required": [],
            },
            permission=ToolPermission.READ_ONLY,
            category="fun",
        )

    async def _build_task_graph(self) -> None:
        from openai import AsyncOpenAI as _AOI
        from task_orchestrator import build_task_graph
        import os as _os

        core = self.core
        # 从 os.getenv() 实时读取，避免使用模块级冻结的空 API Key
        _key = _os.getenv("MIMO_API_KEY", "")
        _url = _os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
        route_client = _AOI(
            api_key=_key,
            base_url=_url,
        )
        core._task_graph = build_task_graph(
            dispatcher=core.dispatcher,
            agent_configs=core._agent_route_configs,
            route_client=route_client,
            xiaoda_chat_callback=core._xiaoda_synthesis_chat,
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
        import json as _json
        from config import MCP_SERVERS, WORKSPACE_DIR

        core = self.core

        # 合并配置文件中的 MCP 服务器 + 市场安装的 MCP 服务器
        all_servers: dict[str, Any] = {}
        if MCP_SERVERS:
            all_servers.update(MCP_SERVERS)

        # 加载市场安装的 MCP 配置（mcp_configs/*.json）
        mcp_configs_dir = WORKSPACE_DIR / "mcp_configs"
        if mcp_configs_dir.is_dir():
            for fp in mcp_configs_dir.glob("*.json"):
                try:
                    cfg = _json.loads(fp.read_text(encoding="utf-8"))
                    connections = cfg.get("connections", "")
                    if isinstance(connections, str) and connections:
                        try:
                            connections = _json.loads(connections)
                        except Exception:
                            logger.debug("bootstrap.mcp_connections_json_parse_error: {}", exc_info=True)
                            connections = {}
                    if isinstance(connections, dict) and connections.get("command"):
                        server_name = cfg.get("id", fp.stem)
                        all_servers[server_name] = connections
                        logger.debug("mcp.loaded_installed", name=server_name)
                except Exception as e:
                    logger.debug("mcp.load_installed_failed", file=fp.name, error=str(e))

        if all_servers:
            try:
                await core._mcp_manager.start_all(all_servers)
                logger.info("mcp.servers_started", count=len(core._mcp_manager._clients))
            except Exception as e:
                logger.warning("mcp.start_failed", error=str(e))

    # ── 插件自动启用 ──────────────────────────────────────

    async def _auto_enable_plugins(self) -> None:
        """自动加载并启用已发现的插件。

        PluginManager.discover() 在 web/server.py 中已完成，
        此处对所有已发现的插件执行 load + enable，
        使插件注册的工具对 LLM 可见。
        """
        from web.server import app
        plugin_mgr = getattr(app.state, "plugin_manager", None)
        if not plugin_mgr:
            return

        to_enable = list(plugin_mgr.plugins.keys())
        for pid in to_enable:
            try:
                loaded = await plugin_mgr.load(pid)
                if loaded:
                    await plugin_mgr.enable(pid)
                    logger.info("plugin.auto_enabled", id=pid)
            except Exception as e:
                logger.debug("plugin.auto_enable_failed", id=pid, error=str(e))

        # 刷新工具 schema 缓存，使插件注册的工具生效
        from tool_engine.tool_registry import invalidate_schema_cache
        invalidate_schema_cache()

        # 再次刷新 ToolCallRepair 快照
        try:
            from tool_engine.tool_registry import to_openai_tools
            self.core.tool_repair._allowed_tools = set(t["function"]["name"] for t in to_openai_tools())
        except Exception:
            logger.debug("bootstrap.tool_repair_refresh_failed: {}", exc_info=True)


def get_base_dir() -> Path:
    """获取应用基础目录（PyInstaller 打包环境或开发环境）"""
    import sys
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    else:
        return Path(__file__).resolve().parent.parent
