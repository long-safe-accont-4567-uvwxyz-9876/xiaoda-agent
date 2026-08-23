"""AgentCore 启动引导器 — 从 agent_core.py 提取的初始化逻辑。

职责：
- 基础设施初始化（数据库、向量存储）
- 认知系统初始化（记忆、知识图谱、笔记本、学习、画像、本能）
- 子代理注册与任务图构建
- 交互层初始化（错误处理、上下文恢复、斜杠命令）
- MCP 服务器启动
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TYPE_CHECKING

from loguru import logger

from config import _ensure_workspace_template
from config import DEFAULT_PROVIDER as _DEFAULT_PROVIDER, PRO_MODEL_NAME as _PRO_MODEL
from config import MODEL_NAME as _MODEL_NAME, get_provider_config as _get_provider_config
from config import get_agent_display_name
from core.background_tasks import BackgroundTaskManager, _spawn

if TYPE_CHECKING:
    from agent_core import AgentCore


class StartupHealth:
    """启动步骤结果聚合器：失败照旧即时 warning，末尾输出一行汇总健康报告。

    背景（技术债 P1-4）：各子步骤独立容错本身合理，但失败模式全是散落
    warning 日志，无聚合视图——出问题时要在日志里翻半天才知道哪些子系统
    降级了。本类只做记录与汇总，不改变任何容错行为。
    """

    def __init__(self) -> None:
        self._steps: dict[str, str] = {}   # name -> "ok" 或错误摘要

    def reset(self) -> None:
        self._steps.clear()

    def record_ok(self, name: str) -> None:
        self._steps[name] = "ok"

    def record_fail(self, name: str, err: str) -> None:
        self._steps[name] = (err or "failed")[:160]

    @property
    def failed(self) -> dict[str, str]:
        return {k: v for k, v in self._steps.items() if v != "ok"}

    def summary(self) -> str:
        total = len(self._steps)
        bad = self.failed
        if not bad:
            return f"startup.health all_ok total={total}"
        detail = "; ".join(f"{k}={v}" for k, v in bad.items())
        return f"startup.health degraded={len(bad)}/{total} failed=[{detail}]"


# 硅基流动免费模型端点：KG 提取（v1/v2）/ QueryTransformer / Instinct / 错误规则
# 管线等辅助 LLM 共用。原先 5 组调用点各自内联 base_url+model 字面量，换模型要改
# 8 处——收敛为常量（对齐 model_router_config 用 provider 元数据消灭硬编码的思路）。
SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
SILICONFLOW_FREE_MODEL = "THUDM/GLM-4-9B-0414"


def _embedding_service_for_mode(
    mode: str,
    model_dir: str,
    query_prefix: str,
) -> Any | None:
    if mode != "local":
        return None
    from local_ai.integration.embedding import LocalEmbeddingService

    return LocalEmbeddingService.bundled(model_dir, query_prefix=query_prefix)


async def _local_memory_services(
    instance_manager: Any,
    embed_mode: str,
    model_dir: str,
    query_prefix: str,
    *,
    remote_reranker: Any = None,
) -> Any:
    from local_ai.integration.embedding import LocalEmbeddingService
    from local_ai.integration.reranker import LocalRerankerService

    embedding = LocalEmbeddingService.managed(
        instance_manager,
        lambda: _embedding_service_for_mode(
            embed_mode,
            model_dir,
            query_prefix,
        ),
    )
    reranker = LocalRerankerService.managed(instance_manager, remote_reranker)
    return SimpleNamespace(embedding=embedding, reranker=reranker)


class AgentCoreBootstrapper:
    """将 AgentCore 的异步初始化流程封装为独立类。

    用法::

        core = AgentCore()
        bootstrapper = AgentCoreBootstrapper(core)
        await bootstrapper.bootstrap()
    """

    def __init__(self, core: AgentCore) -> None:
        self.core = core
        self.health = StartupHealth()

    async def _run_optional_step(self, name: str, step: Any, *args: Any) -> None:
        """执行可选初始化步骤：失败 warning + 计入健康报告。

        日志事件规范化：原先 9 个独立事件名（reinit_xiaoli_failed /
        reinit_tts_failed / ...，且 reinit 前缀在首次启动时本来就是误称）
        统一为 agent_core.step_failed step={name}——单事件名+结构化字段
        更易检索。容错行为不变：失败不阻止后续步骤。
        """
        try:
            result = step(*args)
            if asyncio.iscoroutine(result):
                await result
            self.health.record_ok(name)
        except Exception as e:
            logger.warning("agent_core.step_failed step={} error={}", name, str(e))
            self.health.record_fail(name, str(e))

    async def bootstrap(self, reinit: bool = False) -> None:
        """执行完整的初始化流程。缺少 API Key 时降级启动，仅提供 WebUI 设置页面。

        Args:
            reinit: 为 True 时跳过已初始化的基础设施和认知系统，
                    仅执行降级模式中未完成的步骤（xiaoli/tts/sub_agents 等）。
                    各步骤独立容错，单个步骤失败不会阻止核心聊天。
        """
        self.health.reset()
        from config import MIMO_API_KEY as _mimo_key
        from utils.encrypted_credential import reveal_credential
        _mimo_key = reveal_credential(_mimo_key)
        if not _mimo_key or not _mimo_key.strip():
            logger.warning("agent_core.degraded_mode reason=no_mimo_api_key")
            if not reinit:
                # 首次降级启动：基础设施与认知系统分别容错——
                # infra 失败时 cognitive 仍可尝试（memory_manager 等组件
                # 对 vec_store=None 有兜底），但两者失败原因分别记录，
                # 避免单点故障掩盖其它组件的真实错误。
                try:
                    await self._init_infrastructure()
                    _ensure_workspace_template()
                    self.health.record_ok("infrastructure")
                except Exception as e:
                    logger.warning("agent_core.degraded_init_infrastructure_error error={}", str(e))
                    self.health.record_fail("infrastructure", str(e))
                try:
                    await self._init_cognitive()
                    self.health.record_ok("cognitive")
                except Exception as e:
                    logger.warning("agent_core.degraded_init_cognitive_error error={}", str(e))
                    self.health.record_fail("cognitive", str(e))
            # _initialized 保持 False → process() 返回降级回复
            logger.warning(self.health.summary())
            return

        if reinit:
            # 降级模式恢复：跳过已初始化的基础设施和认知系统
            logger.info("agent_core.reinit_skipping_infrastructure")
        else:
            await self._init_infrastructure()
            _ensure_workspace_template()
            await self._init_cognitive()

        await self._bootstrap_optional_components()

        self.core._initialized = True
        self.core.startup_health = self.health
        logger.info("agent_core.initialized" + (" (reinit)" if reinit else ""))
        if self.health.failed:
            logger.warning(self.health.summary())
        else:
            logger.info(self.health.summary())

    async def _bootstrap_optional_components(self) -> None:
        """初始化可选组件（各自独立容错，失败不阻止核心聊天）。"""
        h = self.health
        # J-Space 架构优化初始化（非阻塞，失败不影响主流程）
        try:
            from core.j_space_bootstrap import init_j_space
            init_j_space()
            h.record_ok("j_space")
        except Exception as e:
            logger.warning(f"j_space.bootstrap_failed (non-blocking): {e}")
            h.record_fail("j_space", str(e))

        # 共享黑板后台清理任务（避免过期条目堆积，惰性清理之外的周期兜底）
        try:
            if self.core._shared_blackboard is not None:
                self.core._shared_blackboard_cleanup_task = await self.core._shared_blackboard.start_cleanup_task()
                logger.info("blackboard.cleanup_task_started")
            h.record_ok("blackboard_cleanup")
        except Exception as e:
            logger.warning("agent_core.blackboard_cleanup_start_failed error={}", str(e))
            h.record_fail("blackboard_cleanup", str(e))

        # 以下步骤各自独立容错：单个可选功能失败不应阻止核心聊天
        await self._run_optional_step("xiaoli", self.core.xiaoli.init)
        await self._run_optional_step("voice_refs", self._ensure_voice_refs)
        await self._run_optional_step("stickers", self._ensure_stickers)

        # TTS 引擎（可选）+ 注册全局单例供 synthesize_voice 工具访问
        async def _init_tts() -> None:
            await self.core.tts.init()
            from emotion.tts_engine import set_global_tts_engine
            set_global_tts_engine(self.core.tts)
        await self._run_optional_step("tts", _init_tts)

        # 子代理注册 / 任务图 / 交互层 / MCP / 插件（均可选）
        await self._run_optional_step("sub_agents", self._register_sub_agents)
        await self._run_optional_step("task_graph", self._build_task_graph)
        await self._run_optional_step("interaction", self._init_interaction)
        await self._run_optional_step("mcp", self._init_mcp)

        # 刷新 ToolCallRepair 的工具名快照（delegate_task 等动态注册的工具在 __init__ 之后才出现）
        def _refresh_tool_repair() -> None:
            from tool_engine.tool_registry import to_openai_tools
            self.core.tool_repair._allowed_tools = set(t["function"]["name"] for t in to_openai_tools())
        try:
            _refresh_tool_repair()
            h.record_ok("tool_repair_refresh")
        except Exception as e:
            logger.debug("agent_core.reinit_tool_repair_refresh_failed error={}", str(e))
            h.record_fail("tool_repair_refresh", str(e))

        # 自动加载并启用插件（discover 已在 web/server.py 中完成）
        await self._run_optional_step("plugins", self._auto_enable_plugins)

    # ── 基础设施 ──────────────────────────────────────────

    def _get_bundled_assets_dir(self) -> Path:
        """获取安装包内置 assets 目录"""
        try:
            import sys
            if getattr(sys, 'frozen', False):
                return Path(sys._MEIPASS) / "assets"
            return Path(__file__).resolve().parent.parent / "assets"
        except Exception:
            logger.debug("bootstrap.bundled_assets_dir_fallback: {}", exc_info=True)
            return Path(__file__).resolve().parent.parent / "assets"

    def _ensure_voice_refs(self) -> None:
        """首次运行时将参考音频从安装包复制到用户数据目录"""
        import shutil
        from config import VOICE_REF_DIR
        bundled_dir = self._get_bundled_assets_dir() / "voice_refs"
        if not bundled_dir.exists():
            return
        VOICE_REF_DIR.mkdir(parents=True, exist_ok=True)
        for filename in ("xiaoda_hq.wav", "xiaoda.wav", "xiaoli.mp3"):
            stem = filename.rsplit(".", 1)[0].lower()
            agent_name = None
            for prefix in ("xiaoda", "xiaoli", "xiaoke", "xiaolian", "xiaolang"):
                if stem.startswith(prefix):
                    agent_name = prefix
                    break
            if not agent_name:
                continue
            agent_dir = VOICE_REF_DIR / agent_name
            agent_dir.mkdir(parents=True, exist_ok=True)
            dest = agent_dir / filename
            if not dest.exists():
                src = bundled_dir / filename
                if src.exists():
                    try:
                        shutil.copy2(src, dest)
                        logger.info("bootstrap.voice_ref_copied", file=filename, agent=agent_name)
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
                        except Exception:
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
                        except Exception:
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
        if getattr(core, "local_ai_instances", None) is None:
            from local_ai.devices.registry import DeviceRegistry
            from local_ai.instances.manager import InstanceManager
            from local_ai.models.registry import ModelRegistry
            from local_ai.runtimes.registry import RuntimeRegistry

            core.local_ai_instances = InstanceManager(
                ModelRegistry(core.db),
                DeviceRegistry(),
                RuntimeRegistry(),
            )
        # 恢复常驻本地推理（backend=local 节点绑定的模型实例）：
        # 必须在下方 vector_store.init() 之前完成——managed embedding 服务的
        # 维度解析依赖已启动的实例；实例未就绪时 resolve 不到 runtime，
        # 维度兜底 512 会与 1024 维向量库冲突（local_deploy mode=local 时 init_failed）。
        # server.py 启动后还有一次幂等恢复（同一 model_id 已运行则复用）。
        try:
            from web.config_service import get_config_service
            from web.local_deploy_nodes import restore_local_instances

            await restore_local_instances(core, get_config_service())
            logger.info("bootstrap.local_instances_restored")
        except Exception as e:  # noqa: BLE001
            logger.warning("bootstrap.local_instances_restore_failed error={}", str(e))
        from llm_gateway.transports import LocalOrtTransport
        from local_ai.integration.chat import LocalChatService

        local_chat = LocalChatService.managed(core.local_ai_instances)
        core.router.set_local_transport(LocalOrtTransport(
            local_chat,
            "local-chat",
            stream_context_factory=lambda request: request.extra["route"],
        ))
        # 远程嵌入 Key：与文内其他 siliconflow 服务一致（SILICONFLOW_API_KEY 优先
        # 兼容 setup_wizard，EMBED_API_KEY 为旧别名）；两者都缺时由 VectorStore
        # 内自动降级本地模型（见 vector_store.embed_fallback_to_local）。
        embed_api_key = (os.getenv("EMBED_API_KEY", "")
                         or os.getenv("SILICONFLOW_API_KEY", ""))
        embed_base_url = os.getenv("EMBED_BASE_URL", SILICONFLOW_BASE_URL)
        # 默认 remote：检索（embedding）走 SiliconFlow 远程 API（模型不再随包内置，
        # 见 docs/repo_hygiene_notes.md）。EMBED_MODE=local 仍可强制本地推理。
        # WebUI 本地部署页持久化的引擎模式优先（webui_overrides.json local_deploy.mode）
        embed_mode = os.getenv("EMBED_MODE", "remote")
        try:
            import json as _json
            from config import get_config_dir
            _ov_path = Path(get_config_dir()) / "webui_overrides.json"
            if _ov_path.exists():
                _ov = _json.loads(_ov_path.read_text(encoding="utf-8"))
                _ld = (_ov or {}).get("local_deploy", {})
                if isinstance(_ld, dict) and _ld.get("mode") in ("local", "remote"):
                    embed_mode = _ld["mode"]
                    # 持久化失效兜底（2026-08-13 用户规则）：mode=local 要求
                    # 「功能节点里选了本地模型」（引擎启动为运行时状态，由
                    # restore_local_instances 负责）。若没有任何节点 backend=local
                    # （历史残留 / 手动编辑）→ 持久化失效，回退 remote 默认 API。
                    if embed_mode == "local":
                        _nodes = _ld.get("nodes")
                        if not isinstance(_nodes, dict) or "local" not in {
                            str(v).lower() for v in _nodes.values()
                        }:
                            embed_mode = "remote"
                            logger.info(
                                "bootstrap.local_deploy_mode_ignored no_local_node "
                                "mode=local->remote")
                    logger.info("bootstrap.local_deploy_mode_applied mode={}", embed_mode)
                # 算力设备持久化：WebUI「本地部署 → 算力设备检测」选择后重启生效
                if isinstance(_ld, dict) and _ld.get("device") in ("cpu", "npu"):
                    os.environ["LOCAL_EMBED_BACKEND"] = _ld["device"]
                    logger.info("bootstrap.local_deploy_device_applied device={}", _ld["device"])
        except Exception as e:  # noqa: BLE001
            logger.debug("bootstrap.local_deploy_mode_read_failed error={}", str(e))
        core._vec_store = None
        if embed_mode == "local" or embed_api_key:
            try:
                from memory.vector_store import _default_local_model_dir

                memory_services = await _local_memory_services(
                    core.local_ai_instances,
                    embed_mode,
                    _default_local_model_dir(),
                    os.getenv("LOCAL_EMBED_QUERY_PREFIX", ""),
                )
                core._vec_store = VectorStore(
                    db_path=str(core.db.db_path.parent / (core.db.db_path.stem + "_vec.db")),
                    embed_api_key=embed_api_key,
                    embed_base_url=embed_base_url,
                    embed_mode=embed_mode,
                    embedding_service=memory_services.embedding,
                )
                await core._vec_store.init()
                logger.info("vector_store.enabled" +
                            (f" mode={embed_mode}" if embed_mode == "local" else ""))
            except Exception as e:
                logger.warning("vector_store.init_failed mode={} error={}", embed_mode, str(e))
                core._vec_store = None
        else:
            # 默认 remote 且未配置任何远程 Key：向量库不创建（记忆检索整体禁用）。
            # 明确提示而非静默；用户配置 SILICONFLOW_API_KEY 后重启即可。
            logger.warning(
                "vector_store.skipped mode=remote api_key=MISSING "
                "记忆检索不可用：请配置 SILICONFLOW_API_KEY/EMBED_API_KEY，"
                "或设置 EMBED_MODE=local 并放置本地 bge 模型后重启")

    # ── 认知系统 ──────────────────────────────────────────

    async def _init_cognitive(self) -> None:
        """初始化认知系统：Reranker、QueryTransformer、Memory、KG、Instinct、ErrorPipeline。"""
        from memory.memory_manager import MemoryManager
        from memory.knowledge_graph import KnowledgeGraph
        from memory.notebook_manager import NotebookManager
        from memory.learning_manager import LearningManager
        from emotion.portrait_manager import PortraitManager
        import config

        core = self.core

        # 1. Reranker + QueryTransformer（硅基流动免费模型，可按节点选择本地/API）
        from web.config_service import get_config_service
        from web.local_deploy_nodes import get_backend
        _cfg_svc = get_config_service()

        reranker = self._init_reranker(config)
        from local_ai.integration.reranker import LocalRerankerService
        from memory.vector_store import _default_local_model_dir

        memory_services = await _local_memory_services(
            core.local_ai_instances,
            getattr(core._vec_store, "_embed_mode", os.getenv("EMBED_MODE", "local")),
            _default_local_model_dir(),
            os.getenv("LOCAL_EMBED_QUERY_PREFIX", ""),
            remote_reranker=reranker,
        )
        # 应用 reranker 节点后端选择（auto/local/api/off）
        if isinstance(memory_services.reranker, LocalRerankerService):
            memory_services.reranker.set_backend(get_backend(_cfg_svc, "reranker"))
        query_transformer = self._init_query_transformer(config, router=core.router)

        # 2. Memory + KnowledgeGraph
        core.memory = MemoryManager(
            db=core.db,
            memory=core.db.memory,
            vector_store=core._vec_store,
            router=core.router,
            reranker=memory_services.reranker,
            reranker_service=(
                memory_services.reranker
                if isinstance(memory_services.reranker, LocalRerankerService)
                else None
            ),
            query_transformer=query_transformer,
        )
        # 启动时对账：检测主表已落盘但向量索引缺失的记忆（fire-and-forget，不阻塞启动）
        try:
            _spawn(core.memory.reconcile_vector_index_gap())
        except Exception as e:
            logger.warning("bootstrap.vector_reconcile_spawn_failed", error=str(e))
        # ContextNest A2/A3: 注入上下文治理 (哈希链 + 审计追踪)
        try:
            from memory.context_governance import ContextGovernance
            governance = ContextGovernance(conn=core.db._conn)
            core.memory.set_governance(governance)
        except Exception as e:
            logger.warning("bootstrap.governance_init_failed", error=str(e))
        core.knowledge_graph = KnowledgeGraph(db=core.db, knowledge_db=core.db.knowledge, router=core.router)
        sf_key = os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("EMBED_API_KEY", "")
        kg_backend = get_backend(get_config_service(), "kg_extract")
        if kg_backend == "off":
            core.knowledge_graph.set_backend("off")
        elif kg_backend == "local":
            core.knowledge_graph.set_backend("local")
        elif sf_key:
            core.knowledge_graph.set_free_model_client(
                api_key=sf_key,
                base_url=SILICONFLOW_BASE_URL,
                model=SILICONFLOW_FREE_MODEL,
            )
        core.memory.set_knowledge_graph(core.knowledge_graph)

        # KG v2 注入（功能开关开启时启用 v2 路径，失败降级到 v1）
        if getattr(config, 'KG_V2_ENABLED', False):
            try:
                from memory.knowledge_graph_v2 import KnowledgeGraphV2
                from memory.kg_search import KGSearchEngine
                kg_v2 = KnowledgeGraphV2(
                    db_v2=core.db.kg_v2,
                    vector_store=core._vec_store,
                    router=core.router,
                )
                # 复用 v1 的免费模型配置，确保 KG v2 提取也走相同后端
                kg_v2_set_backend = getattr(kg_v2, "set_backend", None)
                if kg_backend == "off" and kg_v2_set_backend is not None:
                    kg_v2_set_backend("off")
                elif kg_backend == "local" and kg_v2_set_backend is not None:
                    kg_v2_set_backend("local")
                elif sf_key:
                    kg_v2.set_free_model_client(
                        api_key=sf_key,
                        base_url=SILICONFLOW_BASE_URL,
                        model=SILICONFLOW_FREE_MODEL,
                    )
                core.knowledge_graph.set_kg_v2(kg_v2)
                kg_search_engine = KGSearchEngine(
                    db=core.db.kg_v2,
                    vector_store=core._vec_store,
                    conn=core.db._conn,
                )
                core.memory.set_kg_v2_engine(kg_search_engine)
                logger.info("kg_v2.enabled")
            except Exception as e:
                logger.warning("kg_v2.init_failed_fallback_to_v1", error=str(e))

        if core._failure_trigger._memory_db is None:
            core._failure_trigger._memory_db = core.memory.memory
        # 注入 MemoryManager 到 memory_tool，修复记忆工具不可用问题
        from tools import memory_tool
        memory_tool.bind(core.memory)
        from tools import profile_tool
        profile_tool.bind(core.db.profiles)
        from core.profile_context import ProfileContextProvider
        core.context.profile_context_provider = ProfileContextProvider(core.db.profiles)
        # 注入 core 到 schedule_tool，让 Agent 能查询/修改/删除提醒
        from tools import schedule_tool
        schedule_tool.bind(core)
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

        # 5. 情景记忆行数限制器（H1）：接入定期清理，防止 episodic_memories 无限增长
        try:
            from memory.episodic_limiter import get_episodic_limiter
            _limiter = get_episodic_limiter(core.db)
            if _limiter is not None:
                _limiter.start_scheduler(interval=3600)
                logger.info("episodic_limiter.scheduler_started",
                            max_rows=_limiter.stats()["max_rows"])
        except Exception as e:
            logger.warning("episodic_limiter.start_failed", error=str(e))

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
    def _init_query_transformer(config: Any, router: Any = None) -> Any:
        """初始化 QueryTransformer（硅基流动免费模型，可按节点选本地=主 LLM）。"""
        from memory.query_transform import QueryTransformer
        from web.config_service import get_config_service
        from web.local_deploy_nodes import get_backend
        if not getattr(config, "QUERY_TRANSFORM_ENABLED", True):
            logger.info("query_transformer.disabled_by_config")
            return None
        backend = get_backend(get_config_service(), "query_transform")
        if backend == "off":
            logger.info("query_transformer.disabled_by_node_off")
            return QueryTransformer(router=router, backend="off")
        if backend == "local":
            if router is None:
                logger.info("query_transformer.disabled_no_router")
                return None
            logger.info("query_transformer.local_router_mode")
            return QueryTransformer(router=router, backend="local")
        qt_api_key = os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("EMBED_API_KEY", "")
        if not qt_api_key:
            logger.info("query_transformer.disabled_no_api_key")
            return None
        logger.info("query_transformer.enabled", model=f"{SILICONFLOW_FREE_MODEL} (free)")
        return QueryTransformer(
            router=router,
            api_key=qt_api_key,
            base_url=SILICONFLOW_BASE_URL,
            backend=backend,
        )

    async def _init_instinct_and_pipeline(self, core: Any, sf_key: str) -> None:
        """初始化 InstinctManager 和 ErrorRulePipeline，并注入 ToolCallHandler。"""
        from instinct_manager import InstinctManager
        core.instinct_manager = InstinctManager(db=core.db, router=core.router)
        await core.instinct_manager.init()
        # Instinct 提取：按节点后端选择注入免费模型（local=本地模型 / off=禁用）
        from web.config_service import get_config_service
        from web.local_deploy_nodes import get_backend
        instinct_backend = get_backend(get_config_service(), "instinct")
        if instinct_backend == "off":
            core.instinct_manager.set_backend("off")
        elif instinct_backend == "local":
            core.instinct_manager.set_backend("local")
        elif sf_key:
            # Instinct 提取改用硅基流动免费模型（非思考模型，避免 Z1 思考碎片）
            core.instinct_manager.set_free_model_client(
                api_key=sf_key,
                base_url=SILICONFLOW_BASE_URL,
                model=SILICONFLOW_FREE_MODEL,
            )
        # 加载 Instinct 提示到上下文
        instinct_prompt = await core.instinct_manager.build_instinct_prompt()
        if instinct_prompt:
            core.context.instinct_prompt = instinct_prompt
        logger.info("instinct_manager.initialized")

        # P5: 失败经验→规则闭环（可选组件，失败安全）
        # 先构造到局部变量，全部成功（构造 + set_backend）后再赋给 core
        # 与注入 handler——异常时 core.error_pipeline 保持原值（None），
        # 不会留下半初始化对象被别处读到。
        try:
            from tool_engine.error_rule_pipeline import ErrorRulePipeline
            pipeline = ErrorRulePipeline(db=core.db, router=core.router)
            error_backend = get_backend(get_config_service(), "error_rule")
            if error_backend == "off":
                pipeline.set_backend("off")
            elif error_backend == "local":
                pipeline.set_backend("local")
            elif sf_key:
                pipeline.set_free_model_client(
                    api_key=sf_key,
                    base_url=SILICONFLOW_BASE_URL,
                    model=SILICONFLOW_FREE_MODEL,
                )
            # 构造与后端配置均成功，原子提交到 core 并注入 handler
            core.error_pipeline = pipeline
            if getattr(core, "_tool_call_handler", None) is not None:
                core._tool_call_handler.set_error_pipeline(pipeline)
            logger.info("error_rule_pipeline.initialized")
        except Exception as e:
            logger.warning("error_rule_pipeline.init_failed", error=str(e))

    # ── 子代理注册 ────────────────────────────────────────

    async def _register_sub_agents(self) -> None:
        from agent_dispatcher import SubAgentConfig
        import config as _cfg_mod
        # 同函数内 AGENTS_CONFIG_DIR 有 ImportError 兜底，此符号同等对待：
        # config 重构删除常量时不致启动崩溃（兜底取源头定义 config_paths）
        XIAOLI_STICKER_DIR = getattr(_cfg_mod, "XIAOLI_STICKER_DIR", None)
        if XIAOLI_STICKER_DIR is None:
            from config_paths import XIAOLI_STICKER_DIR as _xsd
            XIAOLI_STICKER_DIR = _xsd
        # frozen 模式下使用用户目录中的 agents 配置（_init_user_resources 已复制模板）
        try:
            from config import AGENTS_CONFIG_DIR as _agents_dir
        except ImportError:
            _agents_dir = Path(__file__).resolve().parent.parent / "config" / "agents"

        core = self.core
        _prov_cfg = _get_provider_config(_DEFAULT_PROVIDER)
        _agent_model = _PRO_MODEL or _MODEL_NAME

        # P0 修复（2026-08-04 实证）：启动时从 config/agents/{name}.json 读取持久化的
        # provider/model/base_url/api_key_env，否则硬编码 _DEFAULT_PROVIDER 会覆盖
        # 用户通过 WebUI 切换的模型选择 → 重启后子 agent 被重置到默认 mimo。
        # 根因：update() 已持久化到文件（文件里确为 agnes），但 _register_sub_agents
        # 从未读取该文件，一直用 _DEFAULT_PROVIDER 构造 SubAgentConfig。
        import json as _json

        def _load_persisted(_name: str) -> dict:
            """读取 config/agents/{name}.json 持久化配置；缺失字段返回空。"""
            _fp = _agents_dir / f"{_name}.json"
            try:
                if _fp.exists():
                    return _json.loads(_fp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logger.debug("bootstrap.agent_persist_load_failed name={}", _name, exc_info=True)
            return {}

        def _resolved_provider(_name: str) -> str:
            return _load_persisted(_name).get("provider") or _DEFAULT_PROVIDER

        def _resolved_model(_name: str) -> str:
            return _load_persisted(_name).get("model") or _agent_model

        def _resolved_base_url(_name: str) -> str:
            _p = _load_persisted(_name)
            if _p.get("base_url"):
                return _p["base_url"]
            # provider 变更后自动解析 base_url/api_key_env，与 update() 逻辑一致
            _prov = _p.get("provider") or _DEFAULT_PROVIDER
            try:
                _cfg = _get_provider_config(_prov)
                return _cfg["base_url"]
            except (KeyError, ValueError):
                return _prov_cfg["base_url"]

        def _resolved_api_key_env(_name: str) -> str:
            _p = _load_persisted(_name)
            if _p.get("api_key_env"):
                return _p["api_key_env"]
            _prov = _p.get("provider") or _DEFAULT_PROVIDER
            try:
                _cfg = _get_provider_config(_prov)
                return _cfg["api_key_env"]
            except (KeyError, ValueError):
                return _prov_cfg["api_key_env"]

        # P0 修复（2026-08-04）：确保 nahida-data 的 personality 文件有完整内容。
        # 运行时 _load_personality（agent_dispatcher.py:194）直接读 agent.config.personality_file
        # （nahida-data 路径），不经过 _resolve_personality_path。若文件被空内容覆盖（3字节 BOM），
        # read_text 返回空 → 降级为兜底 "你是{display_name}。" → 用户自定义人格丢失。
        # 修复：启动时检查文件，空了就从源码 config/agents/ 恢复；有内容则保留用户自定义。
        import shutil as _shutil
        _src_agents_dir = Path(__file__).resolve().parent.parent / "config" / "agents"

        def _ensure_personality_file(_name: str) -> str:
            _target = _agents_dir / f"{_name}_personality.md"
            if _target.exists() and _target.stat().st_size > 3:
                return str(_target)  # 用户已有内容，保留
            _src = _src_agents_dir / f"{_name}_personality.md"
            if _src.exists():
                _agents_dir.mkdir(parents=True, exist_ok=True)
                _shutil.copy2(_src, _target)
                logger.info("bootstrap.personality_restored name={} bytes={}",
                            _name, _target.stat().st_size)
            return str(_target)

        xiaoli_config = SubAgentConfig(
            name="xiaoli",
            display_name=get_agent_display_name("xiaoli"),
            provider=_resolved_provider("xiaoli"),
            model=_resolved_model("xiaoli"),
            personality_file=_ensure_personality_file("xiaoli"),
            voice_ref="xiaoli",
            excluded_tools={"call_xiaoli", "shell_command", "python_executor", "write_file", "search_files", "read_file", "list_files", "web_browse", "document_reader", "multi_search", "wolfram_query"},
            base_url=_resolved_base_url("xiaoli"),
            api_key_env=_resolved_api_key_env("xiaoli"),
            capabilities=["chat", "play", "fun"],
            route_description="日常聊天、玩耍、轻松有趣的对话",
            sticker_dir=str(XIAOLI_STICKER_DIR),
            wallpaper=_load_persisted("xiaoli").get("wallpaper") or "",
        )
        await core.dispatcher.register(xiaoli_config)
        xiaolang_config = SubAgentConfig(
            name="xiaolang",
            display_name=get_agent_display_name("xiaolang"),
            provider=_resolved_provider("xiaolang"),
            model=_resolved_model("xiaolang"),
            personality_file=_ensure_personality_file("xiaolang"),
            voice_ref=None,
            excluded_tools={"call_xiaoli", "call_xiaoda", "delegate_task"},
            base_url=_resolved_base_url("xiaolang"),
            api_key_env=_resolved_api_key_env("xiaolang"),
            capabilities=["coding", "debug", "script", "programming", "hardware", "system", "devops"],
            route_description="编程、代码编写、调试、技术问题、硬件控制、系统运维、开发辅助",
            # 默认关闭 git/github MCP：首次安装不再自动启用（需在 WebUI 权限矩阵按需开启）
            mcp_servers=[],
            wallpaper=_load_persisted("xiaolang").get("wallpaper") or "",
        )
        await core.dispatcher.register(xiaolang_config)
        xiaolian_config = SubAgentConfig(
            name="xiaolian",
            display_name=get_agent_display_name("xiaolian"),
            provider=_resolved_provider("xiaolian"),
            model=_resolved_model("xiaolian"),
            personality_file=_ensure_personality_file("xiaolian"),
            voice_ref=None,
            excluded_tools={"call_xiaoli", "call_xiaoda", "delegate_task", "shell_command", "python_executor", "write_file"},
            base_url=_resolved_base_url("xiaolian"),
            api_key_env=_resolved_api_key_env("xiaolian"),
            capabilities=["search", "lookup", "query", "explore", "discover"],
            route_description="搜索信息、查询资料、探索发现",
            wallpaper=_load_persisted("xiaolian").get("wallpaper") or "",
        )
        await core.dispatcher.register(xiaolian_config)
        xiaoke_config = SubAgentConfig(
            name="xiaoke",
            display_name=get_agent_display_name("xiaoke"),
            provider=_resolved_provider("xiaoke"),
            model=_resolved_model("xiaoke"),
            personality_file=_ensure_personality_file("xiaoke"),
            voice_ref=None,
            excluded_tools={"call_xiaoli", "call_xiaoda", "delegate_task", "shell_command", "write_file"},
            base_url=_resolved_base_url("xiaoke"),
            api_key_env=_resolved_api_key_env("xiaoke"),
            capabilities=["research", "analysis", "study", "academic"],
            route_description="研究分析、学术思考、深度解读",
            wallpaper=_load_persisted("xiaoke").get("wallpaper") or "",
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
        from model_router import _resolve_provider_key
        from config import get_base_url_for_provider

        core = self.core
        # 统一凭证读取口径：enc:v1: 密文自动解密，避免把密文当 Key → 401
        _key = _resolve_provider_key("MIMO_API_KEY")
        _url = get_base_url_for_provider("mimo")
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

    @staticmethod
    def _sanitize_mcp_configs(
        all_servers: dict[str, Any],
    ) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        """MCP server 启动前的 command/env 纵深防御校验（纯函数，可单测）。

        存量 mcp_configs/*.json 可能在被投毒后残留于磁盘，若不经校验直接
        start_all 会在每次启动时执行任意命令（RCE）。本函数对每个 stdio
        server 复用 security.mcp_command_policy 的二进制白名单 + env 键黑名单
        校验，校验失败的 server 剔除（fail-closed，跳过不启动）。

        非 stdio 传输（sse/streamable-http）不启动本地二进制，不做 command/env
        校验，原样保留。

        Returns:
            (通过校验的 {name: cfg}, [(name, reason), ...]) —— 后者为被拒绝的
            server 名与原因，供调用方记 warning 与审计日志。
        """
        from security.mcp_command_policy import (
            validate_mcp_command,
            validate_mcp_env,
        )

        clean: dict[str, Any] = {}
        rejected: list[tuple[str, str]] = []
        for name, cfg in (all_servers or {}).items():
            if not isinstance(cfg, dict):
                rejected.append((str(name), "配置不是 JSON 对象"))
                continue
            if cfg.get("transport", "stdio") != "stdio":
                # 远程传输（sse/streamable-http）不执行本地二进制
                clean[name] = cfg
                continue
            try:
                validate_mcp_command(cfg.get("command"))
                validate_mcp_env(cfg.get("env"))
            except (ValueError, TypeError) as exc:
                rejected.append((str(name), str(exc)))
                continue
            clean[name] = cfg
        return clean, rejected

    async def _init_mcp(self) -> None:
        import json as _json
        from config import MCP_SERVERS, WORKSPACE_DIR

        core = self.core

        # 只启动被某个已注册 agent 引用的 MCP server（默认关闭 git/github：
        # 无 agent 启用则不启动，避免首次安装默认空跑无用子进程）
        referenced: set[str] = set()
        for _agent in getattr(getattr(core, "dispatcher", None), "_agents", {}).values():
            _cfg = getattr(_agent, "config", None)
            if _cfg is not None:
                referenced.update(getattr(_cfg, "mcp_servers", None) or [])

        # 合并配置文件中的 MCP 服务器 + 市场安装的 MCP 服务器
        all_servers: dict[str, Any] = {}
        if MCP_SERVERS:
            # 内置 server（git/github）仅在被 agent 引用时启动
            for _sname, _scfg in MCP_SERVERS.items():
                if _sname in referenced:
                    all_servers[_sname] = _scfg

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
            # 纵深防御：存量配置投毒防护 —— 校验失败的 server 跳过不启动
            clean_servers, rejected = self._sanitize_mcp_configs(all_servers)
            for name, reason in rejected:
                logger.warning("mcp.config_rejected", server=name, reason=reason)
                try:
                    await core.db.insert_audit_log(
                        "bootstrap.mcp_config_rejected",
                        "bootstrap",
                        _json.dumps({"server": name, "reason": reason},
                                    ensure_ascii=False),
                    )
                except Exception:
                    # 审计失败（db 不可用等）不阻断启动，但提级为 warning：
                    # 这是安全审计路径（被 sanitize 拒绝的 server），审计失败
                    # 会让攻击痕迹静默丢失，需要可见性。
                    logger.warning("bootstrap.mcp_audit_failed: {}", exc_info=True)
            if clean_servers:
                try:
                    await core._mcp_manager.start_all(clean_servers)
                    logger.info("mcp.servers_started", count=len(core._mcp_manager._clients))
                except Exception as e:
                    # 补充 clean/rejected 计数：排查时区分「全部被 sanitize 拒」
                    # 与「启动真崩」。sanitize 拒绝的 server 已在上方逐条 warn。
                    logger.warning("mcp.start_failed error={} clean={} rejected={}",
                                   str(e), len(clean_servers), len(rejected))

    # ── 插件自动启用 ──────────────────────────────────────

    async def _auto_enable_plugins(self) -> None:
        """自动加载并启用已发现的插件。

        PluginManager.discover() 在 web/server.py lifespan 中完成后会注册到
        plugins.manager 的中立注册点；此处经 get_active_plugin_manager() 获取
        并对所有已发现的插件执行 load + enable，使插件注册的工具对 LLM 可见。
        """
        from plugins.manager import get_active_plugin_manager
        plugin_mgr = get_active_plugin_manager()
        if not plugin_mgr:
            logger.warning("plugins.no_active_manager hint=web层lifespan未运行或降级模式")
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
    return Path(__file__).resolve().parent.parent
