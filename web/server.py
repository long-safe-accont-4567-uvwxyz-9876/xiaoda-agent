import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).parent.parent))


async def _apply_model_overrides(core):
    """重启后恢复：自定义 provider 注册 + 路由表覆盖。"""
    import os
    from web.config_service import get_config_service
    from web.custom_providers import register_into_router
    from web.routers.models import load_provider_key
    from model_router import ROUTE_TABLE

    cfg = get_config_service()

    # 自动注册已知免费模型平台（从 .env 文件读取 key，不从 os.environ 读取，避免 CI 环境变量泄露）
    _KNOWN_ENV_PROVIDERS = {
        "SILICONFLOW_API_KEY": ("siliconflow", "openai", "https://api.siliconflow.cn/v1", "SiliconFlow 硅基流动"),
        "OPENROUTER_API_KEY": ("openrouter", "openai", "https://openrouter.ai/api/v1", "OpenRouter"),
        "MODELSCOPE_ACCESS_TOKEN": ("modelscope", "openai", "https://api-inference.modelscope.cn/v1", "ModelScope 魔搭"),
        "AGNES_API_KEY": ("agnes", "openai", os.getenv("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1"), "Agnes AI"),
        "OLLAMA_BASE_URL": ("ollama", "openai", os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"), "Ollama 本地大模型"),
    }
    # 从 .env 文件读取，而非 os.environ，防止构建环境变量泄露到用户安装包
    try:
        from setup_wizard import _load_env_values
        env_values = _load_env_values()
    except Exception:
        env_values = {}
    # _KNOWN_ENV_PROVIDERS 保持现有顺序（SiliconFlow 第一）
    known_env_keys = list(_KNOWN_ENV_PROVIDERS.keys())
    for env_key, (pid, fmt, base_url, label) in _KNOWN_ENV_PROVIDERS.items():
        # Ollama 特殊处理：env 值是 base_url 而非 API Key
        if env_key == "OLLAMA_BASE_URL":
            api_key = "ollama"  # 占位 Key
            base_url = env_values.get(env_key, "").strip() or base_url
            if not base_url:
                continue
        else:
            api_key = env_values.get(env_key, "").strip()
            if not api_key:
                continue
        # 确保配置中有记录
        existing = cfg.get("models.providers", {}) or {}
        if pid not in existing:
            cfg.set(f"models.providers.{pid}", {
                "label": label, "format": fmt, "base_url": base_url,
                "default_model": "", "enabled": True,
                "order": known_env_keys.index(env_key),
            })
        # 确保证书文件存在
        from config import get_credentials_dir
        cred_dir = get_credentials_dir()
        cred_dir.mkdir(parents=True, exist_ok=True)
        fp = cred_dir / f"provider_{pid}.key"
        if not fp.exists() or fp.read_text(encoding="utf-8").strip() != api_key:
            fp.write_text(api_key, encoding="utf-8")
            try:
                os.chmod(fp, 0o600)
            except OSError:
                pass

    # 按 order 字段处理 Provider 注册顺序；未设置 order 的排在已设置之后，按字典插入顺序
    all_providers = cfg.get("models.providers", {}) or {}
    all_keys_order = list(all_providers.keys())
    sorted_providers = sorted(
        all_providers.items(),
        key=lambda kv: (kv[1].get("order", 9999), all_keys_order.index(kv[0]))
    )
    for pid, p in sorted_providers:
        key = load_provider_key(pid)
        if key and p.get("enabled", True):
            try:
                register_into_router(core.router, pid, p.get("format", "openai"),
                                     p.get("base_url", ""), key)
                # 也注册到 credential_pool，以便追踪使用次数和错误
                from utils.credential_pool import get_credential_pool, Credential, CredentialState
                pool = get_credential_pool()
                if pid not in pool._pool:
                    pool.add_credential(Credential(
                        api_key=key,
                        provider=pid,
                        base_url=p.get("base_url", ""),
                    ))
            except Exception as e:
                logger.warning("webui.provider_restore_failed id={} error={}", pid, str(e))
    for task, o in (cfg.get("models.routes", {}) or {}).items():
        entry = ROUTE_TABLE.get(task)
        if not entry or not isinstance(o, dict):
            continue
        if o.get("model"):
            entry["model"] = o["model"]
        if o.get("client"):
            entry["client"] = o["client"]
        if o.get("max_tokens"):
            entry["max_tokens"] = o["max_tokens"]
        if o.get("thinking"):
            entry.setdefault("thinking", {"type": "enabled", "budget_tokens": 2048})
        elif "thinking" in o:
            entry.pop("thinking", None)
        if o.get("timeout"):
            core.router.TASK_TIMEOUTS[task] = o["timeout"]

    # 恢复上次聊天模型（从 config_service 的 models.chat_model 读取）
    chat_model = cfg.get("models.chat_model")
    if isinstance(chat_model, dict) and chat_model.get("provider") and chat_model.get("model_id"):
        provider = chat_model["provider"]
        model_id = chat_model["model_id"]
        try:
            core.router.set_chat_model(provider, model_id)
            logger.info("webui.chat_model_restored provider={} model={}", provider, model_id)
        except Exception as e:
            logger.warning("webui.chat_model_restore_failed provider={} model={} error={} fallback_to_mimo", provider, model_id, str(e))
            # Provider 或模型已失效，回退到 MiMo 默认模型
            try:
                from model_router import MIMO_MODEL
                core.router.set_chat_model("mimo", MIMO_MODEL)
            except Exception:
                pass


async def _start_user_mcp_servers(core):
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
        except Exception as e:
            logger.warning("webui.mcp_restore_failed name={} error={}", name, str(e))


async def _start_services(app, core):
    """启动正常模式下的所有服务组件（PluginManager、MediaTaskQueue、GreetingScheduler、QQ Bot）。"""
    from web.config_service import get_config_service
    from web.media_tasks import MediaTaskQueue
    from web.greeting_scheduler import GreetingScheduler
    from web.routers.tools import apply_tool_overrides
    from web.ws_hub import manager

    await _apply_model_overrides(core)
    apply_tool_overrides()
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

    # 主动检索 B：定时回忆任务调度器（独立后台循环，每 3h 整理回忆笔记）
    try:
        from memory.recall_scheduler import MemoryRecallScheduler
        recall_scheduler = MemoryRecallScheduler(core)
        recall_scheduler.start()
        app.state.recall_scheduler = recall_scheduler
    except Exception as e:
        logger.warning("webui.recall_scheduler_init_failed", error=str(e))

    # QQ Bot
    qq_task = None
    if os.getenv("QQBOT_APP_ID") and os.getenv("ENABLE_QQ_BOT", "true").lower() in ("true", "1", "yes"):
        from qq_bot_adapter import run_qq_bot
        from config import AGENT_CONFIG
        qq_task = asyncio.create_task(
            run_qq_bot(core, sandbox=AGENT_CONFIG.get("qq_bot", {}).get("is_sandbox", False)))
        logger.info("webui.qq_bot_task_started")
    app.state.qq_task = qq_task
    app.state.last_emotion = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    from agent_core import AgentCore
    from web.agent_registry import AgentRegistry
    from web.config_service import get_config_service

    logger.info("webui.lifespan.start")
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

    # 降级模式：直接读 .env 文件检查 MIMO_API_KEY
    # 使用用户目录中的 .env（与 config.get_env_path() 一致）
    import os as _os, sys as _sys
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
        except Exception as _e:
            logger.warning("webui.env_create_failed error={}", str(_e))
    _mimo = ""
    if _os.path.exists(_env_path):
        with open(_env_path, "r", encoding="utf-8", errors="ignore") as _f:
            for _line in _f:
                _s = _line.strip()
                if _s.startswith("MIMO_API_KEY="):
                    _mimo = _s.split("=", 1)[1].strip().strip("'\"")
                    break
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
    qq_task = getattr(app.state, "qq_task", None)
    if qq_task:
        qq_task.cancel()
        try:
            await qq_task
        except (asyncio.CancelledError, Exception):
            pass
    # Shutdown plugins
    plugin_mgr = getattr(app.state, "plugin_manager", None)
    if plugin_mgr:
        try:
            await plugin_mgr.shutdown_all()
        except Exception:
            pass
    greeting_scheduler = getattr(app.state, "greeting_scheduler", None)
    if greeting_scheduler:
        await greeting_scheduler.stop()
    recall_scheduler = getattr(app.state, "recall_scheduler", None)
    if recall_scheduler:
        await recall_scheduler.stop()
    media_queue = getattr(app.state, "media_queue", None)
    if media_queue:
        await media_queue.stop()
    if owns_core:
        try:
            await core.shutdown()
        except Exception:
            pass


def create_app() -> FastAPI:
    app = FastAPI(title="Nahida Agent WebUI", version="0.3.98", lifespan=lifespan)

    from web.routers.auth import router as auth_router
    from web.routers.chat import router as chat_router
    from web.routers.system import router as system_router
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

    for r in (auth_router, chat_router, system_router, agents_router,
              models_router, tools_router, mcp_router, insight_router,
              schedule_router, media_router, health_router, plugins_router,
              setup_router, model_discovery_router):
        app.include_router(r, prefix="/api/v1")

    from web.ws_hub import router as ws_router
    app.include_router(ws_router)

    # 媒体目录使用用户数据目录，避免写入 _MEIPASS 只读目录
    try:
        from config import MEDIA_DIR
        media_dir = MEDIA_DIR
    except ImportError:
        media_dir = Path(__file__).parent / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    # follow_symlink：表情包等媒体是指向外置盘的符号链接
    app.mount("/media", StaticFiles(directory=str(media_dir), follow_symlink=True),
              name="media")

    dist_dir = Path(__file__).parent / "dist"
    if dist_dir.exists():
        app.mount("/", NoCacheHTMLStaticFiles(directory=str(dist_dir), html=True), name="spa")

    return app


class NoCacheHTMLStaticFiles(StaticFiles):
    """index.html 禁缓存（否则改版后旧 HTML 引用已删除的旧 chunk，导航全挂）；
    带 hash 的 /assets/* 短缓存（升级后浏览器会重新验证）。"""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path in ("index.html", ".") or path.endswith(".html"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        elif path.startswith("assets/"):
            # no-cache：每次使用前向服务器验证，升级后立即生效
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app = create_app()
