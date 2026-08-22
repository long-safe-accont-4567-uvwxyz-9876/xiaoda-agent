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


async def _apply_model_overrides(core: Any, provider_service: Any | None = None) -> None:
    """重启后恢复：自定义 provider 注册 + 路由表覆盖。"""
    from web.config_service import get_config_service
    from model_router import ROUTE_TABLE

    logger.info("webui._apply_model_overrides_start")
    cfg = get_config_service()

    if provider_service is None:
        provider_service = getattr(core, "provider_service", None)
    if provider_service is not None:
        core.router = provider_service.runtime_router
    logger.info("webui.before_apply_route_overrides")
    _apply_route_overrides(cfg, core, ROUTE_TABLE)
    logger.info("webui.after_apply_route_overrides")
    _restore_chat_model(cfg, core)


def _register_env_providers(cfg: Any, env_values: Any, os_module: Any) -> None:
    """从 .env 注册已知免费模型平台 provider。"""
    from config import get_provider_catalog

    modelscope_credential = get_provider_catalog().resolve_environment_alias(
        "modelscope",
        env_values,
    )
    modelscope_env = modelscope_credential[0] if modelscope_credential else "MODELSCOPE_ACCESS_TOKEN"
    _KNOWN_ENV_PROVIDERS = {
        "SILICONFLOW_API_KEY": ("siliconflow", "openai", "https://api.siliconflow.cn/v1", "SiliconFlow 硅基流动"),
        "OPENROUTER_API_KEY": ("openrouter", "openai", "https://openrouter.ai/api/v1", "OpenRouter"),
        modelscope_env: (
            "modelscope", "openai",
            "https://api-inference.modelscope.cn/v1", "ModelScope 魔搭"
        ),
        "AGNES_API_KEY": (
            "agnes", "openai",
            # CodeRabbit 一致性修复：用已解析的 env_values（.env）而非进程级 os.getenv，
            # 与 _register_env_providers 的 env_values 来源一致；env_values 未设时回退默认值
            (env_values.get("AGNES_BASE_URL") or "https://apihub.agnes-ai.cn/v1").strip(), "Agnes AI"
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
        "LLAMA_CPP_BASE_URL": (
            "llama.cpp", "openai",
            "", "llama.cpp 本地接口"
        ),
    }
    known_env_keys = list(_KNOWN_ENV_PROVIDERS.keys())
    for env_key, (pid, fmt, _default_url, label) in _KNOWN_ENV_PROVIDERS.items():
        if env_key in ("OLLAMA_BASE_URL", "LLAMA_CPP_BASE_URL"):
            # 本地无 key 接口：仅当 .env 显式配置 base_url 时才注册（_default_url 为空串）
            api_key = pid
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
    from llm_gateway.provider_service import ProviderCredentialStore

    credentials = ProviderCredentialStore()
    if credentials.read(pid) != api_key:
        credentials.write(pid, api_key)


def _provider_sort_key(kv: tuple, key_order: list[str]) -> tuple[int, int]:
    """provider 排序键: order 字段优先, 原始键序兜底."""
    return (kv[1].get("order", 9999), key_order.index(kv[0]))


def _apply_route_overrides(cfg: Any, core: Any, ROUTE_TABLE: Any) -> None:
    """应用路由表覆盖（model/client/max_tokens/thinking/timeout）。

    同时清理持久化文件中 ROUTE_TABLE 已不存在的死路由（如 chat_mimo/chat_mini/chat_ultra）。
    旧版本曾使用这些 task，升级后 ROUTE_TABLE 删除了它们，但持久化文件残留，
    导致 WebUI 显示僵尸路由并每次启动都尝试应用无效覆盖。

    CodeRabbit#5 + C4 + M9 + CR-Major-3 修复：
    - 走 registry.update_route(persist=False) 而非直接改 ROUTE_TABLE 引用，
      保证所有修改走原子入口（registry 是 ROUTE_TABLE 的唯一读写入口）。
    - thinking budget_tokens 保留原 entry 值，不硬编码 2048。
    - provider 未注册时跳过该路由的 client 字段，避免写入死路由。
    """
    routes_config = cfg.get("models.routes", {}) or {}
    logger.info("webui.route_overrides_start total_tasks={}", len(routes_config))
    # CodeRabbit#5 + C4 修复：走 registry 原子入口而非直接改 ROUTE_TABLE 引用。
    # 测试场景 core.router 可能是 MagicMock，此时用临时 registry 包装 ROUTE_TABLE；
    # 生产中 core.router._registry 是 ModelRouteRegistry 实例，直接复用。
    from model_router import ModelRouteRegistry
    registry = getattr(core.router, '_registry', None)
    if not isinstance(registry, ModelRouteRegistry):
        registry = ModelRouteRegistry(ROUTE_TABLE)
    dead_routes: list[str] = []
    for task, o in list(routes_config.items()):
        entry = ROUTE_TABLE.get(task)
        if not entry:
            # 死路由：ROUTE_TABLE 中已删除，但持久化文件还有
            dead_routes.append(task)
            logger.info("webui.route_override_dead task={} reason=not_in_route_table", task)
            continue
        if not isinstance(o, dict):
            logger.warning("webui.route_override_skip task={} reason=invalid", task)
            continue
        # 读取持久化值，未提供的字段用原 entry 兜底
        _model = o.get("model") or entry.get("model", "")
        _client = o.get("client") or entry.get("client", "")
        _max_tokens = o.get("max_tokens") or entry.get("max_tokens")
        # CR-Major-3 修复：thinking 恢复时保留原 budget_tokens，不硬编码 2048
        _orig_thinking = entry.get("thinking") or {}
        _orig_budget = (_orig_thinking.get("budget_tokens", 4096)
                        if isinstance(_orig_thinking, dict) else 4096)
        if "thinking" in o:
            if o["thinking"]:
                _thinking = {"type": "enabled", "budget_tokens": _orig_budget}
            else:
                _thinking = {"type": "disabled"}
            logger.info("webui.thinking_loaded task={} budget_tokens={}",
                        task, _orig_budget)
        else:
            _thinking = entry.get("thinking")
        _timeout = o.get("timeout") or entry.get("timeout")
        # M9 修复：校验 provider 是否已注册，未注册则保留原 client（避免死路由）
        # N-2 修复：内置 provider 集合从 provider_metadata.json 派生，不硬编码
        from config import get_builtin_providers as _get_builtin_providers
        if _client and _client not in _get_builtin_providers():
            _registered = (_client in getattr(core.router, "_custom_clients", {})
                           or _client in getattr(core.router, "_transports", {}))
            if not _registered:
                logger.warning("webui.route_override_provider_unregistered task={} provider={} "
                               "keeping_original_client={}",
                               task, _client, entry.get("client", ""))
                _client = entry.get("client", "")
        # 走 registry 原子入口（persist=False：启动恢复不写回，避免无谓 IO）
        try:
            registry.update_route(
                task, model_id=_model, provider=_client,
                max_tokens=_max_tokens, thinking=_thinking,
                timeout=_timeout, persist=False,
            )
        except (KeyError, RuntimeError) as e:
            logger.warning("webui.route_override_apply_failed task={} error={}",
                           task, str(e))
        if _timeout:
            core.router.TASK_TIMEOUTS[task] = _timeout

    # 清理死路由：从持久化文件删除 ROUTE_TABLE 中已不存在的 task
    if dead_routes:
        for dr in dead_routes:
            try:
                cfg.delete(f"models.routes.{dr}")
                logger.info("webui.dead_route_cleaned task={}", dr)
            except (KeyError, AttributeError, OSError, ValueError) as e:
                logger.warning("webui.dead_route_clean_failed task={} error={}",
                               dr, str(e))
        logger.info("webui.dead_routes_cleaned count={}", len(dead_routes))


def _restore_chat_model(cfg: Any, core: Any) -> None:
    """恢复上次聊天模型（从 config_service 的 models.chat_model 读取）。

    设计原则（用户约束）：
    - 持久化的用户选择是真相源，失败时**绝不覆盖持久化**
    - 仅修改内存中的 ROUTE_TABLE 和 _current_chat_model
    - 失败时内存回退到 provider_metadata.json 的默认模型（不持久化）
      下次启动会重新尝试恢复用户选择
    - 不硬编码任何模型 ID，默认值从 get_default_model_for_provider() 读

    C1 修复：失败回退覆盖所有 sync task（chat/emotion_analysis/...），
    旧实现只回退 chat，导致其他 sync task 仍指向未注册 provider，
    调用时抛 LLMError 必须依赖 fallback 链兜底，延迟和错误率上升。
    """
    chat_model = cfg.get("models.chat_model")
    if not (isinstance(chat_model, dict) and chat_model.get("provider") and chat_model.get("model_id")):
        logger.info("webui.chat_model_no_saved_preference, using default")
        return
    provider = chat_model["provider"]
    model_id = chat_model["model_id"]
    from model_router import ModelRouteRegistry, ROUTE_TABLE
    # 测试场景 core.router 可能是 MagicMock，用临时 registry；生产用真实实例
    registry = getattr(core.router, '_registry', None)
    if not isinstance(registry, ModelRouteRegistry):
        registry = ModelRouteRegistry(ROUTE_TABLE)
    # 检查 ROUTE_TABLE["chat"] 当前值（已被 _apply_route_overrides 修改过）
    current_client = (registry.get_task_ref("chat") or {}).get("client", "")
    current_model = (registry.get_task_ref("chat") or {}).get("model", "")
    logger.info("webui.chat_model_restore_attempt saved={}/{} current_route={}/{}",
                provider, model_id, current_client, current_model)
    try:
        # 检查 provider 是否已注册（自定义 provider 可能未注册）
        # N-2 修复：内置 provider 集合从 provider_metadata.json 派生，不硬编码
        from config import get_builtin_providers as _get_builtin_providers
        if provider not in _get_builtin_providers() and provider not in getattr(core.router, '_custom_clients', {}):
            raise LLMError(f"自定义 provider {provider} 未注册")
        # 成功路径：用 registry.update_route 把 chat_model 写入 ROUTE_TABLE["chat"]
        # （其他 sync task 由 _apply_route_overrides 负责；这里只确保 chat 与用户选择一致）
        try:
            registry.update_route(
                "chat", model_id=model_id, provider=provider, persist=False,
            )
        except (KeyError, RuntimeError) as route_err:
            logger.warning("webui.chat_model_restore_route_failed error={}", str(route_err))
        core.router._current_chat_model = {"provider": provider, "model_id": model_id}
        logger.info("webui.chat_model_restored provider={} model={}", provider, model_id)
    except (ImportError, KeyError, ValueError, OSError, LLMError) as e:
        # 关键：失败时不覆盖持久化，仅内存回退所有 sync task 到默认 provider
        logger.warning(
            "webui.chat_model_restore_failed provider={} model={} "
            "error={} fallback_all_sync_tasks_to_default_in_memory_only_persistence_untouched",
            provider, model_id, str(e)
        )
        try:
            from config import get_default_model_for_provider
            import config as _config_mod
            fallback_provider = _config_mod.DEFAULT_PROVIDER or "mimo"
            fallback_model = get_default_model_for_provider(fallback_provider)
            if not fallback_model:
                # 极端兜底：直接用 ROUTE_TABLE 当前值（不修改）
                logger.error("webui.chat_model_fallback_no_default_model provider={}",
                             fallback_provider)
                return
            # C1 修复：回退所有 sync task，不只 chat
            # chat_pro/chat_flash 已合并进 chat
            _sync_tasks = ("chat",
                           "emotion_analysis", "tool_result_wrap",
                           "memory_encoding")
            _thinking_for_default = {"type": "disabled"}
            for _task in _sync_tasks:
                _old = registry.get_task(_task) or {}
                try:
                    registry.update_route(
                        _task,
                        model_id=fallback_model,
                        provider=fallback_provider,
                        max_tokens=_old.get("max_tokens"),
                        thinking=_thinking_for_default,
                        timeout=_old.get("timeout"),
                        persist=False,  # 不覆盖持久化，下次启动重试用户选择
                    )
                except (KeyError, RuntimeError) as route_err:
                    logger.warning("webui.chat_model_fallback_task_failed task={} error={}",
                                   _task, str(route_err))
            core.router._current_chat_model = {
                "provider": fallback_provider, "model_id": fallback_model,
            }
            logger.info("webui.chat_model_fallback_in_memory provider={} model={} tasks={}",
                        fallback_provider, fallback_model, _sync_tasks)
        except (ImportError, KeyError, AttributeError, OSError, ValueError) as inner_e:
            # CodeRabbit Nit: 内层 except 改具体类型，防止 metadata I/O/JSON 解析失败逃逸
            # 导致 WebUI 启动崩溃（原只捕 ImportError/KeyError/AttributeError）
            logger.error("webui.set_chat_model_fallback_error error={}", str(inner_e))


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
    # 微信 Bot：若有凭证自动启动长轮询（凭证由 WebUI 扫码登录保存）
    # 服务重启后自动恢复，保持登录状态；无凭证时静默跳过
    await _ensure_wechat_bot_task(app)
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


async def _ensure_wechat_bot_task(app: FastAPI) -> None:
    """若有微信凭证，自动启动 WeChatBotAdapter 长轮询。

    凭证由 WebUI 扫码登录后保存到 ~/.ai-agent/wechat_credentials.json。
    服务重启后自动恢复轮询，保持登录状态。启动失败不阻塞 WebUI（仅警告日志），
    用户可在设置页重新扫码登录。
    """
    try:
        from wechat_bot_adapter import WeChatBotAdapter, CREDENTIALS_PATH
        if not CREDENTIALS_PATH.exists():
            return
        core = app.state.core
        adapter = WeChatBotAdapter(
            db=core.db, router=core.router, api=None,
            user_openid="", core=core,
        )
        await adapter.start()
        # 仅当适配器真正连接上且轮询已启动时才记录为活跃并打成功日志。
        # start() 内部会吞掉 ILinkClient 初始化/轮询失败并返回正常，
        # 也可能因凭证文件为空 token 而"看似成功"。若未就绪仍挂到
        # app.state.wechat_bot，会把一个无效适配器误报为已恢复连接。
        connected = getattr(adapter, "_connected", False)
        poller = getattr(adapter, "_poll_task", None)
        if connected and poller is not None and not poller.done():
            app.state.wechat_bot = adapter
            logger.info("webui.wechat_bot_auto_started")
        else:
            logger.warning(
                "webui.wechat_bot_auto_start_not_ready connected={} has_poller={}",
                connected, poller is not None,
            )
    except (RuntimeError, OSError, ConnectionError, ImportError) as e:
        logger.warning(
            "webui.wechat_bot_auto_start_failed error={} type={}",
            str(e)[:200], type(e).__name__,
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[Any]:
    logger.info("webui.lifespan.start")
    _warn_unresolvable_web_master()
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

        # 治本修复（2026-08-05 用户"治标不治本"反馈）：预热 agnes + embed 连接。
        # 根因：httpx keepalive 连接过期后首次调用需重新 TCP+TLS 握手 6s，
        #   agnes 首次冷启动 12.5s（握手6s + thinking6.5s），12s timeout 卡边缘 →
        #   TimeoutError → 用户收不到回复（日志铁证）。
        # 预热：服务启动时后台 HEAD 请求建立连接，首次对话连接已热（0s 握手）。
        # keepalive_expiry=300s 保持连接热，正常对话间隔内不过期。

        from core.background_tasks import _spawn
        _spawn(_prewarm_connections())
        _spawn(_prewarm_local_singletons(core))

        # 恢复常驻本地推理：重启后自动启动 backend=local 节点绑定的模型实例
        _spawn(_restore_local_node_instances(core))

        # 恢复生成型节点后端选择：重启后生效，避免 WebUI 已保存的「本地/远程」被重置
        _spawn(_restore_generative_backends(core, app))

    # 启动事件循环阻塞 watchdog：检测同步阻塞并打印线程栈定位根因
    # 根因：后台任务集体卡 257-265s，_spawn timeout 无法取消同步阻塞
    _stop_watchdog = None
    try:
        from core.background_tasks import start_event_loop_watchdog, stop_event_loop_watchdog
        start_event_loop_watchdog()
        _stop_watchdog = stop_event_loop_watchdog
    except (ImportError, RuntimeError) as e:
        logger.warning("webui.watchdog_start_failed error={}", str(e))

    # 周期刷新运行实例健康状态：设备热插拔/失联后自动降级 degraded/device_unavailable，
    # 保证「可观测运行状态」完整落地（复审 Warning #2 闭环）。
    # 无运行实例时 refresh_health 内部遍历为空，仅剩设备 scan 开销，60s 一次可接受。
    try:
        from core.background_tasks import _spawn as _spawn_bg
        _instances = getattr(core, "local_ai_instances", None)
        if _instances is not None:
            app.state.local_ai_health_task = _spawn_bg(_local_ai_health_loop(_instances))
    except (ImportError, RuntimeError, OSError) as e:
        logger.warning("webui.local_ai_health_loop_start_failed error={}", str(e))

    yield

    # 停止 watchdog
    if _stop_watchdog is not None:
        try:
            _stop_watchdog()
        except (RuntimeError, OSError) as e:
            logger.warning("webui.stop_watchdog_failed error={}", str(e))

    logger.info("webui.lifespan.shutdown")
    await _shutdown_lifespan(app, core, owns_core)


async def _prewarm_connections() -> None:
    """预热 agnes + embed HTTP 连接（治本修复 2026-08-05）。"""
    import os as _os
    import httpx as _httpx
    # 预热 agnes
    try:
        _agnes_url = _os.getenv("AGNES_BASE_URL", "https://apihub.agnes-ai.cn/v1")
        from transports.agnes_transport import _get_agnes_http_client
        _c = _get_agnes_http_client()
        await _c.head(_agnes_url, timeout=_httpx.Timeout(10.0))
        logger.info("agnes.prewarm_done")
    except (ImportError, OSError, RuntimeError, TimeoutError) as _e:
        logger.debug("agnes.prewarm_failed: {}", _e)
    # 预热 embed (siliconflow)
    try:
        _embed_url = _os.getenv("EMBEDDING_BASE_URL", _os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"))
        from utils.http_pool import get_shared_client as _get_sc
        _c2 = _get_sc()
        await _c2.head(_embed_url, timeout=_httpx.Timeout(10.0))
        logger.info("embed.prewarm_done")
    except (ImportError, OSError, RuntimeError, TimeoutError) as _e:
        logger.debug("embed.prewarm_failed: {}", _e)


async def _prewarm_local_singletons(core: Any) -> None:
    """预热 XPSystem/MentalState/constraint/local_embed 单例（治本修复 2026-08-05/08）。"""
    import asyncio as _aio
    async def _warm_xp():
        try:
            from core.xp_system import get_xp_system
            _xp = get_xp_system()
            # 触发 _load() 加载到内存
            await _aio.to_thread(lambda: _xp.get_state("prewarm"))
            logger.info("xp.prewarm_done")
        except (ImportError, OSError, RuntimeError) as _e:
            logger.debug("xp.prewarm_failed: {}", _e)
    async def _warm_mental():
        try:
            from core.mental_state import get_mental_state_manager
            _mgr = get_mental_state_manager()
            # 触发 _load_or_init() 加载到内存
            await _aio.to_thread(lambda: _mgr.state)
            logger.info("mental.prewarm_done")
        except (ImportError, OSError, RuntimeError) as _e:
            logger.debug("mental.prewarm_failed: {}", _e)
    async def _warm_constraint():
        try:
            from core.constraint_injector import search_constraint_lessons
            await _aio.to_thread(search_constraint_lessons, "预热", top_k=1)
            logger.info("constraint.prewarm_done")
        except (ImportError, OSError, RuntimeError) as _e:
            logger.debug("constraint.prewarm_failed: {}", _e)
    # 治本修复（2026-08-08）：预热本地 NPU/CPU embedding provider。
    async def _warm_local_embed():
        try:
            _vec = getattr(getattr(core, "memory", None), "vec", None)
            if _vec is None or getattr(_vec, "_embed_mode", "") != "local":
                return
            status = await _aio.to_thread(_vec.start_local_engine)
            if status.get("engine_running"):
                logger.info("local_embed.prewarm_done")
            else:
                logger.debug("local_embed.prewarm_not_ready status={}", status)
        except (ImportError, OSError, RuntimeError) as _e:
            logger.debug("local_embed.prewarm_failed: {}", _e)
    await _aio.gather(_warm_xp(), _warm_mental(), _warm_constraint(),
                      _warm_local_embed())


async def _restore_local_node_instances(core: Any) -> None:
    try:
        from web.local_deploy_nodes import restore_local_instances
        from web.config_service import get_config_service
        await restore_local_instances(core, get_config_service())
        logger.info("local_deploy.instances_restored")
    except (ImportError, RuntimeError, OSError, ValueError) as _e:
        logger.warning("local_deploy.instances_restore_failed error={}", _e)


async def _restore_generative_backends(core: Any, app: FastAPI) -> None:
    try:
        from web.local_deploy_nodes import (
            NODES, apply_to_runtime, get_backend, get_local_model,
        )
        from web.config_service import get_config_service
        _cfg = get_config_service()
        for _node in NODES:
            if _node.get("kind") != "generative":
                continue
            _node_id = _node["id"]
            _backend = get_backend(_cfg, _node_id)
            _local_model = get_local_model(_cfg, _node_id) or None
            apply_to_runtime(core, None, _node_id, _backend, app=app, local_model=_local_model)
        logger.info("local_deploy.generative_backends_restored")
    except (ImportError, KeyError, RuntimeError, OSError, ValueError) as _e:
        logger.warning("local_deploy.generative_backends_restore_failed error={}", _e)


async def _local_ai_health_loop(instances: Any) -> None:
    import asyncio as _aio
    refresh_task: _aio.Task | None = None
    while True:
        await _aio.sleep(60)
        # 单飞 + 超时：refresh_health 内部做同步设备扫描（/sys 遍历、
        # subprocess、onnxruntime provider 验证），实测出现过单次挂起
        # 65001s（18h），期间健康状态整天不刷新。超时后放弃本轮，下轮
        # 重试；上一轮未完成则跳过（避免任务堆积）。
        if refresh_task is not None and not refresh_task.done():
            logger.debug("local_ai.health_refresh_skipped_previous_running")
            continue
        refresh_task = _aio.create_task(instances.refresh_health())
        try:
            await _aio.wait_for(_aio.shield(refresh_task), timeout=120.0)
        except _aio.TimeoutError:
            logger.warning("local_ai.health_refresh_timeout timeout=120s")
        except (RuntimeError, OSError) as _e:
            logger.debug("local_ai.health_refresh_failed error={}", _e)


async def _init_lifespan_resources(app: FastAPI) -> tuple[Any, bool]:
    """初始化 core、配置服务与 agent registry, 返回 (core, owns_core)"""
    from agent_core import AgentCore
    from web.agent_registry import AgentRegistry
    from web.config_service import get_config_service
    from llm_gateway.provider_service import ProviderService
    from config import get_provider_catalog
    from web.routers.local_ai import initialize_local_ai_services
    from web.ws_hub import manager

    core = getattr(app.state, "core", None)
    owns_core = core is None
    if owns_core:
        core = AgentCore()
        await core.init()
    app.state.core = core

    config_service = get_config_service()
    app.state.provider_service = ProviderService(config_service, get_provider_catalog(), core.router)
    core.provider_service = app.state.provider_service

    registry = AgentRegistry(core)
    await registry.load_persisted()
    app.state.agent_registry = registry
    from config import DATA_DIR
    await initialize_local_ai_services(
        app,
        core,
        manager.broadcast,
        Path(DATA_DIR) / "local_ai_downloads.json",
    )
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
                _os.chmod(_env_path, 0o600)
                logger.info("webui.env_created_from_example")
            else:
                with open(_env_path, "w", encoding="utf-8") as _f:
                    _f.write("")
                _os.chmod(_env_path, 0o600)
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
            if pid in ("ollama", "llama.cpp"):
                # 本地无 key 接口不需要 API key，由下方第 4 步检查 base_url
                continue
            if load_provider_key(pid).strip():
                return True
    except (ImportError, OSError, ValueError) as e:
        logger.warning("webui.custom_provider_credential_check_failed error={}", str(e))

    # 4. 本地无 key 接口（Ollama / llama.cpp）：不需要 API key，仅看 .env 是否显式配置 base_url
    #    （与 _apply_model_overrides 的注册条件一致，本地-only 部署不误入降级模式）
    try:
        from setup_wizard import _load_env_values
        _env = _load_env_values()
        if _env.get("OLLAMA_BASE_URL", "").strip() or _env.get("LLAMA_CPP_BASE_URL", "").strip():
            return True
    except (ImportError, OSError, ValueError):
        logger.debug("server.local_provider_url_check_failed", exc_info=True)

    return False


async def _shutdown_lifespan(app: FastAPI, core: Any, owns_core: bool) -> None:
    """关闭服务与资源: qq_task / 插件 / 调度器 / media / core"""
    from web.ws_hub import manager, stop_media_cleanup

    await stop_media_cleanup()
    await manager.shutdown()
    local_ai = getattr(app.state, "local_ai", None)
    if local_ai is not None:
        await local_ai.shutdown()
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
    # 取消本地 AI 实例健康检查周期任务
    _local_ai_health_task = getattr(app.state, "local_ai_health_task", None)
    if _local_ai_health_task and not _local_ai_health_task.done():
        _local_ai_health_task.cancel()
        with suppress(asyncio.CancelledError, RuntimeError):
            await _local_ai_health_task
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
                logger.debug("server.{}_stop_error", attr, exc_info=True)
    # 停止微信长轮询（避免 poller/ILinkClient 在 graceful shutdown 期间无人管理）
    wechat_bot = getattr(app.state, "wechat_bot", None)
    if wechat_bot is not None:
        try:
            await wechat_bot.stop()
            logger.info("webui.wechat_bot_stopped")
        except (RuntimeError, OSError, asyncio.CancelledError):
            logger.debug("server.wechat_bot_stop_error", exc_info=True)
        app.state.wechat_bot = None
    if owns_core:
        try:
            await core.shutdown()
        except (RuntimeError, OSError):
            logger.debug("server.core_shutdown_error", exc_info=True)


def _warn_unresolvable_web_master() -> None:
    """P2：web 主人身份不可解析时启动告警。

    web 通道以 MASTER_QQ_OPENID（或 OWNER_IDS）作为主人身份解析依据
    （ws_hub 传入 user_id）。两者均未配置时，web 登录用户会被判为非主人
    ——按安全设计 fail-closed（非主人工具门禁会拒绝 EXECUTE 工具），
    但对"纯 web 部署、不用 QQ"的场景是配置错误，需要显式引导而非静默降级。
    """
    import os as _os
    if (_os.getenv("MASTER_QQ_OPENID", "").strip()
            or _os.getenv("OWNER_IDS", "").strip()):
        return
    logger.warning(
        "webui.master_identity_unresolved hint=设置 MASTER_QQ_OPENID 或 OWNER_IDS "
        "（含 webui 本机标识）后 web 登录用户才会被识别为主人；"
        "当前 web 用户将被按非主人处理（工具白名单受限）"
    )


def _read_version() -> str:
    try:
        from pathlib import Path as _P
        return (_P(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return "0.4.95"


def _add_rate_limit_middleware(app: FastAPI) -> None:
    from web.middleware.rate_limit import RateLimitMiddleware
    try:
        from config import DATA_DIR
        _rate_limit_db = str(Path(DATA_DIR) / "rate_limit_buckets.sqlite")
    except (ImportError, AttributeError):
        logger.debug("server.config_fallback_error", exc_info=True)
        _rate_limit_db = str(Path(__file__).parent.parent / "data" / "rate_limit_buckets.sqlite")
    app.add_middleware(RateLimitMiddleware, persist_path=_rate_limit_db)
    # API 响应 gzip 压缩（insight/memories 等大 JSON 实测 55KB 未压缩传输；
    # minimum_size=1000 避免小响应的压缩开销反噬）
    from fastapi.middleware.gzip import GZipMiddleware
    app.add_middleware(GZipMiddleware, minimum_size=1000)


def _add_security_and_sla_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def _allow_frame_embed(request: Any, call_next: Any) -> Any:
        import time as _time
        from utils.trace_context import new_trace_id
        _trace_id = new_trace_id()
        _start = _time.monotonic()
        response = await call_next(request)
        _elapsed = _time.monotonic() - _start
        _sla = getattr(app.state, "sla_exporter", None)
        if _sla and request.url.path != "/metrics":
            _sla.inc_request(request.url.path, str(response.status_code))
            _sla.observe_latency(request.url.path, _elapsed)
            if response.status_code >= 400:
                _sla.inc_error(f"http_{response.status_code}", request.url.path)
        response.headers["X-Trace-Id"] = _trace_id
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "media-src 'self' data: blob:; "
            "font-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
            "frame-ancestors 'self' http://127.0.0.1:18089 http://localhost:18089; "
            "object-src 'none'; base-uri 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        new_token = getattr(request.state, "new_token", None)
        if new_token:
            response.headers["X-New-Token"] = new_token
            new_expiry = getattr(request.state, "new_expiry", 0)
            if new_expiry:
                response.headers["X-New-Token-Expiry"] = str(int(new_expiry))
            from web.routers.auth import set_media_cookie
            set_media_cookie(response, new_token, float(new_expiry or 0))
        return response


def _register_routes(app: FastAPI) -> None:
    from web.routers.auth import router as auth_router
    from web.routers.chat import router as chat_router
    from web.routers.system import router as system_router, public_router as system_public_router
    from web.routers.agents import router as agents_router
    from web.routers.models import router as models_router
    from web.routers.providers import router as providers_router
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
    from web.routers.workflows_v2 import router as workflows_v2_router
    from web.routers.workspace import router as workspace_router
    from web.routers.wechat import router as wechat_router, public_router as wechat_public_router
    from web.routers.local_ai import router as local_ai_router
    from web.routers.local_deploy import router as local_deploy_router
    from web.routers.local_ai_storage import router as local_ai_storage_router
    from web.routers.retrieval import router as retrieval_router
    from web.routers.jspace import router as jspace_router

    for r in (auth_router, chat_router, system_router, agents_router,
              models_router, providers_router, tools_router, mcp_router, insight_router,
              schedule_router, media_router, health_router, plugins_router,
              setup_router, model_discovery_router, market_router,
              mail_manage_router, workflows_router, workflows_v2_router, workspace_router,
              wechat_router, local_ai_router, local_deploy_router, local_ai_storage_router,
              retrieval_router, jspace_router,
              system_public_router, wechat_public_router):
        app.include_router(r, prefix="/api/v1")

    from web.ws_hub import router as ws_router
    app.include_router(ws_router)


def _register_metrics_if_enabled(app: FastAPI) -> None:
    metrics_enabled_env = os.getenv("METRICS_ENABLED")
    if metrics_enabled_env is not None:
        metrics_enabled = metrics_enabled_env.lower() in ("true", "1", "yes")
        logger.info("webui.metrics_endpoint_env_override enabled={}", metrics_enabled)
    else:
        try:
            from web.config_service import get_config_service
            cfg = get_config_service()
            metrics_enabled = bool(
                cfg.get("observability.metrics_enabled", True)
            )
        except (ImportError, ValueError, RuntimeError, OSError) as e:
            logger.warning("webui.metrics_endpoint_config_read_failed err={}", e)
            metrics_enabled = True
        logger.info("webui.metrics_endpoint_config enabled={}", metrics_enabled)
    if metrics_enabled:
        from web.routers.metrics import router as metrics_router
        app.include_router(metrics_router)
        logger.info("webui.metrics_endpoint_enabled")
    else:
        logger.info("webui.metrics_endpoint_disabled")


def _mount_static_files(app: FastAPI) -> None:
    try:
        from config import MEDIA_DIR
        media_dir = MEDIA_DIR
    except ImportError:
        media_dir = Path(__file__).parent / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    from web.media_auth import AuthStaticFiles
    app.mount("/media", AuthStaticFiles(directory=str(media_dir), follow_symlink=True),
              name="media")

    dist_dir = Path(__file__).parent / "dist"
    if dist_dir.exists():
        app.mount("/", NoCacheHTMLStaticFiles(directory=str(dist_dir), html=True), name="spa")


def create_app() -> FastAPI:
    app = FastAPI(title="Xiaoda Agent WebUI", version=_read_version(), lifespan=lifespan)

    _add_rate_limit_middleware(app)
    _add_security_and_sla_middleware(app)

    from web.error_handler import register_error_handlers
    register_error_handlers(app)

    _register_routes(app)

    from core.sla_exporter import get_sla_exporter
    app.state.sla_exporter = get_sla_exporter()

    _register_metrics_if_enabled(app)
    _mount_static_files(app)

    from web.app_ref import set_app
    set_app(app)

    return app


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