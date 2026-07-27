from typing import Any
from collections.abc import AsyncIterator
import asyncio
import hashlib
import os
import sys
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from core.app_exception import LLMError

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
        # P0 修复（硬编码/ollama 默认启用根因）：
        # 原实现 _default_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        # 总是返回非空值（env 未设也回退到 localhost:11434），导致 ollama 永远被注册，
        # 即使用户没配置也会尝试连接 → 持续报错（日志中 custom_provider.registered id=ollama）。
        # 修复：ollama 的 _default_url 设为空串，仅当 env_values（.env）显式配置时才注册。
        # 其他 provider 的 _default_url 是 SaaS 云端固定端点（非用户自定义），保留默认值正确。
        "OLLAMA_BASE_URL": (
            "ollama", "openai",
            "", "Ollama 本地大模型"
        ),
    }
    known_env_keys = list(_KNOWN_ENV_PROVIDERS.keys())
    for env_key, (pid, fmt, _default_url, label) in _KNOWN_ENV_PROVIDERS.items():
        if env_key == "OLLAMA_BASE_URL":
            # ollama：仅当 .env 显式配置 OLLAMA_BASE_URL 时才注册（_default_url 为空串）
            api_key = "ollama"
            base_url = env_values.get(env_key, "").strip()
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
        # P0 修复（ollama 默认启用根因 2/2）：
        # 即使旧版本 bug 已把 ollama 写入持久化 config（base_url=localhost:11434），
        # 这里也要拦住：ollama 是本地服务，必须 OLLAMA_BASE_URL 环境变量显式配置才注册。
        # 云端 provider（siliconflow/openrouter 等）不受此约束 —— 它们的 URL 是固定的 SaaS 端点。
        if pid == "ollama" and not os.getenv("OLLAMA_BASE_URL", "").strip():
            logger.info("webui.skip_ollama_no_env reason=OLLAMA_BASE_URL not set, skipping stale config entry")
            continue
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
    # 直接修改内存中的 ROUTE_TABLE 和 _current_chat_model，不调用 set_chat_model。
    # set_chat_model 会触发持久化（cfg.set），而 _restore_chat_model 只做"恢复"不做"持久化"，
    # 否则每次启动都会重新持久化当前模型到 config，覆盖用户的后续切换选择。
    # 成功路径：直接设置 ROUTE_TABLE["chat"] 和 _current_chat_model
    # fallback 路径：回退到 mimo，同样只修改内存不持久化
    try:
        # 检查 provider 是否已注册（自定义 provider 可能未注册）
        if provider not in ("mimo", "agnes") and provider not in getattr(core.router, '_custom_clients', {}):
            raise LLMError(f"自定义 provider {provider} 未注册")
        chat_entry = ROUTE_TABLE.get("chat")
        if chat_entry is not None:
            chat_entry["model"] = model_id
            chat_entry["client"] = provider
        core.router._current_chat_model = {"provider": provider, "model_id": model_id}
        logger.info("webui.chat_model_restored provider={} model={}", provider, model_id)
    except Exception as e:
        # 必须捕获 Exception：LLMError 继承 AppException 不在原 (KeyError, ValueError,
        # AttributeError, OSError) 范围内，自定义 provider 注册失败时会导致启动崩溃
        logger.warning(
            "webui.chat_model_restore_failed provider={} model={} "
            "error={} fallback_to_mimo_in_memory_only", provider, model_id, str(e)
        )
        # 仅修改内存中的 ROUTE_TABLE，不调用 set_chat_model 避免重新持久化 mimo
        # 这样用户下次切换模型时，config 中仍是原选择，不会被 sticky mimo 覆盖
        try:
            from model_router import MIMO_MODEL
            chat_entry = ROUTE_TABLE.get("chat")
            if chat_entry is not None:
                chat_entry["model"] = MIMO_MODEL
                chat_entry["client"] = "mimo"
            core.router._current_chat_model = {"provider": "mimo", "model_id": MIMO_MODEL}
            logger.info("webui.chat_model_fallback_in_memory provider=mimo model={}", MIMO_MODEL)
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

    # _apply_model_overrides 已提到 lifespan 中无条件执行（在降级判定之前），
    # 保证降级模式下也能注册已保存的自定义 provider / 恢复路由 / 恢复 chat_model。
    # 这里不再重复调用，避免对 router 做二次注册。
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

    # QQ Bot：走统一入口 ensure_qq_bot_task，相同凭证下并发调用（_background_reinit
    # 与 _start_services 同时触发）会通过指纹合并复用现有 task，避免重复启动抖动
    await ensure_qq_bot_task(app)
    app.state.last_emotion = None


def _qq_credential_fingerprint() -> str:
    """计算 QQ Bot 凭证指纹：APP_ID + APP_SECRET + ENABLE_QQ_BOT 的 sha256 前 16 位。

    用于 ensure_qq_bot_task 的合并判定——相同凭证的并发请求只启动一次 Bot，
    避免重复"取消→新建"抖动。只记录指纹前缀到日志，不写 secret 原文。
    """
    app_id = os.getenv("QQBOT_APP_ID", "").strip()
    app_secret = os.getenv("QQBOT_APP_SECRET", "").strip()
    enable = os.getenv("ENABLE_QQ_BOT", "true").strip()
    raw = f"{app_id}|{app_secret}|{enable}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


async def ensure_qq_bot_task(app: FastAPI, force: bool = False) -> bool:
    """QQ Bot 任务的统一入口：按凭证指纹合并并发重启请求。

    根因：restart_qq_bot_task 中 asyncio.Lock 仅保证重启流程互斥；每个等待者
    获得锁后仍会执行"取消当前 task → 新建 task"。没有凭证版本比较、in-flight
    合并或防抖——20 次相同凭证并发请求会导致 19 次 Bot 启动、18 次刚启动即被取消。
    用凭证指纹判定：当前存活 task 的指纹与目标一致且非强制时直接复用，不取消不重建。

    Returns:
        True 表示已启动新的 QQ bot 任务或复用现有任务，False 表示未启动
    """
    # 锁的懒创建沿用 restart_qq_bot_task 既有写法，避免模块级状态
    lock = getattr(app.state, "_qq_restart_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state._qq_restart_lock = lock
    async with lock:
        target_fp = _qq_credential_fingerprint()
        current = getattr(app.state, "qq_task", None)
        applied_fp = getattr(app.state, "_qq_applied_fingerprint", None)
        # 合并判定：凭证未变且有存活 task → 复用，不取消不重建
        if not force and current is not None and not current.done() and applied_fp == target_fp:
            logger.info("webui.qq_bot_reuse_existing fp={}", target_fp)
            return True
        started = await _restart_qq_bot_task_inner(app)
        # 成功创建写回指纹；未启动（凭证缺失/禁用/失败）置 None，
        # 避免下次同凭证误判"已有存活 task"而跳过真正的启动
        app.state._qq_applied_fingerprint = target_fp if started else None
        return started


async def restart_qq_bot_task(app: FastAPI) -> bool:
    """凭证保存路径强制重启的薄封装（force=True 绕过指纹合并）。

    保留此函数名：web/routers/setup.py 在 QQ 凭证保存后调用它，用户改了凭证
    必须重启（不能复用旧 task），所以走 force 分支。
    """
    return await ensure_qq_bot_task(app, force=True)


async def _restart_qq_bot_task_inner(app: FastAPI) -> bool:
    """restart_qq_bot_task 的实际逻辑（在 Lock 保护下执行）。"""
    # 1. 取消已存在的 qq_task（用旧凭证运行的实例）
    old_task = getattr(app.state, "qq_task", None)
    if old_task and not old_task.done():
        old_task.cancel()
        with suppress(asyncio.CancelledError, RuntimeError):
            await old_task
        logger.info("webui.qq_bot_old_task_cancelled_for_restart")

    # 2. 更新 qq_bot_adapter 模块级变量（原在 import 时一次性读取，不会感知 env 更新）
    new_app_id = os.getenv("QQBOT_APP_ID", "").strip()
    new_app_secret = os.getenv("QQBOT_APP_SECRET", "").strip()
    try:
        import qq_bot_adapter
        qq_bot_adapter.APP_ID = new_app_id
        qq_bot_adapter.APP_SECRET = new_app_secret
    except (ImportError, AttributeError) as e:
        logger.warning("webui.qq_bot_adapter_module_update_failed error={}", str(e))
        app.state.qq_task = None
        return False

    # 3. 检查启用条件
    enable_qq = os.getenv("ENABLE_QQ_BOT", "true").lower() in ("true", "1", "yes")
    if not new_app_id or not new_app_secret or not enable_qq:
        logger.info("webui.qq_bot_not_started reason=missing_credentials_or_disabled "
                    "app_id_set={} secret_set={} enable={}",
                    bool(new_app_id), bool(new_app_secret), enable_qq)
        app.state.qq_task = None
        return False

    # 4. 启动新的 QQ bot 任务
    try:
        from config import AGENT_CONFIG
        from qq_bot_adapter import run_qq_bot
        core = app.state.core
        sandbox = AGENT_CONFIG.get("qq_bot", {}).get("is_sandbox", False)
        new_task = asyncio.create_task(run_qq_bot(core, sandbox=sandbox))
        app.state.qq_task = new_task
        logger.info("webui.qq_bot_task_restarted_after_credential_save sandbox={}", sandbox)
        return True
    except (ImportError, RuntimeError, AttributeError) as e:
        logger.error("webui.qq_bot_task_restart_failed error={}", str(e))
        app.state.qq_task = None
        return False


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
    # 根因：原判定只看 MIMO_API_KEY，缺少该 key 时跳过 _start_services，
    # 从而 _apply_model_overrides 也不执行 —— 已保存的 Agnes / OpenRouter /
    # SiliconFlow / 自定义 provider 凭证全部失效。这里先无条件执行
    # _apply_model_overrides（注册自定义 provider、应用路由覆盖、恢复 chat_model），
    # 再以"任一 provider 凭证是否存在"判定是否进入降级模式。
    await _apply_model_overrides(core)
    if not _has_any_provider_credential():
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

    # 启动事件循环阻塞 watchdog：检测同步阻塞并打印线程栈定位根因
    # 根因：后台任务集体卡 257-265s，_spawn timeout 无法取消同步阻塞
    _stop_watchdog = None
    try:
        from core.background_tasks import start_event_loop_watchdog, stop_event_loop_watchdog
        start_event_loop_watchdog()
        _stop_watchdog = stop_event_loop_watchdog
    except Exception as e:
        logger.warning("webui.watchdog_start_failed error={}", str(e))

    yield

    # 停止 watchdog
    if _stop_watchdog is not None:
        try:
            _stop_watchdog()
        except Exception:
            pass

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


def _has_any_provider_credential() -> bool:
    """判断是否持有任一可用 provider 凭证，用于决定是否进入降级模式。

    根因：原 lifespan 仅凭 MIMO_API_KEY 是否存在决定降级，但用户可能保存了
    Agnes / OpenRouter / SiliconFlow 或自定义 provider 凭证。这些场景下若
    MIMO_API_KEY 缺失，会一并跳过 _start_services，导致 _apply_model_overrides
    不执行 —— 自定义 provider 不注册、路由覆盖不应用、models.chat_model 不恢复。
    任一来源有凭证即视为可用，避免误判降级。
    """
    # 1. MiMo：复用现有 .env 解析逻辑，与原 lifespan 旧判定保持一致
    if _resolve_env_api_key().strip():
        return True

    # 2. Agnes：从 .env 读取（_apply_model_overrides 实际注册时也走 .env，
    #    保持判定源与生效源一致，避免"判定为可用但实际未注册"的错配）
    try:
        from setup_wizard import _load_env_values
        if _load_env_values().get("AGNES_API_KEY", "").strip():
            return True
    except (ImportError, OSError, ValueError):
        logger.debug("server.agnes_key_check_failed", exc_info=True)

    # 3. 自定义 provider：复用 config_service 已加载的 providers 配置 +
    #    load_provider_key 读取凭证文件，不新写 JSON 解析
    try:
        from web.config_service import get_config_service
        from web._provider_keys import load_provider_key
        cfg = get_config_service()
        for pid in (cfg.get("models.providers", {}) or {}):
            if pid == "ollama":
                # ollama 不需要 API key，由下方第 4 步检查 OLLAMA_BASE_URL
                continue
            if load_provider_key(pid).strip():
                return True
    except (ImportError, OSError, ValueError) as e:
        logger.warning("webui.custom_provider_credential_check_failed error={}", str(e))

    # 4. Ollama：不需要 API key，仅看 .env 是否显式配置 OLLAMA_BASE_URL
    #    （与 _apply_model_overrides 的注册条件一致，Ollama-only 部署不误入降级模式）
    try:
        from setup_wizard import _load_env_values
        if _load_env_values().get("OLLAMA_BASE_URL", "").strip():
            return True
    except (ImportError, OSError, ValueError):
        logger.debug("server.ollama_url_check_failed", exc_info=True)

    return False


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