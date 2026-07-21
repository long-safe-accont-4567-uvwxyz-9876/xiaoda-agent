from typing import Any
from collections.abc import AsyncIterator
import asyncio
import os
import sys
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from loguru import logger

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).parent.parent))


async def _apply_model_overrides(core: Any) -> None:
    """重启后恢复：自定义 provider 注册 + 路由表覆盖。"""
    import os
    from web.config_service import get_config_service
    from web.custom_providers import register_into_router
    from web.routers.models import load_provider_key
    from model_router import ROUTE_TABLE

    logger.info("webui._apply_model_overrides_start")
    cfg = get_config_service()

    # 从 .env 文件读取，而非 os.environ，防止构建环境变量泄露到用户安装包
    try:
        from setup_wizard import _load_env_values
        env_values = _load_env_values()
    except (ImportError, OSError, ValueError):
        logger.debug("server.load_env_error", exc_info=True)
        env_values = {}

    _register_env_providers(cfg, env_values, os)
    _register_all_providers(cfg, core, load_provider_key, register_into_router)
    logger.info("webui.before_apply_route_overrides")
    _apply_route_overrides(cfg, core, ROUTE_TABLE)
    logger.info("webui.after_apply_route_overrides")
    _restore_chat_model(cfg, core)


def _register_env_providers(cfg: Any, env_values: Any, os_module: Any) -> None:
    """从 .env 注册已知免费模型平台 provider。"""
    _KNOWN_ENV_PROVIDERS = {
        "SILICONFLOW_API_KEY": ("siliconflow", "openai", "https://api.siliconflow.cn/v1", "SiliconFlow 硅基流动"),
        "OPENROUTER_API_KEY": ("openrouter", "openai", "https://openrouter.ai/api/v1", "OpenRouter"),
        "MODELSCOPE_ACCESS_TOKEN": (
            "modelscope", "openai",
            "https://api-inference.modelscope.cn/v1", "ModelScope 魔搭"
        ),
        "AGNES_API_KEY": (
            "agnes", "openai",
            os.getenv("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1"), "Agnes AI"
        ),
        "OLLAMA_BASE_URL": (
            "ollama", "openai",
            os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"), "Ollama 本地大模型"
        ),
    }
    known_env_keys = list(_KNOWN_ENV_PROVIDERS.keys())
    for env_key, (pid, fmt, _default_url, label) in _KNOWN_ENV_PROVIDERS.items():
        if env_key == "OLLAMA_BASE_URL":
            api_key = "ollama"
            base_url = env_values.get(env_key, "").strip() or _default_url
            if not base_url:
                continue
        else:
            api_key = env_values.get(env_key, "").strip()
            base_url = _default_url
            if not api_key:
                continue
        existing = cfg.get("models.providers", {}) or {}
        if pid not in existing:
            cfg.set(f"models.providers.{pid}", {
                "label": label, "format": fmt, "base_url": base_url,
                "default_model": "", "enabled": True,
                "order": known_env_keys.index(env_key),
            })
        _ensure_provider_key_file(pid, api_key, os_module)


def _ensure_provider_key_file(pid: Any, api_key: Any, os_module: Any) -> None:
    """确保证书文件存在且内容正确（base64 编码存储，非明文）。"""
    from config import get_credentials_dir
    from web._provider_keys import _encode_key, _decode_key
    cred_dir = get_credentials_dir()
    cred_dir.mkdir(parents=True, exist_ok=True)
    fp = cred_dir / f"provider_{pid}.key"
    # 读取现有值（兼容旧版明文）
    existing = ""
    if fp.exists():
        raw = fp.read_text(encoding="utf-8").strip()
        existing = _decode_key(raw) or raw if raw else ""
    if existing != api_key:
        fp.write_text(_encode_key(api_key) + "\n", encoding="utf-8")
        with suppress(OSError):
            os.chmod(fp, 0o600)


def _provider_sort_key(kv: tuple, key_order: list[str]) -> tuple[int, int]:
    """provider 排序键: order 字段优先, 原始键序兜底."""
    return (kv[1].get("order", 9999), key_order.index(kv[0]))


def _register_all_providers(cfg: Any, core: Any, load_provider_key: Any, register_into_router: Any) -> None:
    """按 order 字段排序后注册所有 provider 到 router 和 credential_pool。"""
    all_providers = cfg.get("models.providers", {}) or {}
    all_keys_order = list(all_providers.keys())
    sorted_providers = sorted(
        all_providers.items(),
        key=lambda kv: _provider_sort_key(kv, all_keys_order)
    )
    for pid, p in sorted_providers:
        key = load_provider_key(pid)
        if key and p.get("enabled", True):
            try:
                register_into_router(core.router, pid, p.get("format", "openai"),
                                     p.get("base_url", ""), key)
                from utils.credential_pool import get_credential_pool, Credential
                pool = get_credential_pool()
                if pid not in pool._pool:
                    pool.add_credential(Credential(
                        api_key=key, provider=pid, base_url=p.get("base_url", ""),
                    ))
            except (ImportError, KeyError, ValueError, OSError) as e:
                logger.warning("webui.provider_restore_failed id={} error={}", pid, str(e))


def _apply_route_overrides(cfg: Any, core: Any, ROUTE_TABLE: Any) -> None:
    """应用路由表覆盖（model/client/max_tokens/thinking/timeout）。"""
    routes_config = cfg.get("models.routes", {}) or {}
    logger.info("webui.route_overrides_start total_tasks={}", len(routes_config))
    for task, o in routes_config.items():
        entry = ROUTE_TABLE.get(task)
        if not entry or not isinstance(o, dict):
            logger.warning("webui.route_override_skip task={} reason=no_entry_or_invalid", task)
            continue
        if o.get("model"):
            entry["model"] = o["model"]
        if o.get("client"):
            entry["client"] = o["client"]
        if o.get("max_tokens"):
            entry["max_tokens"] = o["max_tokens"]
        if "thinking" in o:
            original_thinking = entry.get("thinking")
            if o["thinking"]:
                entry["thinking"] = {"type": "enabled", "budget_tokens": 2048}
            else:
                entry["thinking"] = {"type": "disabled"}
            logger.info("webui.thinking_loaded task={} original={} new={}",
                        task, original_thinking, entry.get("thinking"))
        if o.get("timeout"):
            core.router.TASK_TIMEOUTS[task] = o["timeout"]


def _restore_chat_model(cfg: Any, core: Any) -> None:
    """恢复上次聊天模型（从 config_service 的 models.chat_model 读取）。

    注意：此函数只做"恢复"，不做"持久化"。fallback 时禁止调用 set_chat_model，
    否则会把 mimo 重新写入 config，覆盖用户原选择，形成 sticky fallback。

    关键修复：fallback 分支不再修改 ROUTE_TABLE["chat"]["client"]。
    旧逻辑会把 client 改成 "mimo"，后续 PUT /models/routes/chat 会读取这个内存值
    并持久化 chat_model=mimo，导致用户明明设置 agnes 却被覆盖成 mimo（sticky fallback）。
    正确行为：fallback 只更新 _current_chat_model（影响 GET /models/chat-model 返回值），
    保留 ROUTE_TABLE 中的用户原选择。请求路由时会通过 _select_client_for_provider
    检测到 agnes 客户端不可用，自然走 fallback 链到 mimo，无需污染 ROUTE_TABLE。
    """
    chat_model = cfg.get("models.chat_model")
    if not (isinstance(chat_model, dict) and chat_model.get("provider") and chat_model.get("model_id")):
        logger.info("webui.chat_model_no_saved_preference, using default")
        return
    provider = chat_model["provider"]
    model_id = chat_model["model_id"]
    # 检查 ROUTE_TABLE["chat"] 当前值（可能已被 _apply_route_overrides 修改）
    from model_router import ROUTE_TABLE
    current_client = ROUTE_TABLE.get("chat", {}).get("client", "")
    current_model = ROUTE_TABLE.get("chat", {}).get("model", "")
    logger.info("webui.chat_model_restore_attempt saved={}/{} current_route={}/{}",
                provider, model_id, current_client, current_model)
    try:
        core.router.set_chat_model(provider, model_id)
        logger.info("webui.chat_model_restored provider={} model={}", provider, model_id)
    except Exception as e:
        # 必须捕获 Exception：LLMError 继承 AppException 不在原 (KeyError, ValueError,
        # AttributeError, OSError) 范围内，自定义 provider 注册失败时会导致启动崩溃
        logger.warning(
            "webui.chat_model_restore_failed provider={} model={} "
            "error={} keep_route_table_unchanged", provider, model_id, str(e)
        )
        # 关键修复：只更新 _current_chat_model（GET /models/chat-model 用），
        # 不修改 ROUTE_TABLE["chat"]["client"]，避免污染用户原选择。
        # 请求路由时 _select_client_for_provider 会检测客户端不可用并抛 LLMError，
        # 触发 _try_fallback_chain 走 mimo 降级，无需在此预先污染 ROUTE_TABLE。
        try:
            from model_router import MIMO_MODEL
            core.router._current_chat_model = {"provider": "mimo", "model_id": MIMO_MODEL}
            logger.info("webui.chat_model_fallback_current_only provider=mimo model={} "
                        "route_table_preserved={}/{}", MIMO_MODEL, current_client, current_model)
        except (ImportError, KeyError, AttributeError):
            logger.debug("server.set_chat_model_fallback_error", exc_info=True)


async def _start_user_mcp_servers(core: Any) -> None:
    """启动 WebUI 管理的 MCP server。"""
    from web.config_service import get_config_service
    from tool_engine.mcp_client import MCPClient
    cfg = get_config_service()
    for name, rec in (cfg.get("mcp", {}) or {}).items():
        if not isinstance(rec, dict) or not rec.get("enabled", True):
            continue
        if name in core._mcp_manager._clients:
            continue
        client = MCPClient(name, rec.get("command", ""),
                           rec.get("args", []), rec.get("env") or None)
        core._mcp_manager._clients[name] = client
        try:
            await client.start()
            logger.info("webui.mcp_restored name={}", name)
        except (OSError, RuntimeError, asyncio.CancelledError) as e:
            logger.warning("webui.mcp_restore_failed name={} error={}", name, str(e))


async def _init_recall_scheduler(core: Any) -> tuple[str, Any]:
    """G16: 初始化 MemoryRecallScheduler（可并行）。

    主动检索 B：定时回忆任务调度器（独立后台循环，每 3h 整理回忆笔记）。
    失败时返回 (attr_name, None)，不影响其他并行初始化的调度器。
    """
    try:
        from memory.recall_scheduler import MemoryRecallScheduler
        recall_scheduler = MemoryRecallScheduler(core)
        recall_scheduler.start()
        return ("recall_scheduler", recall_scheduler)
    except (ImportError, AttributeError, OSError) as e:
        logger.warning("webui.recall_scheduler_init_failed", error=str(e))
        return ("recall_scheduler", None)


async def _init_spontaneous_recall(core: Any) -> tuple[str, Any]:
    """G16: 初始化 SpontaneousRecall（可并行）。

    自发回忆：每小时随机想 1 条记忆，生成内心独白（让 agent 有"内心生活"）。
    失败时返回 (attr_name, None)。
    """
    try:
        from core.spontaneous_recall import SpontaneousRecall
        spontaneous = SpontaneousRecall(core)
        spontaneous.start()
        return ("spontaneous_recall", spontaneous)
    except (ImportError, AttributeError, OSError) as e:
        logger.warning("webui.spontaneous_recall_init_failed", error=str(e))
        return ("spontaneous_recall", None)


async def _init_growth_narrative(core: Any) -> tuple[str, Any]:
    """G16: 初始化 GrowthNarrative（可并行）。

    成长叙事：每天 23:00 生成成长总结，写入自我模型和长期记忆。
    失败时返回 (attr_name, None)。
    """
    try:
        from core.growth_narrative import GrowthNarrative
        growth = GrowthNarrative(core)
        growth.start()
        return ("growth_narrative", growth)
    except (ImportError, AttributeError, OSError) as e:
        logger.warning("webui.growth_narrative_init_failed", error=str(e))
        return ("growth_narrative", None)


async def _init_mail_poller(core: Any, config_service: Any) -> tuple[str, Any]:
    """G16: 初始化 MailPoller（可并行）。

    邮件机器人轮询器（后台循环，检测新邮件→注入 Agent→邮件回复）。
    失败时返回 (attr_name, None)。
    """
    try:
        from web.mail_poller import MailPoller
        mail_poller = MailPoller(core, config_service)
        mail_poller.start()
        return ("mail_poller", mail_poller)
    except (ImportError, AttributeError, OSError) as e:
        logger.warning("webui.mail_poller_init_failed error={}", str(e))
        return ("mail_poller", None)


async def _start_services(app: Any, core: Any) -> None:
    """启动正常模式下的所有服务组件（PluginManager、MediaTaskQueue、GreetingScheduler、QQ Bot）。"""
    from web.config_service import get_config_service
    from web.media_tasks import MediaTaskQueue
    from web.greeting_scheduler import GreetingScheduler
    from web.routers.tools import apply_tool_overrides
    from web.ws_hub import manager, start_media_cleanup

    await _apply_model_overrides(core)
    apply_tool_overrides()
    start_media_cleanup()
    await _start_user_mcp_servers(core)

    # Initialize Plugin Manager
    from plugins.manager import PluginManager
    plugin_manager = PluginManager(
        tool_registry=None,
        hook_engine=core._hook_engine if hasattr(core, "_hook_engine") else None,
        memory_manager=core.memory if hasattr(core, "memory") else None,
        knowledge_graph=core.kg if hasattr(core, "kg") else None,
        mcp_manager=core._mcp_manager,
        agent_core=core,
    )
    import tool_engine.tool_registry as _tool_registry_mod
    plugin_manager._tool_registry = _tool_registry_mod
    plugin_manager.discover()
    app.state.plugin_manager = plugin_manager

    queue = MediaTaskQueue(core, manager.broadcast)
    queue.start()
    app.state.media_queue = queue

    scheduler = GreetingScheduler(core, get_config_service(), manager.broadcast)
    scheduler.start()
    app.state.greeting_scheduler = scheduler

    # G16: 独立调度器并行初始化（recall/spontaneous/growth/mail）
    # 这 4 个调度器相互独立、各自 try/except 包裹（单点失败不影响其他），
    # 用 asyncio.gather 并行启动以缩短启动时间（参考 docs/performance_audit_2026-07-20.md）。
    config_service = get_config_service()
    init_results = await asyncio.gather(
        _init_recall_scheduler(core),
        _init_spontaneous_recall(core),
        _init_growth_narrative(core),
        _init_mail_poller(core, config_service),
        return_exceptions=False,  # 每个函数内部已 try/except，不会抛异常
    )
    for attr_name, instance in init_results:
        if instance is not None:
            setattr(app.state, attr_name, instance)

    # QQ Bot
    qq_task = None
    if os.getenv("QQBOT_APP_ID", "") and os.getenv("ENABLE_QQ_BOT", "true").lower() in ("true", "1", "yes"):
        from qq_bot_adapter import run_qq_bot
        from config import AGENT_CONFIG
        qq_task = asyncio.create_task(
            run_qq_bot(core, sandbox=AGENT_CONFIG.get("qq_bot", {}).get("is_sandbox", False)))
        logger.info("webui.qq_bot_task_started")
    app.state.qq_task = qq_task
    app.state.last_emotion = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[Any]:
    logger.info("webui.lifespan.start")
    try:
        core, owns_core = await _init_lifespan_resources(app)
    except RecursionError:
        # FastAPI merged_lifespan 递归溢出保护（Starlette 版本不兼容时可能触发）
        logger.error("webui.lifespan.recursion_overflow — 升险：请确认 starlette>=0.40.0")
        raise RuntimeError(
            "Lifespan 递归溢出，通常是 starlette 版本与 fastapi 不兼容。"
            "请执行: pip install 'starlette>=0.40.0' 后重启。"
        ) from None

    # 降级模式：直接读 .env 文件检查 MIMO_API_KEY
    _mimo = _resolve_env_api_key()
    if not _mimo:
        logger.info("webui.degraded_mode")
        # 初始化空的 plugin/media/scheduler 避免后续 AttributeError
        app.state.plugin_manager = None
        app.state.media_queue = None
        app.state.greeting_scheduler = None
        app.state.qq_task = None
        app.state.last_emotion = None
        logger.info("webui.lifespan.ready_degraded")
    else:
        await _start_services(app, core)
        logger.info("webui.lifespan.ready")

    yield

    logger.info("webui.lifespan.shutdown")
    await _shutdown_lifespan(app, core, owns_core)


async def _init_lifespan_resources(app: FastAPI) -> tuple[Any, bool]:
    """初始化 core、配置服务与 agent registry, 返回 (core, owns_core)"""
    from agent_core import AgentCore
    from web.agent_registry import AgentRegistry
    from web.config_service import get_config_service

    core = getattr(app.state, "core", None)
    owns_core = core is None
    if owns_core:
        core = AgentCore()
        await core.init()
    app.state.core = core

    get_config_service()  # 触发加载 overrides

    registry = AgentRegistry(core)
    await registry.load_persisted()
    app.state.agent_registry = registry
    return core, owns_core


def _resolve_env_api_key() -> str:
    """读取 .env 中的 MIMO_API_KEY 用于判断降级模式, 不存在时兜底创建空 .env"""
    import os as _os
    from pathlib import Path as _Path
    try:
        from config import ENV_PATH
        _env_path = str(ENV_PATH)
    except ImportError:
        _env_path = str(_Path.home() / ".ai-agent" / ".env")
    # 确保 .env 文件存在（首次启动时 agent.py 已创建，这里做兜底）
    if not _os.path.exists(_env_path):
        try:
            from setup_wizard import ENV_EXAMPLE_PATH
            if _os.path.exists(ENV_EXAMPLE_PATH):
                import shutil as _shutil
                _shutil.copy2(ENV_EXAMPLE_PATH, _env_path)
                logger.info("webui.env_created_from_example")
            else:
                with open(_env_path, "w", encoding="utf-8") as _f:
                    _f.write("")
                logger.info("webui.env_created_empty")
        except (OSError, PermissionError) as _e:
            logger.warning("webui.env_create_failed error={}", str(_e))
    _mimo = ""
    if _os.path.exists(_env_path):
        with open(_env_path, encoding="utf-8", errors="ignore") as _f:
            for _line in _f:
                _s = _line.strip()
                if _s.startswith("MIMO_API_KEY="):
                    _mimo = _s.split("=", 1)[1].strip().strip("'\"")
                    break
    return _mimo


async def _shutdown_lifespan(app: FastAPI, core: Any, owns_core: bool) -> None:
    """关闭服务与资源: qq_task / 插件 / 调度器 / media / core"""
    qq_task = getattr(app.state, "qq_task", None)
    if qq_task:
        qq_task.cancel()
        with suppress(asyncio.CancelledError, RuntimeError):
            await qq_task
    # 取消后台一次性任务（健康自检 / 画像整合）
    for _attr in ("health_run_task", "portrait_consolidate_task"):
        _t = getattr(app.state, _attr, None)
        if _t and not _t.done():
            _t.cancel()
            with suppress(asyncio.CancelledError, RuntimeError):
                await _t
    # Shutdown plugins
    plugin_mgr = getattr(app.state, "plugin_manager", None)
    if plugin_mgr:
        try:
            await plugin_mgr.shutdown_all()
        except (RuntimeError, OSError):
            logger.debug("server.plugin_shutdown_error", exc_info=True)
    greeting_scheduler = getattr(app.state, "greeting_scheduler", None)
    if greeting_scheduler:
        await greeting_scheduler.stop()
    recall_scheduler = getattr(app.state, "recall_scheduler", None)
    if recall_scheduler:
        await recall_scheduler.stop()
    media_queue = getattr(app.state, "media_queue", None)
    if media_queue:
        await media_queue.stop()
    mail_poller = getattr(app.state, "mail_poller", None)
    if mail_poller:
        await mail_poller.stop()
    # 停止自发回忆和成长叙事后台任务（避免 shutdown 后继续访问已关闭的 db/memory）
    for attr in ("spontaneous_recall", "growth_narrative"):
        obj = getattr(app.state, attr, None)
        if obj and hasattr(obj, "stop"):
            try:
                await obj.stop()
            except (RuntimeError, OSError):
                logger.debug(f"server.{attr}_stop_error", exc_info=True)
    if owns_core:
        try:
            await core.shutdown()
        except (RuntimeError, OSError):
            logger.debug("server.core_shutdown_error", exc_info=True)


def create_app() -> FastAPI:
    # 动态读取版本号，不再硬编码
    try:
        from pathlib import Path as _P
        _ver = (_P(__file__).resolve().parent.parent / "VERSION").read_text().strip()
    except (OSError, ValueError):
        _ver = "0.4.95"
    app = FastAPI(title="Xiaoda Agent WebUI", version=_ver, lifespan=lifespan)

    # 速率限制中间件（三级: 全局/用户/写端点, 防 DDoS/滥用）
    # 在路由之前注册, 尽早拦截超限请求; 限制值可通过环境变量覆盖
    # F7: 令牌桶状态持久化到 SQLite, 进程重启后恢复 (避免重启即放行)
    from web.middleware.rate_limit import RateLimitMiddleware
    try:
        from config import DATA_DIR
        _rate_limit_db = str(Path(DATA_DIR) / "rate_limit_buckets.sqlite")
    except (ImportError, AttributeError):
        logger.debug("server.config_fallback_error", exc_info=True)
        _rate_limit_db = str(Path(__file__).parent.parent / "data" / "rate_limit_buckets.sqlite")
    app.add_middleware(RateLimitMiddleware, persist_path=_rate_limit_db)

    # 允许 splash HTTP 服务器嵌入 WebUI（iframe 预加载无缝衔接）
    @app.middleware("http")
    async def _allow_frame_embed(request: Any, call_next: Any) -> Any:
        import time as _time
        from utils.trace_context import new_trace_id
        _trace_id = new_trace_id()
        _start = _time.monotonic()
        response = await call_next(request)
        _elapsed = _time.monotonic() - _start
        _sla = getattr(app.state, "sla_exporter", None)
        # 跳过 /metrics 自身，避免抓取指标时污染监控数据
        if _sla and request.url.path != "/metrics":
            _sla.inc_request(request.url.path, str(response.status_code))
            _sla.observe_latency(request.url.path, _elapsed)
            if response.status_code >= 400:
                _sla.inc_error(f"http_{response.status_code}", request.url.path)
        response.headers["X-Trace-Id"] = _trace_id
        response.headers["Content-Security-Policy"] = "frame-ancestors 'self' http://127.0.0.1:*"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # 滑动续期：get_current_user 在 request.state 上设置了新 token 时写入响应头
        new_token = getattr(request.state, "new_token", None)
        if new_token:
            response.headers["X-New-Token"] = new_token
            new_expiry = getattr(request.state, "new_expiry", 0)
            if new_expiry:
                response.headers["X-New-Token-Expiry"] = str(int(new_expiry))
        return response

    # Q1: 注册统一异常处理器（AppException -> 结构化 error_code; 未捕获异常 -> E_SYS999）
    from web.error_handler import register_error_handlers
    register_error_handlers(app)

    from web.routers.auth import router as auth_router
    from web.routers.chat import router as chat_router
    from web.routers.system import router as system_router, public_router as system_public_router
    from web.routers.agents import router as agents_router
    from web.routers.models import router as models_router
    from web.routers.tools import router as tools_router
    from web.routers.mcp import router as mcp_router
    from web.routers.insight import router as insight_router
    from web.routers.schedule import router as schedule_router
    from web.routers.media import router as media_router
    from web.routers.health import router as health_router
    from web.routers.plugins import router as plugins_router
    from web.routers.setup import router as setup_router
    from web.routers.model_discovery import router as model_discovery_router
    from web.routers.market import router as market_router
    from web.routers.mail_manage import router as mail_manage_router
    from web.routers.workflows import router as workflows_router

    for r in (auth_router, chat_router, system_router, agents_router,
              models_router, tools_router, mcp_router, insight_router,
              schedule_router, media_router, health_router, plugins_router,
              setup_router, model_discovery_router, market_router,
              mail_manage_router, workflows_router, system_public_router):
        app.include_router(r, prefix="/api/v1")

    from web.ws_hub import router as ws_router
    app.include_router(ws_router)

    from core.sla_exporter import get_sla_exporter
    _sla = get_sla_exporter()
    app.state.sla_exporter = _sla

    # Prometheus /metrics 端点 (P1-4): 三层优先级控制注册
    # 优先级 (高 -> 低):
    #   1. 环境变量 METRICS_ENABLED (CI / 容器编排场景, 强制覆盖)
    #   2. config_service.observability.metrics_enabled (用户在 webui_overrides.json 修改)
    #   3. 默认 True (开箱即用)
    # - 任一层级关闭时不注册路由 -> /metrics 返回 404
    # - 由 web/routers/metrics.py 提供, 桥接 utils/metrics.py + 进程级默认指标
    metrics_enabled_env = os.getenv("METRICS_ENABLED")
    if metrics_enabled_env is not None:
        # 环境变量优先级最高 (CI / 部署场景强制覆盖)
        metrics_enabled = metrics_enabled_env.lower() in ("true", "1", "yes")
        logger.info(
            "webui.metrics_endpoint_env_override enabled={}", metrics_enabled
        )
    else:
        # 未设环境变量时, 读 config_service 的 observability.metrics_enabled
        # 让用户通过 WebUI 开关即时控制 (无需手动保存, config_service 原子写盘 + 热生效)
        try:
            from web.config_service import get_config_service
            cfg = get_config_service()
            metrics_enabled = bool(
                cfg.get("observability.metrics_enabled", True)
            )
        except Exception as e:
            # config_service 异常时 fail-open (保留默认开启), 不阻塞 server 启动
            logger.warning("webui.metrics_endpoint_config_read_failed err={}", e)
            metrics_enabled = True
        logger.info("webui.metrics_endpoint_config enabled={}", metrics_enabled)
    if metrics_enabled:
        from web.routers.metrics import router as metrics_router
        app.include_router(metrics_router)
        logger.info("webui.metrics_endpoint_enabled")
    else:
        logger.info("webui.metrics_endpoint_disabled")

    # 媒体目录使用用户数据目录，避免写入 _MEIPASS 只读目录
    try:
        from config import MEDIA_DIR
        media_dir = MEDIA_DIR
    except ImportError:
        media_dir = Path(__file__).parent / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    # follow_symlink：表情包等媒体是指向外置盘的符号链接
    # 壁纸等媒体文件禁强缓存，确保换图后浏览器不使用旧缓存
    app.mount("/media", NoCacheMediaStaticFiles(directory=str(media_dir), follow_symlink=True),
              name="media")

    dist_dir = Path(__file__).parent / "dist"
    if dist_dir.exists():
        app.mount("/", NoCacheHTMLStaticFiles(directory=str(dist_dir), html=True), name="spa")

    return app


class NoCacheMediaStaticFiles(StaticFiles):
    """媒体文件（壁纸/表情包等）禁强缓存。

    设置 Cache-Control: no-cache，浏览器每次都会向服务器验证是否有新版本，
    换壁纸后无需清浏览器缓存即可看到新图。
    """

    async def get_response(self, path: Any, scope: Any) -> Any:
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


class NoCacheHTMLStaticFiles(StaticFiles):
    """index.html 禁缓存（否则改版后旧 HTML 引用已删除的旧 chunk，导航全挂）；
    带 hash 的 /assets/* 短缓存（升级后浏览器会重新验证）。

    SPA fallback: 非 API/WS 路径 404 时返回 index.html,
    让 Vue Router 接管客户端路由 (刷新/直接访问 URL 不白屏)。
    """

    async def get_response(self, path: Any, scope: Any) -> Any:
        # Starlette 1.3+ StaticFiles.get_response 在路径不存在时直接
        # raise HTTPException(404) 而非返回 Response(status_code=404),
        # 因此需用 try/except 捕获并回退到 index.html (SPA fallback)。
        from starlette.exceptions import HTTPException as StarletteHTTPException

        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if (
                exc.status_code == 404
                and scope.get("method", "") == "GET"
                and not path.startswith(("api/", "ws", "media/"))
                and not path.startswith(("assets/",))  # 静态资源 404 不 fallback
            ):
                index_file = Path(self.directory) / "index.html"
                if index_file.exists():
                    from starlette.responses import FileResponse
                    return FileResponse(
                        str(index_file),
                        media_type="text/html",
                        headers={"Cache-Control": "no-cache, must-revalidate"},
                    )
            raise  # 其它 4xx/5xx 或非 GET 路径重新抛出

        # 路径存在时的 SPA fallback 兜底（如某些版本返回 404 Response 而非抛异常）
        if (
            response.status_code == 404
            and scope.get("method", "") == "GET"
            and not path.startswith(("api/", "ws", "media/"))
            and not path.startswith(("assets/",))
        ):
            index_file = Path(self.directory) / "index.html"
            if index_file.exists():
                from starlette.responses import FileResponse
                return FileResponse(
                    str(index_file),
                    media_type="text/html",
                    headers={"Cache-Control": "no-cache, must-revalidate"},
                )

        # 原有缓存控制逻辑
        if path in ("index.html", ".") or path.endswith(".html"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        elif path.startswith("assets/"):
            # no-cache：每次使用前向服务器验证，升级后立即生效
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app = create_app()