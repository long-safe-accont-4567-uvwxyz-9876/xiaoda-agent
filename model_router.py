import asyncio
import contextlib
import os
import threading
import time
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import openai as _openai_mod  # 用于 openai.APIError 异常捕获
from loguru import logger
from openai import AsyncOpenAI

from config import AGNES_BASE_URL, AGNES_TEXT_MODEL, PROMPT_CACHING_ENABLED
from config import DEFAULT_PROVIDER as _CFG_DEFAULT_PROVIDER
from config import FLASH_MODEL_NAME as _CFG_FLASH_MODEL
from config import MODEL_NAME as _CFG_MODEL_NAME
from config import get_builtin_providers as _get_builtin_providers
from config import set_default_provider as _set_default_provider
from core.app_exception import LLMError
from core.error_codes import ErrorCodeEnum
from db.db_analytics import AnalyticsDB
from llm_gateway.client_lifecycle import ClientLifecycleMixin, _ssrf_check  # noqa: F401,E402
from llm_gateway.fallback_chain import FallbackChainMixin  # noqa: E402

# ── Phase 5 拆分：成本/缓存统计块抽为 llm_gateway/router_metrics.CostTrackingMixin ──
# _reasoning_content_var 随 pop_reasoning_content 搬入 mixin，此处同名引入仍供
# _handle_route_response 使用；CostTrackingMixin 同名 re-export 保持外部 import 兼容。
from llm_gateway.router_metrics import CostTrackingMixin, _reasoning_content_var  # noqa: F401,E402
from llm_gateway.transports import CompletionRequest, TransportError

# ── Phase 1 拆分：provider 配置块抽为 model_router_config（函数体逐字节搬移） ──
# 同名 re-export 保持兼容：`from model_router import MIMO_MODEL` 与
# `patch("model_router.MIMO_MODEL")` 等既有用法不受影响。
from model_router_config import (  # noqa: F401,E402
    _CROSS_PROVIDER_MAP,
    _LOCAL_ORT_PROVIDER,
    _OLLAMA_DEFAULT_MODEL,
    _OLLAMA_MODEL_MAP,
    _PROVIDER_CAPS_FROM_FILE,
    _PROVIDER_METADATA,
    MIMO_API_KEY,
    MIMO_BASE_URL,
    MIMO_MODEL,
    MIMO_PRICING,
    MIMO_PRO_MODEL,
    PROVIDER_MAX_TOKENS_CAP,
    PROVIDER_PRICING,
    _apply_env_cross_provider_fallback,
    _cross_provider_map_from_file,
    _env_max_tokens_cap,
    _file_max_tokens_cap,
    _load_cross_provider_map,
    _load_ollama_model_map,
    _load_provider_base_url,
    _load_provider_max_tokens_cap,
    _load_provider_metadata,
    _ollama_default_model_from_env,
    _ollama_model_map_from_env,
    _ollama_model_map_from_file,
    _resolve_provider_key,
    translate_model_for_provider,
)

# ── Phase 2 拆分：ModelRouteRegistry 抽为 model_router_registry（逐字节搬移） ──
from model_router_registry import ModelRouteRegistry as ModelRouteRegistry  # noqa: F401,E402
from transports import AgnesTransport, MiMoTransport, ProviderTransport

# 根因修复：agnes API connect=5s 过短导致 APIConnectionError，统一从 agnes_transport 引入共享 httpx 配置
from transports.agnes_transport import AGNES_HTTP_TIMEOUT, _get_agnes_http_client
from utils.common import DEFAULT_MAX_TOKENS
from utils.credential_pool import get_credential_pool
from utils.error_classifier import ErrorClassifier, RecoveryAction
from utils.llm_cleanup import merge_continuation
from utils.metrics import metrics
from utils.prompt_caching import apply_cache_control

ROUTE_TABLE = {
    # chat 主路由：128K 上限，支撑长时间连贯对话，搭配滑动窗口+摘要压缩避免退化
    # 不再锁死 8192，避免长会话频繁截断历史导致记忆断裂
    "chat": {"model": _CFG_MODEL_NAME, "max_tokens": 131072, "client": _CFG_DEFAULT_PROVIDER, "thinking": {"type": "disabled"}},
    "emotion_analysis": {"model": _CFG_FLASH_MODEL or _CFG_MODEL_NAME, "max_tokens": 1024, "client": _CFG_DEFAULT_PROVIDER, "thinking": {"type": "disabled"}},
    "tool_result_wrap": {"model": _CFG_FLASH_MODEL or _CFG_MODEL_NAME, "max_tokens": 2048, "client": _CFG_DEFAULT_PROVIDER, "thinking": {"type": "disabled"}},
    "memory_encoding": {"model": _CFG_FLASH_MODEL or _CFG_MODEL_NAME, "max_tokens": DEFAULT_MAX_TOKENS, "client": _CFG_DEFAULT_PROVIDER, "thinking": {"type": "disabled"}},
    "chat_agnes": {"model": AGNES_TEXT_MODEL, "max_tokens": 131072, "client": "agnes", "thinking": {"type": "disabled"},
                   "presence_penalty": 0.3, "frequency_penalty": 0.0},
}

# 模型偏好 label 表（默认 provider 展示用）。
# 已废弃的 mimo-pro/mimo-flash/mimo-mini 预设已被移除——模型切换统一走动态
# provider/模型（对齐 WebUI 模型选择 button），不再支持预设字符串切换。
MODEL_PREFERENCES = {
    "mimo": {"label": "MiMo 模式", "desc": "使用小米 MiMo-V2.5 模型"},
}

# P0 修复（2026-08-05 用户要求"10秒内响应"）：移除 'timeout'。
# 根因：agnes APITimeoutError 被错误分类为 connection_error（APITimeoutError 是
#   APIConnectionError 子类，已在 error_classifier 修复检查顺序）→ 触发重试 →
#   30s+30s=60s 阻塞（日志 main_path=67214ms 铁证）。
# 移除 timeout 后：agnes 超时直接降级，不双倍等待。agnes 正常 6-7s，read=8s
# 覆盖正常耗时；偶发超时降级返回提示，比等 60s 好。
# connection_error 保留重试（握手失败重试有意义，且 connect 慢的情况少见）。
RETRYABLE_ERRORS = {'rate_limit', 'connection_error'}
MAX_RETRIES = 1
# per-provider LLM 调用并发上限：agnes 支持最多 3 并发，统一各 provider 上限为 3，
# 取代原先 asyncio.Lock 的串行（1 并发）。凭证轮换/客户端刷新仍走 _get_credential_lock 串行。
MAX_PROVIDER_CONCURRENCY = 3
# chat_pro/chat_flash 已合并进 chat（同一 provider 同一 model，无区分意义）
# 降级链：chat 失败 → chat_agnes（agnes provider 作为独立兜底）
FALLBACK_ROUTE = {
    "chat": "chat_agnes",
}


def list_discovered_model_ids() -> list[str]:
    """返回所有已发现模型的 'provider/model' 标识（供 CLI /model 参数补全）。

    与 GET /models/discover 一致，动态枚举（非硬编码）。模型发现缓存为空时返回空列表。
    """
    ids: list[str] = []
    try:
        from web._discovery_cache import _cache as _disc_cache
        _data = (_disc_cache.get("data") or []) if _disc_cache else []
        for _pg in _data:
            _provider = _pg.get("provider", "")
            for _m in (_pg.get("models") or []):
                _mid = _m.get("id", "")
                if _provider and _mid:
                    ids.append(f"{_provider}/{_mid}")
    except (ImportError, KeyError, ValueError, OSError):
        logger.debug("router.discovered_model_ids_failed", exc_info=True)
    return sorted(ids)


class ModelRouter(CostTrackingMixin, ClientLifecycleMixin, FallbackChainMixin):
    """模型路由器，按任务类型选择模型/Provider 并处理重试与凭证轮换。"""

    _DEFAULT_TIMEOUTS: ClassVar[dict[str, int]] = {
        "emotion_analysis": 10,
        "emotion": 10,
        # P0 修复（2026-08-04 实证阻塞）：60→30。
        # 根因：agnes 正常 6-7s 响应，但事件循环被 DB 操作偶发阻塞时，
        # agnes HTTP 响应收不到 → asyncio.wait_for(create, timeout=60) 等 60s 才超时 →
        # retry 再 30s = 90s+ 阻塞（日志 22:53:37 llm_verify=83196ms 铁证）。
        # 降到 30s：agnes 正常 9s 完成（6s 响应+3s 处理），30s 余量足够；
        # 慢时 30s 快速失败 + retry，最坏 60s 而非 120s。
        "chat": 30,
        "tool_call": 30,
        "image_gen": 90,
    }
    # 后台 LLM task 集合：这些 task 调 LLM 但不直接面向用户，
    # 必须让路于主 chat（task_type="chat"），避免并发竞争 agnes API。
    _BG_LLM_TASKS: ClassVar[set[str]] = {
        "memory_encoding", "emotion_analysis", "tool_result_wrap", "entity_extraction",
    }

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 api_key_2: str | None = None, db: Any=None) -> None:
        self.TASK_TIMEOUTS: dict[str, int] = dict(self._DEFAULT_TIMEOUTS)
        # 从 os.getenv() 实时读取，避免使用模块级冻结变量；fallback 到 provider_metadata.json 派生的 MIMO_BASE_URL
        _mimo_key = api_key or _resolve_provider_key("MIMO_API_KEY")
        _mimo_url = base_url or os.getenv("MIMO_BASE_URL") or MIMO_BASE_URL
        _ssrf_check(_mimo_url)  # SSRF 防护：校验 base_url
        self._client = AsyncOpenAI(api_key=_mimo_key, base_url=_mimo_url) if _mimo_key else None
        self._db = db
        # 默认偏好跟随 DEFAULT_PROVIDER（默认 mimo，但通过 provider_metadata.json 表达）
        self._model_preference = _CFG_DEFAULT_PROVIDER
        self._cost_buffer: list[dict] = []
        self._cost_flush_threshold = 3
        self._last_cache_warning = 0.0
        # _analytics / _db 默认 None：set_db() 未被调用时 route() 访问这两个属性会
        # AttributeError，导致所有 LLM 调用 call_failed（"所有功能都坏"事故根因）。
        # 此处兜底初始化，set_db() 被调用时会覆盖为真实实例。
        self._analytics = None
        self._db = None
        self._error_classifier = ErrorClassifier()
        self._credential_pool = get_credential_pool()
        self._credential_locks: dict[str, asyncio.Lock] = {}
        self._provider_semaphores: dict[str, asyncio.Semaphore] = {}
        # agnes 密钥优先级：环境变量 → 加密文件 → credential_pool
        # 根因：用户通过 Web UI 添加 agnes 时，密钥保存在加密文件，但 __init__ 只从环境变量读取
        _agnes_key = os.getenv("AGNES_API_KEY", "")
        if not _agnes_key:
            try:
                from web._provider_keys import load_provider_key
                _agnes_key = load_provider_key("agnes") or ""
            except Exception:
                logger.debug("router.agnes_key_file_load_failed", exc_info=True)
        if not _agnes_key:
            try:
                cred = self._credential_pool.get("agnes")
                if cred:
                    _agnes_key = cred.api_key
            except Exception:
                logger.debug("router.agnes_key_pool_load_failed", exc_info=True)
        _agnes_url = os.getenv("AGNES_BASE_URL", AGNES_BASE_URL)
        _ssrf_check(_agnes_url)  # SSRF 防护：校验 base_url
        self._agnes_client = (
            AsyncOpenAI(
                api_key=_agnes_key,
                base_url=_agnes_url,
                http_client=_get_agnes_http_client(),
                timeout=AGNES_HTTP_TIMEOUT,
                max_retries=0,
            ) if _agnes_key else None
        )

        self._custom_clients_lock = threading.Lock()
        self._custom_clients: dict[str, AsyncOpenAI] = {}
        self._register_credential_pool_providers()
        # 路由表注册中心：ROUTE_TABLE 的唯一读写入口
        # 启动后 ROUTE_TABLE 模块级变量作为只读快照，所有修改走 _registry
        self._registry: ModelRouteRegistry = ModelRouteRegistry(ROUTE_TABLE)
        self._current_chat_model: dict | None = None
        # 主 chat 优先机制：后台 LLM 任务让路，避免并发竞争 agnes API
        # 根因：agnes API 对同 key 并发请求严重排队（实测并发3→32s，串行→19s），
        #       后台 LLM 任务（memory_encoding/emotion_analysis/instinct 等）和主 chat
        #       并发调 agnes → 主 chat 60s 超时 → retry → 83s 阻塞。
        # 修复：主 chat 期间 _chat_idle.clear()，后台 LLM 任务 await _chat_idle.wait() 让路；
        #       后台任务之间用 _bg_llm_semaphore(1) 串行，彻底消除并发竞争。
        self._chat_idle = asyncio.Event()
        self._chat_idle.set()  # 初始空闲
        self._bg_llm_semaphore = asyncio.Semaphore(1)
        # 用户硬约束（2026-08-04）：删除全局 _llm_call_gate 锁。
        # 根因：全局锁让所有 provider 的 LLM 调用串行，主 chat 调用 agnes 时，
        # 即使是不同 provider 的调用也被阻塞 → 阻塞级联。
        # 改为 per-provider 并发信号量（_get_provider_call_semaphore），每个 provider
        # 最多 MAX_PROVIDER_CONCURRENCY 并发（agnes 支持最多 3 并发），且不阻塞其他 provider。
        # 详见 _route_with_retry / _create_completion / chat_stream。
        self._active_bg_llm_tasks: set[asyncio.Task] = set()
        self._cache_stats = {
            "total_calls": 0,
            "hit_tokens": 0,
            "miss_tokens": 0,
        }
        # P6: 缓存命中统计累计计数器（每 100 次请求输出一次统计）
        self._request_count = 0
        self._cached_tokens_total = 0

        self._transports: dict[str, ProviderTransport] = {}
        mimo = MiMoTransport()
        if mimo.is_available():
            self._transports["mimo"] = mimo
        agnes = AgnesTransport()
        if agnes.is_available():
            self._transports["agnes"] = agnes
        logger.info("router.transports", available=list(self._transports.keys()))
    def set_local_transport(self, transport: ProviderTransport) -> None:
        self._transports[_LOCAL_ORT_PROVIDER] = transport

    def _get_provider_call_semaphore(self, provider: str) -> asyncio.Semaphore:
        """返回指定 provider 的 LLM 调用并发信号量（最大 MAX_PROVIDER_CONCURRENCY 并发）。

        与 _get_credential_lock 分离：前者限制 LLM 调用并发（agnes 最多 3 并发），
        后者仍用于凭证轮换/客户端刷新等需要串行的变更操作。
        懒创建以兼容 __new__ 构造的最小实例（测试用）。
        """
        semaphores = getattr(self, "_provider_semaphores", None)
        if semaphores is None:
            semaphores = {}
            self._provider_semaphores = semaphores
        return semaphores.setdefault(
            provider, asyncio.Semaphore(MAX_PROVIDER_CONCURRENCY))

    def _get_custom_clients_lock(self) -> threading.Lock:
        """返回保护 _custom_clients 的统一锁，按需创建。

        生产实例在 __init__ 中已创建；测试中通过 ModelRouter.__new__ 构造的
        最小实例未走 __init__，这里懒创建以兼容。
        """
        lock = getattr(self, "_custom_clients_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._custom_clients_lock = lock
        return lock

    def get_custom_client(self, provider_id: str) -> Any | None:
        with self._get_custom_clients_lock():
            return self._custom_clients.get(provider_id)

    def set_custom_client(self, provider_id: str, client: Any) -> None:
        with self._get_custom_clients_lock():
            self._custom_clients[provider_id] = client

    def remove_custom_client(self, provider_id: str) -> None:
        with self._get_custom_clients_lock():
            self._custom_clients.pop(provider_id, None)

    def has_custom_client(self, provider_id: str) -> bool:
        with self._get_custom_clients_lock():
            return provider_id in self._custom_clients

    def list_custom_clients(self) -> list[tuple[str, Any]]:
        with self._get_custom_clients_lock():
            return list(self._custom_clients.items())

    def clear_custom_clients(self) -> None:
        with self._get_custom_clients_lock():
            self._custom_clients.clear()

    def set_db(self, db: Any, analytics: AnalyticsDB | None = None) -> None:
        self._db = db
        self._analytics = analytics

    def list_transports(self) -> list[str]:
        """返回所有可用 transport 名称"""
        return list(self._transports.keys())

    def get_transport(self, provider: str) -> ProviderTransport | None:
        """获取指定提供商的 Transport"""
        return self._transports.get(provider)

    def set_chat_model(self, provider: str, model_id: str) -> dict:
        """切换 chat 主模型，原子化更新所有同步路由。

        真正的 all-or-nothing 事务（CodeRabbit#1 + Qodo#1 修复）：
        1. 先验证 provider 可用（已注册），未注册直接抛（此时还没改任何状态）
        2. 暂存所有 sync task 旧值快照 + DEFAULT_PROVIDER 旧值
        3. 逐个原子更新所有 sync task（每个 update_route 内部内存+持久化原子）
        4. 任一 task 失败：回滚所有已更新 task（内存+持久化），DEFAULT_PROVIDER 未改无需回滚
        5. 全部 task 成功后才发布 DEFAULT_PROVIDER 和 models.chat_model
        6. chat_model 写入失败也要回滚 Step 3 + DEFAULT_PROVIDER

        旧实现缺陷（已修复）：
        - Step 2 提前改 DEFAULT_PROVIDER，失败时不回滚 → 子代理/成本统计走错 provider
        - Step 5 独立写 chat_model，失败时不回滚 Step 4 → routes 新值但 chat_model 旧值
          重启时 _restore_chat_model 用旧 chat_model 覆盖正确的 ROUTE_TABLE["chat"]
        - timeout 用 self.TASK_TIMEOUTS.get(_task) 读取运行时值，可能固化误改；
          现在用 old_entry.get("timeout") 保留原持久化值

        Args:
            provider: provider 名称
            model_id: 模型 ID

        Returns:
            {"provider": ..., "model_id": ...}

        Raises:
            LLMError: provider 未注册，或事务回滚后重新抛出
        """
        # Step 1: 先验证 provider 可用，未注册直接抛（此时还没改任何状态）
        # N-2 修复：内置 provider 集合从 provider_metadata.json 派生，不硬编码
        # local-ort 为本地 ONNX Runtime GenAI provider，无需云端 client 即可切换。
        if provider != _LOCAL_ORT_PROVIDER:
            if provider not in _get_builtin_providers():
                if not self.has_custom_client(provider):
                    self._lazy_register_provider(provider)
                if not self.has_custom_client(provider):
                    raise LLMError(f"自定义 provider {provider} 未注册，请先注册客户端")

        # Step 2: 保存 DEFAULT_PROVIDER 当前值用于失败回滚
        # 注意：必须用 config.DEFAULT_PROVIDER 实时读取，不能用模块级 import 快照
        import config as _config_mod
        _old_default_provider = _config_mod.DEFAULT_PROVIDER

        # Step 3: 收集所有需要同步的 task（chat 主路由 + 同步 task）
        # chat_pro/chat_flash 已合并进 chat
        _sync_tasks = ("chat",
                       "emotion_analysis", "tool_result_wrap",
                       "memory_encoding")

        # agnes 不支持 thinking，切换到 agnes 时所有 task 禁用 thinking
        _thinking_for_agnes = {"type": "disabled"}

        # Step 4: 暂存快照 + 逐个原子更新（任一失败回滚所有已提交 task）
        _updated_tasks: list[str] = []
        _snapshots: dict[str, dict | None] = {}
        for _task in _sync_tasks:
            if _task not in self._registry.all_tasks():
                continue
            old_entry = self._registry.get_task(_task) or {}
            _snapshots[_task] = old_entry
            _thinking = _thinking_for_agnes if provider == "agnes" else old_entry.get("thinking")
            try:
                self._registry.update_route(
                    _task,
                    model_id=model_id,
                    provider=provider,
                    max_tokens=old_entry.get("max_tokens"),
                    thinking=_thinking,
                    timeout=old_entry.get("timeout"),  # Qodo#3: 保留原 timeout，不用 TASK_TIMEOUTS
                )
                _updated_tasks.append(_task)
            except Exception as e:
                # 失败回滚所有已更新 task（内存 + 持久化）
                logger.error("router.set_chat_model_task_failed task={} error={}",
                             _task, str(e))
                for _done_task in _updated_tasks:
                    _old = _snapshots.get(_done_task) or {}
                    try:
                        self._registry.update_route(
                            _done_task,
                            model_id=_old.get("model", ""),
                            provider=_old.get("client", ""),
                            max_tokens=_old.get("max_tokens"),
                            thinking=_old.get("thinking"),
                            timeout=_old.get("timeout"),
                        )
                    except Exception as rollback_err:
                        logger.error("router.set_chat_model_rollback_failed task={} error={}",
                                     _done_task, str(rollback_err))
                # DEFAULT_PROVIDER 未改，无需回滚
                raise LLMError(
                    f"切换 chat 模型时 task {_task} 失败，已回滚 {_updated_tasks}: {e}"
                ) from e

        logger.info("router.all_tasks_synced",
                    provider=provider, model=model_id,
                    synced_tasks=_updated_tasks)

        # Step 5: 所有 task 成功后才发布 DEFAULT_PROVIDER
        # （之前未改，失败无需回滚；放在这里保证 routes 与 provider 同步切换）
        _set_default_provider(provider)

        # Step 6: 同步 chat_model 字段到 ConfigService（WebUI 显示用）
        # CodeRabbit#1 修复：chat_model 写入失败时回滚 Step 4 的所有 task + DEFAULT_PROVIDER
        try:
            from web.config_service import get_config_service
            cfg = get_config_service()
            cfg.set("models.chat_model", {"provider": provider, "model_id": model_id})
        except Exception as e:
            logger.error("router.chat_model_persist_failed error={} rolling_back_tasks={}",
                         str(e), _updated_tasks)
            for _done_task in _updated_tasks:
                _old = _snapshots.get(_done_task) or {}
                try:
                    self._registry.update_route(
                        _done_task,
                        model_id=_old.get("model", ""),
                        provider=_old.get("client", ""),
                        max_tokens=_old.get("max_tokens"),
                        thinking=_old.get("thinking"),
                        timeout=_old.get("timeout"),
                    )
                except Exception as rollback_err:
                    logger.error("router.set_chat_model_chat_model_rollback_failed task={} error={}",
                                 _done_task, str(rollback_err))
            # DEFAULT_PROVIDER 也要回滚
            _set_default_provider(_old_default_provider)
            raise LLMError(f"持久化 chat_model 失败，已回滚所有 task 和 DEFAULT_PROVIDER: {e}") from e

        self._current_chat_model = {"provider": provider, "model_id": model_id}
        logger.info("router.chat_model_changed", provider=provider, model=model_id)
        return {"provider": provider, "model_id": model_id}

    def get_current_chat_model(self) -> dict:
        if self._current_chat_model is not None:
            return self._current_chat_model
        return {"provider": _CFG_DEFAULT_PROVIDER, "model_id": ROUTE_TABLE.get("chat", {}).get("model", _CFG_MODEL_NAME)}

    def get_max_tokens_for_task(self, task_type: str = "chat") -> int:
        """获取指定 task_type 的 max_tokens（上下文窗口大小）。

        用于 AgentContext 动态计算压缩阈值，避免硬编码不分模型的问题。
        若 task_type 不存在或字段缺失，返回保守兜底值 60000。
        """
        cfg = self._registry.get_task_ref(task_type) or self._registry.get_task_ref("chat") or {}
        return int(cfg.get("max_tokens", 60000))

    def get_active_max_tokens(self) -> int:
        """获取当前激活模型偏好的实际上下文窗口大小。

        直接解析 chat 主路由的 max_tokens（模型切换统一走动态 provider/模型，
        已废弃的 mimo-pro/mimo-flash/mimo-mini 预设不再参与 task_type 解析）。
        供 AgentContext 动态压缩阈值使用。
        """
        task_type = self.resolve_task_type("chat")
        return self.get_max_tokens_for_task(task_type)

    # 已知自定义 provider 的默认模型映射
    # 注意：这些是 fallback 值，当 provider 的 default_model 为空时使用
    # 建议通过 /models/health-check 端点定期验证这些模型ID是否仍然可用
    _CUSTOM_PROVIDER_DEFAULT_MODELS: ClassVar[dict[str, str]] = {
        "ollama": "qwen2.5:latest",
        "llama.cpp": "",
        "siliconflow": "THUDM/GLM-4-9B-0414",
        "openrouter": "openrouter/free",
        "modelscope": "Qwen/Qwen3-8B",
    }

    def _get_custom_provider_default_model(self, provider: str) -> str:
        """获取自定义 provider 的默认模型 ID。"""
        # 优先从配置服务获取
        try:
            from web.config_service import get_config_service
            cfg = get_config_service()
            record = cfg.get(f"models.providers.{provider}", {}) or {}
            dm = record.get("default_model", "")
            if dm:
                return dm
        except (KeyError, AttributeError, TypeError):
            logger.debug("model_router.default_model_lookup_failed", exc_info=True)
        # 回退到内置映射
        return self._CUSTOM_PROVIDER_DEFAULT_MODELS.get(provider, "")

    def set_model_preference(self, preference: str) -> bool:
        if preference in MODEL_PREFERENCES:
            self._model_preference = preference
            logger.info("router.preference_changed", preference=preference)
            return True
        if "/" in preference:
            # 仅记录偏好标记，不再调用 set_chat_model 触发切换。
            # set_chat_model 是 CLI 与 WebUI 共用的唯一切换入口（_cmd_model 已先调用），
            # 此处再切换会造成双重执行（QODO #1）：重复落盘/路由更新、增加失败概率。
            self._model_preference = preference
            logger.info("router.preference_changed", preference=preference)
            return True
        return False

    def get_model_preference(self) -> str:
        """返回当前聊天模型的 'provider/model' 标识。

        以 get_current_chat_model() 为单一数据源（set_chat_model 是 CLI 与 WebUI 共用的
        唯一切换入口），保证 WebUI 切换后 CLI 展示实时同步（一变都变）。
        """
        _cur = self.get_current_chat_model()
        _p = _cur.get("provider", "") or ""
        _m = _cur.get("model_id", "") or ""
        return f"{_p}/{_m}" if _p and _m else (_m or self._model_preference)

    def get_model_preference_label(self) -> str:
        """返回当前聊天模型的显示名（以实际模型为准，与 WebUI 同步）。"""
        _m = (self.get_current_chat_model().get("model_id", "") or "")
        return _m or MODEL_PREFERENCES.get(self._model_preference, {}).get("label", "未知")

    def list_models(self) -> dict:
        """动态列出当前模型与所有已发现 provider 的模型（对齐 WebUI 模型选择 button）。

        模型清单从模型发现缓存动态读取（非硬编码，与 GET /models/discover 一致）。
        """
        providers: list[dict] = []
        try:
            from web._discovery_cache import _cache as _disc_cache
            _data = (_disc_cache.get("data") or []) if _disc_cache else []
            for _pg in _data:
                _provider = _pg.get("provider", "")
                _models = _pg.get("models", []) or []
                if not _provider or not _models:
                    continue
                providers.append({
                    "provider": _provider,
                    "label": _pg.get("label", _provider),
                    "models": [
                        {"id": m.get("id", ""),
                         "display_name": m.get("display_name") or m.get("id", ""),
                         "free": bool(m.get("free"))}
                        for m in _models
                    ],
                })
        except (ImportError, KeyError, ValueError, OSError):
            logger.debug("router.list_models_discovery_failed", exc_info=True)
        # 当前模型以 get_current_chat_model() 为准：set_chat_model 是 CLI 与 WebUI
        # 共用的唯一切换入口，统一维护 _current_chat_model / registry / DEFAULT_PROVIDER /
        # models.chat_model 持久化。这样无论 CLI 还是 WebUI 切换，/model 与模型选择 button
        # 都实时反映同一模型（"一变都变"），避免 _model_preference 在 WebUI 切换后残留旧值。
        _current = self.get_current_chat_model()
        _cp = _current.get("provider", "") or ""
        _cm = _current.get("model_id", "") or ""
        return {
            "current": f"{_cp}/{_cm}" if _cp and _cm else (_cm or "未知"),
            "current_label": _cm or "未知",
            "providers": providers,
        }

    def resolve_task_type(self, base_task: str) -> str:
        return base_task

    @staticmethod
    def _apply_caching_headers(extra_headers: dict | None) -> dict | None:
        """P6: 当 PROMPT_CACHING_ENABLED 时自动补充缓存标识头。"""
        if not PROMPT_CACHING_ENABLED:
            return extra_headers
        if extra_headers is None:
            extra_headers = {}
        # Anthropic 兼容接口的 prompt caching beta 标识；不支持时由 API 端忽略或返回 400，
        # _route_with_retry 的错误处理会静默降级。
        extra_headers.setdefault("anthropic-beta", "prompt-caching-2024-07-31")
        return extra_headers

    def _is_client_configured(self, provider: str) -> bool:
        """检查指定 provider 的客户端是否已配置（有 API key 且有 base_url）。

        D12: 降级链在调用前用此方法判断目标客户端是否可用，避免向未初始化
        的客户端发起无意义调用导致兜底失效。
        """
        if provider == "mimo":
            return self._client is not None
        if provider == "agnes":
            # 同时检查 _agnes_client 和 _custom_clients["agnes"]
            # 根因：用户通过 WebUI 添加 agnes 时，客户端注册到 _custom_clients["agnes"]，
            # 但旧实现只检查 _agnes_client，导致 fallback 链跳过 agnes。
            return (self._agnes_client is not None
                    or self.has_custom_client("agnes"))
        return self.has_custom_client(provider)

    def bind_builtin(self, provider: str, client: Any) -> Any:
        if provider == "mimo":
            old_client = self._client
            self._client = client
            return old_client
        if provider == "agnes":
            old_client = self._agnes_client
            self._agnes_client = client
            return old_client
        raise ValueError(f"unsupported builtin provider: {provider}")

    def get_builtin_client(self, provider: str) -> Any:
        if provider == "mimo":
            return self._client
        if provider == "agnes":
            return self._agnes_client
        raise ValueError(f"unsupported builtin provider: {provider}")

    async def route(self, task_type: str, messages: list[dict],
                    temperature: float = 0.7, max_tokens: int | None = None,
                    stream: bool = False,
                    tools: list[dict] | None = None,
                    tool_choice: str | None = None,
                    timeout: int | None = None,
                    user_openid: str = "",
                    session_id: str = "",
                    extra_headers: dict | None = None) -> str | object:
        """路由入口：主路由 → 多级 fallback 链。"""
        # CodeRabbit#5 + M5 修复：走 registry.get_task_ref 而非直接读 ROUTE_TABLE。
        # get_task_ref 返回引用（不深拷贝），供热路径使用；replace_table 已保持对象身份，
        # 所以 registry._table 与 ROUTE_TABLE 永远是同一对象，读哪个都行，
        # 但走 registry 是语义上的唯一入口，未来若 registry 改实现也不用改 route()。
        config = self._registry.get_task_ref(task_type) or self._registry.get_task_ref("chat") or {}
        mt = max_tokens or config.get("max_tokens", DEFAULT_MAX_TOKENS)
        if timeout is None:
            timeout = self.TASK_TIMEOUTS.get(task_type, 30)

        # === 主 chat 优先：后台 LLM 任务让路 ===
        # 主 chat (task_type="chat") 执行期间 _chat_idle.clear()，
        # 后台 LLM 任务 (_BG_LLM_TASKS) await _chat_idle.wait() 自动让路。
        # 后台任务之间用 _bg_llm_semaphore(1) 串行，彻底消除 agnes 并发竞争。
        _is_bg_llm = task_type in self._BG_LLM_TASKS
        _current_task = asyncio.current_task()
        _preempt_wait_start = time.time()
        if _is_bg_llm:
            # 可观测性：后台任务进入让路等待（量化让路触发频率与等待时长）
            _chat_busy = not self._chat_idle.is_set()
            _pending_bg = len(self._active_bg_llm_tasks)
            if _chat_busy or _pending_bg > 0:
                logger.info("router.bg_llm_yield_enter", task=task_type,
                            chat_busy=_chat_busy, pending_bg=_pending_bg)
            await self._chat_idle.wait()
            await self._bg_llm_semaphore.acquire()
            try:
                await self._chat_idle.wait()  # 拿到 semaphore 后再次确认主 chat 空闲
            except BaseException:
                self._bg_llm_semaphore.release()
                raise
            # 量化让路等待时长（从进入到拿到 semaphore 并确认 chat 空闲）
            _yield_ms = int((time.time() - _preempt_wait_start) * 1000)
            if _yield_ms > 50:  # >50ms 说明真的让路了
                logger.info("router.bg_llm_yield_done", task=task_type,
                            yield_ms=_yield_ms)
                metrics.observe("router.bg_llm_yield_ms", _yield_ms)
        elif task_type == "chat":
            self._chat_idle.clear()
            # 可观测性：主 chat 抢占——取消所有未完成的后台 LLM 任务
            _cancelled = 0
            for _bg_task in tuple(self._active_bg_llm_tasks):
                if _bg_task is not _current_task and not _bg_task.done():
                    _bg_task.cancel()
                    _cancelled += 1
            await asyncio.sleep(0)
            if _cancelled > 0:
                logger.warning("router.chat_preempt_cancelled",
                               cancelled_bg=_cancelled,
                               remaining_bg=len(self._active_bg_llm_tasks))
                metrics.inc("router.chat_preempt_count")
                metrics.observe("router.chat_preempt_cancelled_n", _cancelled)

        if _is_bg_llm and _current_task is not None:
            self._active_bg_llm_tasks.add(_current_task)

        self._cache_stats["total_calls"] += 1
        extra_headers = self._apply_caching_headers(extra_headers)

        _start = time.time()
        try:
            result = await self._route_with_retry(
                task_type, config, messages, temperature, mt, stream,
                tools, tool_choice, timeout, user_openid, session_id,
                extra_headers=extra_headers,
            )
            metrics.inc(f"model_route.{task_type}.success")
            metrics.observe(f"model_route.{task_type}.duration", time.time() - _start)
            metrics.maybe_report()
            # 结构化日志：LLM 调用成功
            logger.info("llm.call", event="llm_call", model=config.get("model", ""),
                        task=task_type, duration_ms=int((time.time() - _start) * 1000),
                        user_id=user_openid, session_id=session_id)
            return result
        except (RuntimeError, OSError, KeyError, ValueError, TypeError,
                _openai_mod.APIError, LLMError) as e:
            # LLMError 纳入捕获：_select_client_for_provider 在客户端无法恢复时
            # 抛 LLMError（继承 AppException，不属于 RuntimeError/OSError/ValueError），
            # 原捕获集合漏掉它 → 主 provider 客户端未初始化时直接抛给上层，
            # 已配置的 Agnes/自定义 provider 降级链完全不会被触发。
            metrics.inc(f"model_route.{task_type}.failure")
            metrics.observe(f"model_route.{task_type}.duration", time.time() - _start)
            metrics.maybe_report()
            # 结构化日志：LLM 调用失败
            # 根因修复：APIConnectionError 的真实 httpx 异常（ConnectTimeout/DNS/TLS）存在 __cause__ 中，
            # 只记 str(e)="Connection error." 无法定位。补记 cause_type/cause_msg 实现可观测性。
            _cause = e.__cause__ if e.__cause__ else (e.__context__ if e.__context__ else None)
            logger.warning("llm.call_failed", event="llm_call", model=config.get("model", ""),
                           task=task_type, duration_ms=int((time.time() - _start) * 1000),
                           user_id=user_openid, session_id=session_id,
                           error=f"{type(e).__name__}: {e}",
                           cause_type=type(_cause).__name__ if _cause else "",
                           cause_msg=str(_cause)[:300] if _cause else "")
            fb_result = await self._try_fallback_chain(
                e, task_type, messages, temperature, stream,
                tools, tool_choice, timeout, user_openid, session_id, extra_headers,
                original_max_tokens=mt,
            )
            if fb_result is not None:
                return fb_result
            # D12: 所有降级目标均不可用，抛出明确异常而非裸 re-raise，
            # 避免上层因原始错误信息不明确而无法判断兜底已耗尽。
            raise LLMError(
                f"所有降级目标均不可用 (task={task_type}): {type(e).__name__}: {e}",
                error_code=ErrorCodeEnum.E_LLM001,
                cause=e,
            ) from e
        finally:
            # 释放让路资源：主 chat 完成后 set event 唤醒后台任务；
            # 后台任务完成后 release semaphore 让下一个后台任务执行。
            if _is_bg_llm:
                if _current_task is not None:
                    self._active_bg_llm_tasks.discard(_current_task)
                self._bg_llm_semaphore.release()
            elif task_type == "chat":
                self._chat_idle.set()

    async def route_config(self, config: dict, messages: list[dict],
                           temperature: float = 0.7, max_tokens: int = DEFAULT_MAX_TOKENS,
                           tools: list[dict] | None = None,
                           tool_choice: str | None = None,
                           timeout: int = 30) -> str | object:
        return await self._route_with_retry(
            "chat", config, messages, temperature, max_tokens, False,
            tools, tool_choice, timeout, "", "",
        )

    def _apply_prompt_caching(self, provider: str, messages: list[dict]) -> list[dict]:
        """应用 Prompt Caching（仅 Anthropic 兼容接口）。

        P0 修复（2026-08-07 人格漂移根因）：apply_cache_control 会把 system
        content 转为 Anthropic 格式 list（[{"type":"text","text":...,"cache_control":...}]）。
        OpenAI 兼容接口（agnes/openrouter/siliconflow 等）的 content 必须是字符串，
        收到 list 格式会导致服务端忽略该 system 消息 → LLM 退回出厂默认人格
        （agnese 自称 "Agnes, by Sapiens AI"）。仅 mimo（Anthropic 兼容）可用。
        """
        if provider != "mimo":
            return messages
        return apply_cache_control(messages)

    def get_vision_provider_and_model(self) -> tuple[str, str]:
        """P0 修复（用户要求"主chatLLM是谁图片发给谁，不要硬编mimo"）：
        返回用于图片识别的 (provider, model)。

        选择策略（无硬编码）：
          1. 优先用当前主 chat LLM（ROUTE_TABLE["chat"] 的 client + model），
             若该 provider 在 provider_metadata.json 中 supports_vision=True → 直接用
          2. 主 chat LLM 不支持 vision 时，从 provider_metadata.json 找第一个
             supports_vision=True 的 provider，用其 default_model
          3. 都找不到时，回退到环境变量 MIMO_MODEL_NAME（最后兜底，避免完全不可用）

        这样用户切换主模型到任意 vision-capable 模型时，图片自动走主模型，
        不再被硬编码绑死到 mimo。
        """
        # 1. 主 chat LLM
        _main_provider = ""
        _main_model = ""
        try:
            _chat_cfg = self._registry.get_task_ref("chat") or {}
            _main_provider = str(_chat_cfg.get("client", ""))
            _main_model = str(_chat_cfg.get("model", ""))
        except (KeyError, AttributeError):
            logger.debug("router.vision_chat_cfg_lookup_failed", exc_info=True)

        def _supports_vision(provider: str) -> bool:
            _meta = _PROVIDER_CAPS_FROM_FILE.get(provider, {}) if isinstance(_PROVIDER_CAPS_FROM_FILE, dict) else {}
            return bool(isinstance(_meta, dict) and _meta.get("supports_vision", False))

        if _main_provider and _main_model and _supports_vision(_main_provider):
            return _main_provider, _main_model

        # 2. 从元数据找 vision-capable provider
        if isinstance(_PROVIDER_CAPS_FROM_FILE, dict):
            for _p, _meta in _PROVIDER_CAPS_FROM_FILE.items():
                if isinstance(_meta, dict) and _meta.get("supports_vision", False):
                    _m = _meta.get("default_model", "")
                    if _m:
                        # 校验该 provider 是否已注册（有 client 可用）
                        try:
                            if _p in _get_builtin_providers() or self.has_custom_client(_p):
                                return _p, _m
                        except Exception:
                            logger.warning("router.vision_provider_check_failed", exc_info=True)

        # 3. 兜底：环境变量（不硬编码具体模型名）
        _fallback_model = os.getenv("MIMO_MODEL_NAME", "")
        if _fallback_model:
            return "mimo", _fallback_model
        return "", ""

    @staticmethod
    def _cap_max_tokens(mt: int, provider: str) -> int:
        """P0 修复：按 provider 上限裁剪 max_tokens，避免 agnes 65536 限制触发 500 错误。"""
        cap = PROVIDER_MAX_TOKENS_CAP.get(provider)
        if cap is None:
            return mt
        try:
            _mt = int(mt)
        except (TypeError, ValueError):
            return cap
        return min(_mt, cap) if _mt > 0 else cap

    async def chat_stream(self, messages: list, task_type: str = "chat",
                          temperature: float = 0.7, max_tokens: int = 2000,
                          user_openid: str = "", session_id: str = "",
                          extra_headers: dict | None = None,
                          tools: list[dict] | None = None,
                          tool_choice: str | None = None) -> AsyncIterator[str]:
        """流式调用 LLM，yield 每个 chunk 的 delta content。

        复用 _route_with_retry 的重试/错误分类/凭证轮换逻辑，
        不再独立实现一套调用路径，保证行为一致性。

        P0 修复（截断检测根因）：
        原实现在 async for chunk in stream 循环中只 yield delta.content，
        从不读取 chunk.choices[0].finish_reason，导致：
          1. _stream_finish_reason_var 永远为 None
          2. verification loop 无法检测 finish_reason="length"（max_tokens 截断）
          3. 截断重试机制完全失效（用户反复反馈"截断问题又出现了"根因）
        修复：在流结束时捕获最后一个 chunk 的 finish_reason，写入 ContextVar。

        CR-Major-1 修复（fallback 链缺失 + 流式 usage 漏算）：
        原实现在重试耗尽后直接 raise last_error，不调用 _try_fallback_chain。
        流式调用是用户主要交互方式（QQ/WebUI），主 provider 故障时整条链路断了，
        已配置的 Agnes/自定义 provider 降级完全不会被触发。
        同时原实现不传 stream_options.include_usage，provider 不返回 usage，
        流式调用费用统计漏算（用户反馈"流式调用不计费"根因）。
        修复：
          1. 重试耗尽后调用 _try_fallback_chain；fallback 返回字符串时包装成
             async generator yield 出去，保证调用方语义一致。
          2. 传 stream_options={"include_usage": True}，捕获最后一个 chunk 的 usage
             并调 _record_stream_usage 记录费用。
        """
        config = self._registry.get_task_ref(task_type) or self._registry.get_task_ref("chat") or {}
        model = config["model"]
        mt = max_tokens or config.get("max_tokens", DEFAULT_MAX_TOKENS)
        provider = config.get("client", _CFG_DEFAULT_PROVIDER)
        timeout = self.TASK_TIMEOUTS.get(task_type, 30)

        if provider == _LOCAL_ORT_PROVIDER:
            async for chunk in self._stream_local_chat(
                messages, task_type, model, mt, temperature,
            ):
                yield chunk
            return

        messages = self._apply_prompt_caching(provider, messages)
        extra_headers = self._apply_caching_headers(extra_headers)
        tools = self._filter_tools_for_model(tools, model)

        _start = time.time()
        stream = None
        last_error = None
        _stream_finish_reason: str | None = None
        # CR-Major-1: 在循环外初始化，except 分支才能安全引用（stall timeout 日志需要）
        _stall_timeout = float(os.getenv("LLM_STREAM_STALL_TIMEOUT", "15"))
        _chunk_count = 0
        _stream_usage: Any = None
        _content_yielded = False

        for attempt in range(MAX_RETRIES + 1):
            try:
                _stream_finish_reason = None
                client = await self._select_client_for_provider(provider)
                kwargs = self._build_route_kwargs(
                    model, messages, temperature, mt, True,
                    tools, tool_choice, extra_headers, config, provider,
                )
                # CR-Major-1 修复：stream_options include_usage，让 provider 在最后一个
                # chunk 返回 usage，供 _record_stream_usage 记录费用。
                kwargs["stream_options"] = {"include_usage": True}
                # per-provider 并发信号量：agnes 最多 3 并发，create + stream 消费期间
                # 占用信号量，保证同 provider 并发流不超过 MAX_PROVIDER_CONCURRENCY；
                # 不同 provider 之间不互斥。
                # 注意：不复用 _create_completion，因为其信号量仅覆盖 create；
                #       chat_stream 需在流消费期间也占用信号量（限制并发流数量）。
                async with self._get_provider_call_semaphore(provider):
                    stream = await asyncio.wait_for(
                        client.chat.completions.create(**kwargs),
                        timeout=timeout,
                    )
                    # P0 修复（qq_group 截断根因）：添加 stall timeout 检测死流
                    # 根因：原实现在 async for chunk in stream 中无 stall timeout，
                    # 如果 provider 中途关闭连接且不发送 finish_reason chunk，
                    # 循环会正常结束（无异常），content 被静默截断，
                    # _stream_finish_reason 保持 None → 不触发 length retry → 用户看到截断回复。
                    # 修复：用 asyncio.wait_for 包装每个 chunk 的读取，15 秒无新 chunk → TimeoutError
                    # _stall_timeout 已在循环外初始化（except 分支需引用）
                    _chunk_count = 0
                    _stream_usage = None
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                stream.__anext__(),
                                timeout=_stall_timeout,
                            )
                        except StopAsyncIteration:
                            break  # 流正常结束
                        _chunk_count += 1
                        # CR-Major-1：捕获 usage（最后一个 chunk 才有，include_usage=True 时）
                        _chunk_usage = getattr(chunk, "usage", None)
                        if _chunk_usage is not None:
                            _stream_usage = _chunk_usage
                        try:
                            _choice = chunk.choices[0]
                        except (AttributeError, IndexError):
                            continue
                        # P0 修复：捕获 finish_reason（最后一个 chunk 才有）
                        _chunk_fr = getattr(_choice, "finish_reason", None)
                        if _chunk_fr:
                            _stream_finish_reason = _chunk_fr
                        delta = getattr(_choice.delta, "content", None)
                        if delta:
                            _content_yielded = True
                            yield delta
                # P0 修复：流结束后检测是否收到 finish_reason
                # 如果未收到，说明 provider 可能中途关闭连接（死流），content 可能被截断
                if not _stream_finish_reason:
                    logger.warning("llm.stream_no_finish_reason",
                                   model=model, task=task_type,
                                   provider=provider, chunk_count=_chunk_count,
                                   hint="provider 可能中途关闭连接，content 可能被截断")
                # P0 修复：流结束后写入 ContextVar，供 verification loop 检测截断
                if _stream_finish_reason:
                    try:
                        from agent_core._shared import _stream_finish_reason_var
                        _stream_finish_reason_var.set(_stream_finish_reason)
                    except (ImportError, AttributeError):
                        logger.debug("router.stream_finish_reason_var_set_failed", exc_info=True)
                    # 截断诊断日志：finish_reason="length" 时记录 mt 和内容长度
                    if _stream_finish_reason == "length":
                        logger.warning("llm.stream_truncated_by_max_tokens",
                                       model=model, task=task_type,
                                       max_tokens=mt, provider=provider,
                                       finish_reason=_stream_finish_reason)
                # CR-Major-1：流式 usage 记录费用（include_usage=True 时 _stream_usage 非空）
                if _stream_usage is not None:
                    try:
                        await self._record_stream_usage(
                            task_type, model, type("R", (), {"usage": _stream_usage})(),
                            user_openid=user_openid, session_id=session_id,
                            provider=provider,
                        )
                    except (AttributeError, TypeError, OSError) as _ue:
                        logger.debug("router.stream_usage_record_skip: {}", _ue)
                metrics.inc(f"model_route.{task_type}.success")
                metrics.observe(f"model_route.{task_type}.duration", time.time() - _start)
                metrics.maybe_report()
                logger.info("llm.call", event="llm_call", model=model,
                            task=task_type, duration_ms=int((time.time() - _start) * 1000),
                            user_id=user_openid, session_id=session_id, stream=True,
                            finish_reason=_stream_finish_reason)
                return
            except (RuntimeError, OSError, KeyError, ValueError, _openai_mod.APIError,
                    asyncio.TimeoutError, LLMError) as e:
                # CR-Major-1：补 LLMError 捕获，_select_client_for_provider 抛 LLMError 时
                # 也走重试/降级，而非直接传播给上层流式消费者（与 route() 的 except 集合对齐）。
                # P0 修复：捕获 stall timeout（asyncio.TimeoutError）
                # 根因：stream stall timeout 触发时抛出 asyncio.TimeoutError，
                # 原异常处理器不捕获此类型，导致流未被正确关闭 + 异常直接传播。
                # 修复：将 asyncio.TimeoutError 纳入捕获范围，正确关闭流并走重试逻辑。
                last_error = e
                if stream:
                    with contextlib.suppress(AttributeError, OSError):
                        await stream.close()
                    stream = None
                if _content_yielded:
                    raise
                # stall timeout 特殊处理：记录诊断日志
                if isinstance(e, asyncio.TimeoutError):
                    logger.warning("llm.stream_stall_timeout",
                                   model=model, task=task_type,
                                   provider=provider, stall_timeout=_stall_timeout,
                                   chunk_count=_chunk_count,
                                   hint="流式响应中途停滞，可能 provider 故障")
                should_retry = await self._handle_route_exception(
                    e, provider, task_type, model, attempt,
                )
                if not should_retry:
                    break

        # CR-Major-1 修复：重试耗尽后调用 fallback 链，而非直接 raise。
        # 流式调用是用户主要交互方式，主 provider 故障时应降级到 Agnes/自定义 provider。
        # fallback 链返回字符串时（非流式降级结果），包装成 async generator yield 出去，
        # 保证调用方 `async for chunk in chat_stream(...)` 语义一致。
        metrics.inc(f"model_route.{task_type}.failure")
        metrics.observe(f"model_route.{task_type}.duration", time.time() - _start)
        metrics.maybe_report()
        if last_error is None:
            # 理论不可达（循环至少跑一次，失败才有 last_error）；防御性兜底
            raise LLMError("流式调用失败：未知错误（last_error 未设置）")
        logger.warning("llm.stream_fallback_attempt", event="llm_stream_fallback",
                       model=model, task=task_type, provider=provider,
                       error=f"{type(last_error).__name__}: {last_error}"[:200])
        try:
            # fallback 链 stream=True 时返回流对象；stream=False 返回字符串
            # 这里传 stream=True 让 fallback 也走流式（若目标 provider 支持）
            fb_result = await self._try_fallback_chain(
                last_error, task_type, messages, temperature, True,
                tools, tool_choice, timeout, user_openid, session_id,
                extra_headers, original_max_tokens=mt,
            )
        except (RuntimeError, OSError, LLMError) as fb_err:
            logger.error("llm.stream_fallback_failed error={}", str(fb_err)[:200])
            fb_result = None
        if fb_result is not None:
            # fallback 返回字符串（_route_with_retry stream=False 路径，或 provider 不支持流式）
            # 包装成 async generator yield 出去，保证调用方语义一致
            if isinstance(fb_result, str):
                yield fb_result
                return
            # fallback 返回流对象（stream=True 路径），透传其 chunks
            if hasattr(fb_result, "__aiter__"):
                async for _fb_chunk in fb_result:
                    _fb_choices = getattr(_fb_chunk, "choices", None)
                    _fb_delta = None
                    if _fb_choices:
                        try:
                            _fb_delta = getattr(_fb_choices[0], "delta", None)
                        except (IndexError, AttributeError):
                            _fb_delta = None
                    if _fb_delta is not None:
                        _fb_content = getattr(_fb_delta, "content", None)
                        if _fb_content:
                            yield _fb_content
                return
            # 其他类型（如 response 对象）直接 yield 字符串形式
            yield str(fb_result)
            return
        # 所有降级目标均不可用，抛出明确异常（与 route() 语义一致）
        raise LLMError(
            f"流式调用所有降级目标均不可用 (task={task_type}): "
            f"{type(last_error).__name__}: {last_error}",
            error_code=ErrorCodeEnum.E_LLM001,
            cause=last_error,
        ) from last_error

    async def _stream_local_chat(
        self,
        messages: list,
        task_type: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        transport = self.get_transport(_LOCAL_ORT_PROVIDER)
        if transport is None:
            raise LLMError(
                "local-ort provider selected but local transport is not configured",
                error_code=ErrorCodeEnum.E_LLM006,
            )
        request = CompletionRequest(
            model=model,
            messages=tuple(messages),
            temperature=temperature,
            max_tokens=max_tokens,
            extra={"route": f"router:{task_type}", "model_id": model},
        )
        try:
            async for chunk in transport.stream(request):
                if chunk.text:
                    yield chunk.text
        except TransportError as error:
            if error.__cause__ is not None:
                from local_ai.integration.reranker import LocalModelUnavailableError

                if isinstance(error.__cause__, LocalModelUnavailableError):
                    raise error.__cause__
            raise

    @staticmethod
    def _classify_error(exc: Exception) -> str:
        """将异常分类为可重试/不可重试错误类型。"""
        exc_msg = str(exc).lower()
        exc_name = type(exc).__name__.lower()
        if isinstance(exc, asyncio.TimeoutError) or 'timeout' in exc_name or 'timeout' in exc_msg:
            return 'timeout'
        if 'rate' in exc_msg or '429' in exc_msg or 'rate_limit' in exc_name:
            return 'rate_limit'
        if 'connection' in exc_name or 'connection' in exc_msg or 'connect' in exc_msg:
            return 'connection_error'
        return 'unknown'

    @staticmethod
    def _build_route_kwargs(model: str, messages: list[dict], temperature: float,
                             max_tokens: int, stream: bool,
                             tools: list[dict] | None, tool_choice: str | None,
                             extra_headers: dict | None,
                             config: dict, provider: str) -> dict:
        """构造非流式/流式路由调用的 kwargs。"""
        # P0 修复：按 provider 上限裁剪 max_tokens（agnes 上限 65536）
        max_tokens = ModelRouter._cap_max_tokens(max_tokens, provider)
        # Ollama 模型名翻译：把工作流/云模型名映射为本地实际模型名，
        # 避免请求转发到本地 Ollama 时因模型不存在报错（真实代理的核心配套）。
        _send_model = translate_model_for_provider(provider, model)
        kwargs = {
            "model": _send_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        # ── 防止模型生成退化（repetition degeneration）───
        # 根因：自回归模型的 greedy decoding 无法逃出重复循环，且自增强效应
        # 使重复概率越来越高，最终泄露训练数据中的高频片段。
        # 论文 arXiv:2512.04419 的结论：
        #   - Beam Search + early_stopping=True 是通用方案（但 OpenAI API 不支持）
        #   - presence_penalty 仅对条件模式重复有效，对结构化内容重复无效
        #   - frequency_penalty 论文未测试，作为合理启发式保留
        #   - stop 序列 + 后处理清洗是 API 调用场景下的必要兜底
        fp = config.get("frequency_penalty", 1.0)
        # 优先 WebUI 全局设置（models.frequency_penalty），回退模型配置
        try:
            from config import get_frequency_penalty
            fp = get_frequency_penalty(default=fp)
        except Exception:
            logger.warning("router.frequency_penalty_config_load_failed", exc_info=True)
        if fp:
            kwargs["frequency_penalty"] = fp
        # 论文验证有效值为 1.2，对条件模式重复有效；对结构化重复效果有限但无副作用
        pp = config.get("presence_penalty", 1.0)
        # 优先 WebUI 全局设置（models.presence_penalty），回退模型配置
        try:
            from config import get_presence_penalty
            pp = get_presence_penalty(default=pp)
        except Exception:
            logger.warning("router.presence_penalty_config_load_failed", exc_info=True)
        if pp:
            kwargs["presence_penalty"] = pp
        # 退化兜底停止序列：当模型开始输出工具定义泄露时立即停止
        kwargs["stop"] = ["Never use this AI assistant tool", "\"Never use"]
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
            # 诊断日志：记录发送给 LLM 的工具名称列表
            # P0 修复：loguru 使用 {} 占位符，不是 printf 风格 %s（原写法导致日志显示字面 %s）
            tool_names = [t.get("function", {}).get("name", "?") for t in tools]
            logger.debug("router.tools_sent provider={} model={} count={} names={}",
                         provider, model, len(tools), tool_names)
        if extra_headers:
            kwargs["extra_headers"] = extra_headers

        # 支持 thinking 参数（通用）
        # 关键修复：thinking 关闭时也要传递 enable_thinking: false，否则 agnes 模型使用默认行为
        thinking_config = config.get("thinking")
        # P0 修复：thinking_debug 从 INFO 降为 DEBUG（每次 route 调用都触发，INFO 级别刷屏）
        logger.debug("router.thinking_debug provider={} thinking={}", provider, thinking_config)
        if provider == "agnes":
            # agnes 模型需要明确传递 enable_thinking 参数
            enabled = bool(thinking_config and thinking_config.get("type") == "enabled")
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": enabled}}
        elif thinking_config:
            kwargs["extra_body"] = {"thinking": thinking_config}
        return kwargs

    async def _create_completion(
        self,
        provider: str,
        *,
        model: str,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        stream: bool,
        tools: list[dict] | None,
        tool_choice: str | None,
        extra_headers: dict | None,
        config: dict,
        timeout: int,
        stream_options: dict | None = None,
    ) -> Any:
        """统一「客户端选择 → 构造 kwargs → 加锁创建」的调用核心。

        供 chat_stream / _route_with_retry / _route_for_continuation 复用，
        消除三处重复。stream_options 仅流式路径需要时透传。
        """
        client = await self._select_client_for_provider(provider)
        kwargs = self._build_route_kwargs(
            model, messages, temperature, max_tokens, stream,
            tools, tool_choice, extra_headers, config, provider,
        )
        if stream_options:
            kwargs["stream_options"] = stream_options
        async with self._get_provider_call_semaphore(provider):
            return await asyncio.wait_for(
                client.chat.completions.create(**kwargs),
                timeout=timeout,
            )

    async def _handle_route_response(self, response: Any, task_type: str, model: str,
                                     stream: bool, user_openid: str, session_id: str,
                                     provider: str, tools: list[dict] | None,
                                     messages: list[dict] | None = None,
                                     temperature: float | None = None,
                                     max_tokens: int | None = None,
                                     config: dict | None = None) -> str | object:
        """处理路由成功响应：记录费用、缓存、凭证成功，返回 content 或 response。"""
        if stream:
            # 流式调用：在返回前尝试记录费用（部分 provider 在流结束时提供 usage）
            try:
                await self._record_stream_usage(task_type, model, response,
                                                user_openid, session_id, provider)
            except (AttributeError, TypeError, OSError) as e:
                logger.debug("router.stream_usage_record_failed: %s", e)
            return response

        self._track_cache(response)
        await self._record_usage(task_type, model, response, user_openid, session_id, provider)
        self._check_cache_health()

        # 报告凭证成功
        await self._credential_pool.report_success(provider)

        if tools and response.choices[0].message.tool_calls:
            _reasoning_content_var.set(getattr(response.choices[0].message, "reasoning_content", None) or "")
            return response

        content = response.choices[0].message.content or ""
        rc = getattr(response.choices[0].message, "reasoning_content", None) or ""
        _reasoning_content_var.set(rc)
        # 关键修复：禁止用 reasoning_content 代替 content
        # 根因：agnes-2.0-flash 即使 enable_thinking=False，在 max_tokens 不足或某些边界条件下
        # 仍可能返回 reasoning_content。用思考链代替回复会导致"推理严重泄漏"——
        # LLM 的内部思考过程被当成最终回复发给用户。
        # 正确做法：content 为空时返回降级提示，触发上层 fallback 机制。
        if not content and rc:
            logger.warning("router.reasoning_leak_blocked",
                           model=model, task=task_type,
                           rc_len=len(rc), finish_reason=getattr(response.choices[0], "finish_reason", None))
            content = ""  # 留空，让上层降级/fallback 机制接管

        # usage 诊断日志：记录实际生成 token 数，帮助定位截断根因
        _usage = getattr(response, "usage", None)
        if _usage:
            logger.debug("router.usage", model=model, task=task_type,
                         prompt_tokens=getattr(_usage, "prompt_tokens", 0),
                         completion_tokens=getattr(_usage, "completion_tokens", 0),
                         finish_reason=getattr(response.choices[0], "finish_reason", None),
                         content_len=len(content))

        # 检查 finish_reason：截断重试（assistant-prefill 方式，不污染上下文）
        # P0 重构（用户要求"不许截断" + "重试机制保留"）：
        # 根因 1：原截断重试追加 "请继续完成你的回复" 作为 user message，
        #         LLM 把它当成真实用户输入，在后续轮次回应"继续完成"等元词汇，
        #         造成上下文污染和角色出戏（详见 conversation_logs 2026-07-25 17:46 案例）。
        # 根因 2：max_tokens=32768 对中文长回复过小，频繁触发 length 截断。
        # 修复：
        #   1. WEB_UI_MAX_TOKENS 提升到 131072（匹配模型上下文窗口），从源头消除大部分截断
        #   2. 保留重试机制，但改用 assistant-prefill（不追加 user message），
        #      避免"请继续"prompt 污染上下文
        #   3. 重试使用 _route_for_continuation（去递归化），最多 2 轮
        #   4. 上下文溢出仍由 agent_context.py 的压缩机制处理（"重置机制"）
        finish_reason = getattr(response.choices[0], "finish_reason", None)
        if finish_reason and finish_reason != "stop":
            content_len = len(content)
            if finish_reason == "length":
                logger.warning("llm.truncated_by_max_tokens",
                               model=model, task=task_type,
                               content_len=content_len,
                               finish_reason=finish_reason)
                # 重试机制保留：使用 assistant-prefill 续写（不追加 user message）
                # 关键：不追加 "请继续完成你的回复" 等 user message，
                #       避免污染上下文（LLM 会在后续轮次回应这些元词汇）
                # Feature flag: TRUNCATION_RETRY_DERECURSE（默认 true）
                _derecurse = os.getenv("TRUNCATION_RETRY_DERECURSE", "true").lower() in ("true", "1", "yes")
                # CodeRabbit #6 修复：加 messages 非空检查（防御性编程）
                # _handle_route_response 的 messages 参数是 list[dict] | None = None
                # 虽然 _route_with_retry 签名是非 Optional，但防御性检查避免潜在 None 触发 AttributeError
                if messages and content and len(content) > 10:
                    _retry_max_tokens = max_tokens * 2 if max_tokens else None
                    for _retry_round in range(2):  # 最多 2 轮重试
                        try:
                            retry_messages = messages.copy()
                            # assistant-prefill：追加已有内容，让 LLM 从此处续写
                            retry_messages.append({"role": "assistant", "content": content})
                            # 注意：不追加任何 user message，避免"请继续"prompt 污染上下文
                            if _derecurse:
                                # 新路径：直接调底层，不递归 route()，返回原始 response
                                retry_response = await self._route_for_continuation(
                                    task_type, retry_messages, temperature=temperature,
                                    max_tokens=_retry_max_tokens,
                                    user_openid=user_openid, session_id=session_id,
                                )
                                retry_content = ""
                                _retry_finish = None
                                if retry_response is not None:
                                    _choices = getattr(retry_response, "choices", None) or []
                                    if _choices:
                                        retry_content = getattr(_choices[0].message, "content", "") or ""
                                        _retry_finish = getattr(_choices[0], "finish_reason", None)
                            else:
                                # 旧路径（兼容回退）：递归调用 route()
                                retry_result = await self.route(
                                    task_type, retry_messages, temperature=temperature,
                                    max_tokens=_retry_max_tokens,
                                    user_openid=user_openid, session_id=session_id,
                                )
                                retry_content = retry_result if isinstance(retry_result, str) else (retry_result.choices[0].message.content or "")
                                _retry_finish = getattr(retry_result, "choices", [{}])
                                _retry_finish = getattr(_retry_finish[0], "finish_reason", None) if _retry_finish else None
                            if retry_content and len(retry_content) > 5:
                                content, _merge_action = merge_continuation(
                                    content, retry_content, assume_tail=True,
                                )
                                logger.info("llm.truncated_retry_success",
                                            final_len=len(content), model=model,
                                            retry_round=_retry_round + 1,
                                            finish_reason=_retry_finish,
                                            derecurse=_derecurse,
                                            merge_action=_merge_action,
                                            method="assistant_prefill")
                                # 检查是否仍然截断（基于真实 finish_reason 判断）
                                if _retry_finish != "length":
                                    break  # 不再截断，退出重试
                            else:
                                break  # 无内容，退出
                        except Exception as e:
                            logger.warning("llm.truncated_retry_failed", error=str(e), model=model,
                                           retry_round=_retry_round + 1)
                            break
            elif finish_reason == "content_filter":
                logger.warning("llm.content_filtered",
                               model=model, task=task_type,
                               content_len=content_len,
                               finish_reason=finish_reason)
                # content_filter 通常是 provider 服务端审查（如 mimo-v2.5 对敏感内容过滤）
                # 抛出异常触发 fallback 链，给 agnes 等其他 provider 一次重试机会
                raise RuntimeError(
                    f"content_filter by {provider}/{model}: 服务端内容审查拦截"
                )
            else:
                logger.info("llm.unusual_finish",
                            model=model, task=task_type,
                            finish_reason=finish_reason,
                            content_len=content_len)

        # 关键修复：空 content 一律抛异常触发 fallback
        # 根因：agnes-2.0-flash 多种异常行为：
        #   1. finish_reason=tool_calls + 空 content（工具调用已在前面的分支处理，到这里 content 为空=异常）
        #   2. finish_reason=stop + 空 content（模型认为不需要回复，但用户会收到空回复）
        # 两种情况都应触发 fallback 重试其他 provider，而不是返回空字符串给用户。
        if not content.strip():
            raise RuntimeError(
                f"empty_content by {provider}/{model}: finish_reason={finish_reason}, "
                f"content 为空（模型未生成有效回复）"
            )

        # 关键：WebUI 设置必须生效，不许泄露思考
        thinking_config = (config or {}).get("thinking", {})
        thinking_disabled = thinking_config.get("type") == "disabled"

        # 1. 清空 reasoning_content（不管 thinking 是否禁用，都不泄露给用户）
        _reasoning_content_var.set("")

        # 2. thinking 禁用时，清理 content 中可能嵌入的推理标记
        if thinking_disabled:
            from utils.text_utils import strip_reasoning
            content = strip_reasoning(content)

        return content

    async def _handle_route_exception(self, e: Exception, provider: str,
                                      task_type: str, model: str,
                                      attempt: int) -> bool:
        """处理路由异常：分类、报告、轮换凭证。返回 True 表示可重试，False 表示已耗尽。

        对于 ABORT 或不可重试错误，直接 raise 传播给调用方。
        """
        classified = self._error_classifier.classify(e)
        await self._credential_pool.report_error(provider, classified)

        # 根据恢复策略执行不同操作
        if classified.action == RecoveryAction.ROTATE_CREDENTIAL:
            await self._rotate_credential_on_error(provider, classified)

        if classified.action == RecoveryAction.ABORT:
            logger.error("router.call_aborted", task=task_type, model=model,
                         reason=classified.reason.value,
                         error=f"{type(e).__name__}: {e}")
            raise e

        if not classified.is_retryable:
            logger.error("router.call_failed", task=task_type, model=model,
                         attempt=attempt + 1, reason=classified.reason.value,
                         action=classified.action.value,
                         error=f"{type(e).__name__}: {e}")
            raise e

        if attempt < MAX_RETRIES:
            backoff = classified.backoff_seconds if classified.backoff_seconds > 0 else 1 * (attempt + 1)
            # P0 修复（2026-08-05）：loguru extra 字段在当前日志格式下不打印，
            # 导致 router.retry 只显示 event name，看不到 reason/error。
            # 改为 f-string 写入 message，确保 agnes 失败原因可见。
            logger.warning(
                f"router.retry task={task_type} model={model} "
                f"attempt={attempt + 1} reason={classified.reason.value} "
                f"action={classified.action.value} backoff={backoff:.1f}s "
                f"error={type(e).__name__}: {e}")
            await asyncio.sleep(backoff)
            return True
        logger.error("router.retry_exhausted", task=task_type, model=model,
                     attempts=MAX_RETRIES + 1, reason=classified.reason.value,
                     error=f"{type(e).__name__}: {e}")
        return False

    async def _route_with_retry(self, task_type: str, config: dict,
                                messages: list[dict], temperature: float,
                                max_tokens: int, stream: bool,
                                tools: list[dict] | None, tool_choice: str | None,
                                timeout: int, user_openid: str, session_id: str,
                                extra_headers: dict | None = None) -> str | object:
        """带重试的路由调用：客户端选择 → 构建 kwargs → 调用 API → 处理响应/异常。"""
        model = config["model"]
        last_error = None
        provider = config.get("client", _CFG_DEFAULT_PROVIDER)

        if provider == _LOCAL_ORT_PROVIDER:
            chunks = []
            async for chunk in self._stream_local_chat(
                messages, task_type, model, max_tokens, temperature,
            ):
                chunks.append(chunk)
            return "".join(chunks)

        messages = self._apply_prompt_caching(provider, messages)
        # 主路由路径也需过滤工具，防止小模型收到工具定义后输出退化
        tools = self._filter_tools_for_model(tools, model)

        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await self._create_completion(
                    provider,
                    model=model, messages=messages, temperature=temperature,
                    max_tokens=max_tokens, stream=stream, tools=tools, tool_choice=tool_choice,
                    extra_headers=extra_headers, config=config, timeout=timeout,
                )
                return await self._handle_route_response(
                    response, task_type, model, stream,
                    user_openid, session_id, provider, tools,
                    messages=messages, temperature=temperature, max_tokens=max_tokens,
                    config=config,
                )

            except (RuntimeError, OSError, KeyError, ValueError,
                    _openai_mod.APIError, LLMError) as e:
                # LLMError：客户端未初始化/无法恢复，重试同一 provider 无意义，
                # 但必须让它作为 last_error 抛出到 route 的降级链（见 route 的注释）
                last_error = e
                should_retry = await self._handle_route_exception(
                    e, provider, task_type, model, attempt,
                )
                if not should_retry:
                    break
        raise last_error

    async def _route_for_continuation(self, task_type: str, messages: list[dict],
                                       temperature: float = 0.7,
                                       max_tokens: int | None = None,
                                       user_openid: str = "",
                                       session_id: str = "") -> Any | None:
        """截断续写专用路由：直接调用 LLM 返回原始 response 对象，不递归触发截断重试。

        P0 修复（Task 1.1+1.2）：替代原 `await self.route(...)` 递归调用。
        - 不进入 `_handle_route_response`，避免再次触发截断重试形成递归风暴
        - 返回原始 response 对象，让调用方正确读取 `finish_reason` 判断是否仍截断
        - 单次调用，无重试（截断续写本身的 2 轮循环由调用方控制）
        - 失败时返回 None（调用方按"无内容"分支处理）

        注意：此方法仅用于截断续写场景，常规路由请使用 route()。
        """
        # 统一走 registry 入口（语义一致；registry._table 即 ROUTE_TABLE，性能无差异）
        config = self._registry.get_task_ref(task_type) or self._registry.get_task_ref("chat") or {}
        model = config["model"]
        provider = config.get("client", _CFG_DEFAULT_PROVIDER)
        mt = max_tokens or config.get("max_tokens", DEFAULT_MAX_TOKENS)
        timeout = self.TASK_TIMEOUTS.get(task_type, 30)

        # 应用 prompt caching（与主路由保持一致）
        messages = self._apply_prompt_caching(provider, messages)

        try:
            response = await self._create_completion(
                provider,
                model=model, messages=messages, temperature=temperature,
                max_tokens=mt, stream=False, tools=None, tool_choice=None,
                extra_headers=None, config=config, timeout=timeout,
            )
            self._track_cache(response)
            logger.info("llm.continuation_call", model=model, task=task_type,
                        user_id=user_openid, session_id=session_id,
                        max_tokens=mt)
            return response
        except (RuntimeError, OSError, KeyError, ValueError, _openai_mod.APIError) as e:
            # 续写失败不影响主流程，调用方按"无内容"分支处理
            logger.warning("llm.continuation_failed",
                           model=model, task=task_type,
                           error=f"{type(e).__name__}: {e}"[:200])
            return None

# ── 全局单例工厂 ──────────────────────────────────────────────
# core/dream_engine_v2 与 emotion/emotion_llm 的 local 推理路径需要共享一个
# ModelRouter，但两者都不是 AgentCore 持有方。此前它们引用不存在的
# get_model_router()（潜伏 ImportError 死路径，调用方 try/except 后静默降级
# 为 None）。补上懒加载单例，修复死路径。
_model_router_singleton: ModelRouter | None = None
_model_router_lock = threading.Lock()


def get_model_router() -> ModelRouter:
    """获取全局 ModelRouter 单例（懒加载）。"""
    global _model_router_singleton
    if _model_router_singleton is None:
        with _model_router_lock:
            if _model_router_singleton is None:
                _model_router_singleton = ModelRouter()
    return _model_router_singleton
