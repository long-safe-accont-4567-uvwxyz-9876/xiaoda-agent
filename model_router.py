import asyncio
import os
import threading
import time
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

# ── Phase 6 拆分：流式执行链块抽为 llm_gateway/router_execution.ExecutionMixin ──
# 方法体逐字节搬移；MAX_RETRIES 随链搬入；此处同名 re-export 保持
# `from model_router import ExecutionMixin` / `from model_router import MAX_RETRIES` 兼容。
from llm_gateway.router_execution import MAX_RETRIES, ExecutionMixin  # noqa: F401,E402

# ── Phase 5 拆分：成本/缓存统计块抽为 llm_gateway/router_metrics.CostTrackingMixin ──
# _reasoning_content_var 随 pop_reasoning_content 搬入 mixin，此处同名引入原供
# _handle_route_response 使用（Phase 6 已随执行链搬出），现仅作同名 re-export
# 保持外部 import 兼容；CostTrackingMixin 同理。
from llm_gateway.router_metrics import CostTrackingMixin, _reasoning_content_var  # noqa: F401,E402

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

# FALLBACK_ROUTE 数据已下沉 model_router_registry（llm_gateway 契约要求网关不得反向
# import 门面）；此处同名 re-export 维持旧 import 路径（tests/web 路由仍从这取）。
from model_router_registry import FALLBACK_ROUTE  # noqa: F401,E402

# ── Phase 2 拆分：ModelRouteRegistry 抽为 model_router_registry（逐字节搬移） ──
from model_router_registry import ModelRouteRegistry as ModelRouteRegistry  # noqa: F401,E402
from transports import AgnesTransport, MiMoTransport, ProviderTransport

# 根因修复：agnes API connect=5s 过短导致 APIConnectionError，统一从 agnes_transport 引入共享 httpx 配置
from transports.agnes_transport import AGNES_HTTP_TIMEOUT, _get_agnes_http_client
from utils.common import DEFAULT_MAX_TOKENS
from utils.credential_pool import get_credential_pool
from utils.error_classifier import ErrorClassifier
from utils.metrics import metrics
from utils.prompt_caching import apply_cache_control

# 长对话路由的 max_tokens 上限（128K）：chat 与 chat_agnes 共用，改值两处同步。
CHAT_MAX_TOKENS = 131072

ROUTE_TABLE = {
    # chat 主路由：128K 上限，支撑长时间连贯对话，搭配滑动窗口+摘要压缩避免退化
    # 不再锁死 8192，避免长会话频繁截断历史导致记忆断裂
    "chat": {"model": _CFG_MODEL_NAME, "max_tokens": CHAT_MAX_TOKENS, "client": _CFG_DEFAULT_PROVIDER, "thinking": {"type": "disabled"}},
    "emotion_analysis": {"model": _CFG_FLASH_MODEL or _CFG_MODEL_NAME, "max_tokens": 1024, "client": _CFG_DEFAULT_PROVIDER, "thinking": {"type": "disabled"}},
    "tool_result_wrap": {"model": _CFG_FLASH_MODEL or _CFG_MODEL_NAME, "max_tokens": 2048, "client": _CFG_DEFAULT_PROVIDER, "thinking": {"type": "disabled"}},
    "memory_encoding": {"model": _CFG_FLASH_MODEL or _CFG_MODEL_NAME, "max_tokens": DEFAULT_MAX_TOKENS, "client": _CFG_DEFAULT_PROVIDER, "thinking": {"type": "disabled"}},
    "chat_agnes": {"model": AGNES_TEXT_MODEL, "max_tokens": CHAT_MAX_TOKENS, "client": "agnes", "thinking": {"type": "disabled"},
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
# MAX_RETRIES 已随执行链搬入 llm_gateway/router_execution.py（顶部同名 re-export）
# per-provider LLM 调用并发上限：agnes 支持最多 3 并发，统一各 provider 上限为 3，
# 取代原先 asyncio.Lock 的串行（1 并发）。凭证轮换/客户端刷新仍走 _get_credential_lock 串行。
MAX_PROVIDER_CONCURRENCY = 3


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


class ModelRouter(ExecutionMixin, CostTrackingMixin, ClientLifecycleMixin, FallbackChainMixin):
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
                         str(e), _updated_tasks, exc_info=True)
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
            # 取消安全修复：clear() 与下方主 try(:685) 之间存在 await 窗口（sleep(0)），
            # wait_for 超时取消若恰好落在窗口内，函数不会进入主 try 的 finally，
            # _chat_idle 将永久保持 cleared → 所有后台 LLM 任务死锁在 _chat_idle.wait()。
            # 故此窗口内的任何异常退出路径必须先 set 回再抛出。
            try:
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
            except BaseException:
                self._chat_idle.set()
                raise

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

    # ── Phase 6 拆分：流式执行链块抽为 llm_gateway/router_execution.ExecutionMixin ──
    # chat_stream / _stream_local_chat / _classify_error / _build_route_kwargs /
    # _create_completion / _handle_route_response / _handle_route_exception /
    # _route_with_retry / _route_for_continuation / _cap_max_tokens（方法体逐字节
    # 搬移；_build_route_kwargs 内原 ModelRouter._cap_max_tokens 引用改为
    # ExecutionMixin._cap_max_tokens，唯一一行函数体偏差）。
    # route / route_config 留在本体：主入口对执行链仅单向 self 调用
    # （_route_with_retry / _try_fallback_chain），经 MRO 命中 Mixin 实现。

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
