import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
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
    }
    # 从 .env 文件读取，而非 os.environ，防止构建环境变量泄露到用户安装包
    try:
        from setup_wizard import _load_env_values
        env_values = _load_env_values()
    except Exception:
        env_values = {}
    for env_key, (pid, fmt, base_url, label) in _KNOWN_ENV_PROVIDERS.items():
        api_key = env_values.get(env_key, "").strip()
        if not api_key:
            continue
        # 确保配置中有记录
        existing = cfg.get("models.providers", {}) or {}
        if pid not in existing:
            cfg.set(f"models.providers.{pid}", {
                "label": label, "format": fmt, "base_url": base_url,
                "default_model": "", "enabled": True,
            })
        # 确保证书文件存在
        from pathlib import Path
        cred_dir = Path(__file__).resolve().parent.parent / "credentials"
        cred_dir.mkdir(parents=True, exist_ok=True)
        fp = cred_dir / f"provider_{pid}.key"
        if not fp.exists() or fp.read_text(encoding="utf-8").strip() != api_key:
            fp.write_text(api_key, encoding="utf-8")
            try:
                os.chmod(fp, 0o600)
            except OSError:
                pass

    for pid, p in (cfg.get("models.providers", {}) or {}).items():
        key = load_provider_key(pid)
        if key and p.get("enabled", True):
            try:
                register_into_router(core.router, pid, p.get("format", "openai"),
                                     p.get("base_url", ""), key)
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("server.startup")
    try:
        from core import NahidaCore
        core = NahidaCore()
        await core.init()
        app.state.core = core
        # Apply model overrides (custom providers + route table)
        await _apply_model_overrides(core)
        # Initialize agent registry
        try:
            from web.agent_registry import AgentRegistry
            registry = AgentRegistry(core)
            await registry.load_persisted()
            app.state.agent_registry = registry
            logger.info("server.registry_loaded")
        except Exception as e:
            logger.warning("server.registry_init_failed error={}", str(e))
        logger.info("server.core_initialized")
    except Exception as e:
        logger.error("server.core_init_failed error={}", str(e))
        # Degraded mode: continue without core
        app.state.core = None
        app.state.agent_registry = None
        logger.warning("server.degraded_mode")

    yield

    # Shutdown
    logger.info("server.shutdown")


app = FastAPI(title="Nahida Agent", lifespan=lifespan)

# ── Static files (Vue SPA) ──
_static_dir = Path(__file__).parent / "dist"
if _static_dir.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_static_dir / "assets")), name="static-assets")
    logger.info("server.static_mounted path={}", _static_dir)


# ── API routers ──
from web.routers import health, setup, models, model_discovery, agents, chat, ws_hub

app.include_router(health.router, prefix="/api/v1")
app.include_router(setup.router, prefix="/api/v1")
app.include_router(models.router, prefix="/api/v1")
app.include_router(model_discovery.router, prefix="/api/v1")
app.include_router(agents.router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")
app.include_router(ws_hub.router)


# ── SPA fallback ──
from fastapi.responses import FileResponse, HTMLResponse

@app.get("/")
async def serve_index():
    index = _static_dir / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return HTMLResponse("<h1>Nahida Agent</h1><p>Frontend not built. Run <code>npm run build</code> in web/frontend/</p>")

@app.get("/{path:path}")
async def spa_fallback(path: str):
    # Try static file first
    file = _static_dir / path
    if file.is_file():
        return FileResponse(str(file))
    # Fallback to index.html for SPA routing
    index = _static_dir / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return HTMLResponse("<h1>404</h1>", status_code=404)
