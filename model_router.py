from typing import Any, AsyncIterator, ClassVar
import os
import time
import asyncio
import contextvars
from openai import AsyncOpenAI
import openai as _openai_mod  # 用于 openai.APIError 异常捕获
from loguru import logger

from db.db_analytics import AnalyticsDB
from utils.metrics import metrics
from config import AGNES_BASE_URL, AGNES_TEXT_MODEL, PROMPT_CACHING_ENABLED
from config import MODEL_NAME as _CFG_MODEL_NAME, PRO_MODEL_NAME as _CFG_PRO_MODEL
from config import FLASH_MODEL_NAME as _CFG_FLASH_MODEL, DEFAULT_PROVIDER as _CFG_DEFAULT_PROVIDER
from config import set_default_provider as _set_default_provider
from transports import ProviderTransport, MiMoTransport, AgnesTransport
from utils.prompt_caching import apply_cache_control
from utils.error_classifier import ErrorClassifier, RecoveryAction
from utils.credential_pool import get_credential_pool
from security.ssrf_guard import validate_url as _ssrf_validate_url
from core.app_exception import LLMError
from core.error_codes import ErrorCodeEnum
import contextlib


MIMO_MODEL = os.getenv("MIMO_MODEL_NAME", "mimo-v2.5")
MIMO_PRO_MODEL = os.getenv("MIMO_PRO_MODEL_NAME", "mimo-v2.5-pro")
MIMO_BASE_URL = os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
MIMO_API_KEY = os.getenv("MIMO_API_KEY", "")

MIMO_PRICING = {
    "standard": {
        "input_per_m": 0.10,
        "cache_hit_per_m": 0.01,
        "output_per_m": 0.20,
    },
    "pro": {
        "input_per_m": 0.20,
        "cache_hit_per_m": 0.02,
        "output_per_m": 0.40,
    },
}

# Provider 级别定价表（USD/百万 tokens）
# 自定义 provider 默认使用 default 档；未知 provider 也使用 default
PROVIDER_PRICING = {
    "mimo": MIMO_PRICING,  # mimo 内部按 model 名再细分 standard/pro
    "agnes": {
        "input_per_m": 0.15,
        "cache_hit_per_m": 0.015,
        "output_per_m": 0.30,
    },
    "default": {
        "input_per_m": 0.20,
        "cache_hit_per_m": 0.02,
        "output_per_m": 0.40,
    },
}

ROUTE_TABLE = {
    "chat": {"model": _CFG_MODEL_NAME, "max_tokens": 1500, "client": _CFG_DEFAULT_PROVIDER},
    "chat_pro": {"model": _CFG_PRO_MODEL or _CFG_MODEL_NAME, "max_tokens": 2000, "client": _CFG_DEFAULT_PROVIDER, "thinking": {"type": "enabled", "budget_tokens": 2048}},
    "chat_flash": {"model": _CFG_FLASH_MODEL or _CFG_MODEL_NAME, "max_tokens": 1000, "client": _CFG_DEFAULT_PROVIDER},
    "chat_mini": {"model": _CFG_FLASH_MODEL or _CFG_MODEL_NAME, "max_tokens": 800, "client": _CFG_DEFAULT_PROVIDER},
    "chat_mimo": {"model": MIMO_MODEL, "max_tokens": 1500, "client": "mimo"},
    "emotion_analysis": {"model": _CFG_FLASH_MODEL or _CFG_MODEL_NAME, "max_tokens": 300, "client": _CFG_DEFAULT_PROVIDER},
    "tool_result_wrap": {"model": _CFG_FLASH_MODEL or _CFG_MODEL_NAME, "max_tokens": 300, "client": _CFG_DEFAULT_PROVIDER},
    "memory_encoding": {"model": _CFG_FLASH_MODEL or _CFG_MODEL_NAME, "max_tokens": 800, "client": _CFG_DEFAULT_PROVIDER},
    "chat_agnes": {"model": AGNES_TEXT_MODEL, "max_tokens": 2000, "client": "agnes"},
}

MODEL_PREFERENCES = {
    "mimo": {"label": "MiMo 模式", "desc": "使用小米 MiMo-V2.5 模型"},
    "mimo-pro": {"label": "MiMo Pro 模式", "desc": "使用小米 MiMo-V2.5-Pro 深度思考"},
    "mimo-flash": {"label": "MiMo Flash 模式", "desc": "使用小米 MiMo-V2.5 快速响应"},
    "mimo-mini": {"label": "MiMo Mini 模式", "desc": "使用小米 MiMo-V2.5 轻量任务"},
}

RETRYABLE_ERRORS = {'timeout', 'rate_limit', 'connection_error'}
MAX_RETRIES = 2
FALLBACK_ROUTE = {
    "chat_pro": "chat_flash",
    "chat_flash": "chat_mini",
    "chat_mini": "chat_agnes",
}

# 请求级隔离的 reasoning_content，避免并发请求间共享状态
_reasoning_content_var = contextvars.ContextVar('reasoning_content', default='')


def _ssrf_check(url: str) -> None:
    """SSRF 防护：5步法校验 base_url 安全性（best-effort，本地 provider 如 Ollama 校验失败仅告警不阻塞）"""
    try:
        ok, reason = _ssrf_validate_url(url)
        if not ok:
            logger.warning("router.ssrf_blocked url={} reason={}", url, reason)
    except (ValueError, OSError) as e:
        logger.debug("router.ssrf_check_skip url={} error={}", url, str(e))


class ModelRouter:
    """模型路由器，按任务类型选择模型/Provider 并处理重试与凭证轮换。"""

    _DEFAULT_TIMEOUTS: ClassVar[dict[str, int]] = {
        "emotion_analysis": 10,
        "emotion": 10,
        "chat_flash": 60,
        "chat": 90,
        "chat_pro": 90,
        "tool_call": 60,
        "image_gen": 90,
    }

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 api_key_2: str | None = None, db: Any=None) -> None:
        self.TASK_TIMEOUTS: dict[str, int] = dict(self._DEFAULT_TIMEOUTS)
        # 从 os.getenv() 实时读取，避免使用模块级冻结变量
        _mimo_key = api_key or os.getenv("MIMO_API_KEY", "")
        _mimo_url = base_url or os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
        _ssrf_check(_mimo_url)  # SSRF 防护：校验 base_url
        self._client = AsyncOpenAI(api_key=_mimo_key, base_url=_mimo_url) if _mimo_key else None
        _agnes_key = os.getenv("AGNES_API_KEY", "")
        _agnes_url = os.getenv("AGNES_BASE_URL", AGNES_BASE_URL)
        _ssrf_check(_agnes_url)  # SSRF 防护：校验 base_url
        self._agnes_client = AsyncOpenAI(api_key=_agnes_key, base_url=_agnes_url) if _agnes_key else None
        self._db = db
        self._model_preference = "mimo"
        self._cost_buffer: list[dict] = []
        self._cost_flush_threshold = 3
        self._last_cache_warning = 0.0
        self._error_classifier = ErrorClassifier()
        self._credential_pool = get_credential_pool()
        self._credential_locks: dict[str, asyncio.Lock] = {}

        self._custom_clients: dict[str, AsyncOpenAI] = {}
        self._current_chat_model: dict | None = None
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

    def _get_credential_lock(self, provider: str) -> asyncio.Lock:
        """返回指定 provider 的凭证锁，按需创建。

        不同 provider 之间不再互相阻塞，相同 provider 仍然串行化以保护凭证轮换。
        """
        return self._credential_locks.setdefault(provider, asyncio.Lock())

    def _lazy_register_provider(self, provider: str) -> None:
        """懒注册：从 config_service 恢复未注册的自定义 provider。"""
        try:
            from web.config_service import get_config_service
            from web._provider_keys import load_provider_key
            from web.custom_providers import register_into_router
            cfg = get_config_service()
            record = cfg.get(f"models.providers.{provider}")
            if record:
                api_key = load_provider_key(provider)
                if api_key:
                    register_into_router(
                        self, provider,
                        record.get("format", "openai"),
                        record.get("base_url", ""),
                        api_key,
                    )
                    logger.info("router.lazy_registered provider={}", provider)
        except (ImportError, AttributeError, KeyError, ValueError) as e:
            logger.warning("router.lazy_register_failed provider={} error={}", provider, str(e))

    def refresh_client(self) -> None:
        """重建 MiMo / Agnes 客户端（Setup 保存新 Key 后调用）。

        ModelRouter.__init__ 只在启动时读取一次环境变量创建客户端，
        后续通过 Setup 页面保存的新 Key 不会自动生效。此方法从当前
        os.environ 重新读取 Key 并重建客户端，使新配置立即生效。
        """
        old_mimo = self._client
        old_agnes = self._agnes_client

        new_mimo_key = os.getenv("MIMO_API_KEY", "")
        new_mimo_url = os.getenv("MIMO_BASE_URL", MIMO_BASE_URL)
        if new_mimo_key:
            _ssrf_check(new_mimo_url)  # SSRF 防护：校验 base_url
            self._client = AsyncOpenAI(api_key=new_mimo_key, base_url=new_mimo_url)
            logger.info("router.mimo_client_refreshed",
                        key_suffix=new_mimo_key[-6:] if len(new_mimo_key) >= 6 else "***")
        else:
            self._client = None

        new_agnes_key = os.getenv("AGNES_API_KEY", "")
        new_agnes_url = os.getenv("AGNES_BASE_URL", AGNES_BASE_URL)
        if new_agnes_key:
            _ssrf_check(new_agnes_url)  # SSRF 防护：校验 base_url
            self._agnes_client = AsyncOpenAI(api_key=new_agnes_key, base_url=new_agnes_url)
            logger.info("router.agnes_client_refreshed",
                        key_suffix=new_agnes_key[-6:] if len(new_agnes_key) >= 6 else "***")
        else:
            self._agnes_client = None

        # 关闭旧客户端释放连接
        for old in (old_mimo, old_agnes):
            if old is not None and old not in (self._client, self._agnes_client):
                try:
                    import asyncio
                    loop = asyncio.get_running_loop()
                    _bg_close = loop.create_task(old.close())
                except RuntimeError:
                    pass

        # 同步更新凭证池：确保 MiMo/Agnes 凭证与当前环境变量一致
        try:
            from utils.credential_pool import get_credential_pool
            pool = get_credential_pool()
            # 补充/更新 MiMo 凭证
            if new_mimo_key:
                self._ensure_credential_in_pool(pool, "mimo", new_mimo_key, new_mimo_url)
            # 补充/更新 Agnes 凭证
            if new_agnes_key:
                self._ensure_credential_in_pool(pool, "agnes", new_agnes_key, new_agnes_url)
        except (KeyError, ValueError, AttributeError) as e:
            logger.warning("router.credential_pool_sync_failed error={}", str(e))

    @staticmethod
    def _ensure_credential_in_pool(pool: Any, provider: str, api_key: str, base_url: str) -> None:
        """确保凭证池中有该 provider 的最新凭证。"""
        from utils.credential_pool import Credential
        existing = pool._pool.get(provider, [])
        key_suffix = api_key[-6:] if len(api_key) >= 6 else api_key
        already_exists = any(c.api_key.endswith(key_suffix) for c in existing)
        if not already_exists:
            pool.add_credential(Credential(
                api_key=api_key,
                provider=provider,
                base_url=base_url,
            ))

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
        ROUTE_TABLE["chat"]["model"] = model_id
        ROUTE_TABLE["chat"]["client"] = provider
        # 同步更新全局 DEFAULT_PROVIDER，使子代理、成本统计等全部跟随
        _set_default_provider(provider)
        if provider not in ("mimo", "agnes"):
            if provider not in self._custom_clients:
                self._lazy_register_provider(provider)
            if provider not in self._custom_clients:
                raise LLMError(f"自定义 provider {provider} 未注册，请先注册客户端")
        self._current_chat_model = {"provider": provider, "model_id": model_id}
        # 持久化到 config_service，以便重启后恢复上次聊天模型
        try:
            from web.config_service import get_config_service
            get_config_service().set(
                "models.chat_model",
                {"provider": provider, "model_id": model_id},
            )
        except (OSError, KeyError, ValueError, TypeError) as e:
            logger.warning("router.chat_model_persist_failed error={}", str(e))
        logger.info("router.chat_model_changed", provider=provider, model=model_id)
        return {"provider": provider, "model_id": model_id}

    def get_current_chat_model(self) -> dict:
        if self._current_chat_model is not None:
            return self._current_chat_model
        return {"provider": _CFG_DEFAULT_PROVIDER, "model_id": ROUTE_TABLE.get("chat", {}).get("model", _CFG_MODEL_NAME)}

    # 已知自定义 provider 的默认模型映射
    _CUSTOM_PROVIDER_DEFAULT_MODELS: ClassVar[dict[str, str]] = {
        "siliconflow": "Qwen/Qwen2.5-7B-Instruct",
        "openrouter": "meta-llama/llama-3.3-8b-instruct:free",
        "modelscope": "Qwen/Qwen2.5-7B-Instruct",
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
            provider, model_id = preference.split("/", 1)
            self.set_chat_model(provider, model_id)
            self._model_preference = preference
            logger.info("router.preference_changed", preference=preference)
            return True
        return False

    def get_model_preference(self) -> str:
        return self._model_preference

    def get_model_preference_label(self) -> str:
        if "/" in self._model_preference:
            return self._model_preference.split("/", 1)[1]
        return MODEL_PREFERENCES.get(self._model_preference, {}).get("label", "未知")

    def resolve_task_type(self, base_task: str) -> str:
        if "/" in self._model_preference:
            return base_task
        if self._model_preference == "mimo-pro":
            return "chat_pro"
        if self._model_preference == "mimo-flash":
            return "chat_flash"
        if self._model_preference == "mimo-mini":
            return "chat_mini"
        return base_task

    def _calc_cost(self, prompt_tokens: int, completion_tokens: int,
                   cache_hit_tokens: int = 0, cache_miss_tokens: int = 0,
                   model: str = "", provider: str = "") -> float:
        cache_miss = cache_miss_tokens if cache_miss_tokens > 0 else (prompt_tokens - cache_hit_tokens)
        if cache_miss < 0:
            cache_miss = prompt_tokens
        # 按 provider 查定价表
        if provider == "mimo":
            pricing = MIMO_PRICING.get("pro") if "pro" in model else MIMO_PRICING.get("standard")
        else:
            pricing = PROVIDER_PRICING.get(provider, PROVIDER_PRICING["default"])
        input_cost = (cache_miss / 1_000_000) * pricing["input_per_m"]
        cache_cost = (cache_hit_tokens / 1_000_000) * pricing["cache_hit_per_m"]
        output_cost = (completion_tokens / 1_000_000) * pricing["output_per_m"]
        return input_cost + cache_cost + output_cost

    async def _record_usage(self, task_type: str, model: str, response: Any,
                             user_openid: str = "", session_id: str = "",
                             provider: str = "") -> None:
        try:
            usage = response.usage
            if not usage:
                return
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            cache_hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
            cache_miss = getattr(usage, "prompt_cache_miss_tokens", 0) or 0
            cost = self._calc_cost(prompt_tokens, completion_tokens, cache_hit, cache_miss, model, provider)

            record = {
                "user_openid": user_openid,
                "session_id": session_id,
                "model": model,
                "task_type": task_type,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cache_hit_tokens": cache_hit,
                "cache_miss_tokens": cache_miss,
                "cost_usd": cost,
                "created_at": time.time(),
            }

            if self._analytics:
                self._cost_buffer.append(record)
                if len(self._cost_buffer) >= self._cost_flush_threshold:
                    await self._flush_cost_buffer()
            else:
                logger.debug("router.usage_no_db", task=task_type, cost=f"${cost:.6f}")
        except (OSError, KeyError, ValueError, TypeError) as e:
            logger.warning("router.usage_record_failed", error=str(e))

    async def _record_stream_usage(self, task_type: str, model: str, stream_response: Any,
                                    user_openid: str = "", session_id: str = "",
                                    provider: str = "") -> None:
        """流式调用结束后记录费用：聚合 chunk 的 usage（OpenAI 在最后一个 chunk 提供）。"""
        try:
            usage = getattr(stream_response, "usage", None)
            if not usage:
                # 部分 SDK 需要消费完流才能拿到 usage，这里尝试读取已关闭流的属性
                return
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            cache_hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
            cache_miss = getattr(usage, "prompt_cache_miss_tokens", 0) or 0
            cost = self._calc_cost(prompt_tokens, completion_tokens, cache_hit, cache_miss, model, provider)
            record = {
                "user_openid": user_openid,
                "session_id": session_id,
                "model": model,
                "task_type": task_type,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cache_hit_tokens": cache_hit,
                "cache_miss_tokens": cache_miss,
                "cost_usd": cost,
                "created_at": time.time(),
            }
            if self._analytics:
                self._cost_buffer.append(record)
                if len(self._cost_buffer) >= self._cost_flush_threshold:
                    await self._flush_cost_buffer()
            else:
                logger.debug("router.stream_usage_no_db", task=task_type, cost=f"${cost:.6f}")
        except (OSError, KeyError, ValueError, TypeError) as e:
            logger.warning("router.stream_usage_record_failed", error=str(e))

    async def _flush_cost_buffer(self) -> None:
        if not self._cost_buffer or not self._analytics:
            return
        try:
            await self._analytics.batch_insert_api_usage(self._cost_buffer)
            count = len(self._cost_buffer)
            self._cost_buffer.clear()
            logger.debug("router.cost_flushed", count=count)
        except (OSError, KeyError, ValueError) as e:
            logger.warning("router.cost_flush_failed", error=str(e))

    async def flush_costs(self) -> None:
        await self._flush_cost_buffer()

    async def close(self) -> None:
        """关闭所有 AsyncOpenAI 客户端, 释放 TCP 连接."""
        for client in (self._client, self._agnes_client):
            if client is not None:
                try:
                    await client.close()
                except (RuntimeError, OSError, _openai_mod.APIError):
                    logger.debug("model_router.close_client_error", exc_info=True)
        self._client = None
        self._agnes_client = None
        # 关闭自定义 provider 客户端
        for cp_client in list(getattr(self, "_custom_clients", {}).values()):
            try:
                await cp_client.close()
            except (RuntimeError, OSError, _openai_mod.APIError):
                logger.debug("model_router.close_custom_client_error", exc_info=True)
        if hasattr(self, "_custom_clients"):
            self._custom_clients.clear()

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
            return self._agnes_client is not None
        return provider in getattr(self, "_custom_clients", {})

    async def _try_fallback_chain(self, e: Exception, task_type: str,
                                  messages: list[dict], temperature: float,
                                  stream: bool, tools: list[dict] | None,
                                  tool_choice: str | None, timeout: int,
                                  user_openid: str, session_id: str,
                                  extra_headers: dict | None) -> str | object | None:
        """多级 fallback：FALLBACK_ROUTE → Agnes → 自定义 provider。全部失败返回 None。

        每一级降级前都会检查目标客户端是否已配置（有 API key 且有 base_url），
        未配置的目标会被跳过，避免向未初始化的客户端发起无意义调用。
        """
        # 1. 降级到更便宜的模型
        fallback_type = FALLBACK_ROUTE.get(task_type)
        if fallback_type:
            fallback_config = ROUTE_TABLE.get(fallback_type)
            fallback_provider = fallback_config.get("client", _CFG_DEFAULT_PROVIDER) if fallback_config else _CFG_DEFAULT_PROVIDER
            # D12: 降级前检查目标客户端是否已配置，未配置则跳过该降级目标
            if fallback_config and self._is_client_configured(fallback_provider):
                logger.warning("router.fallback",
                               original_task=task_type, fallback_task=fallback_type,
                               error=f"{type(e).__name__}: {e}")
                try:
                    fallback_tools = self._filter_tools_for_model(tools, fallback_config.get("model", ""))
                    return await self._route_with_retry(
                        fallback_type, fallback_config, messages, temperature,
                        fallback_config.get("max_tokens", 1000), stream,
                        fallback_tools, tool_choice, timeout, user_openid, session_id,
                        extra_headers=extra_headers,
                    )
                except (RuntimeError, OSError, KeyError, ValueError) as fb_err:
                    logger.error("router.fallback_failed",
                                 fallback_task=fallback_type,
                                 error=f"{type(fb_err).__name__}: {fb_err}")
            else:
                logger.warning("router.fallback_skipped",
                               original_task=task_type, fallback_task=fallback_type,
                               reason="target client not configured")

        # 2. 尝试 Agnes 作为最终降级
        if task_type not in ("chat_agnes",) and self._is_client_configured("agnes"):
            try:
                agnes_config = ROUTE_TABLE.get("chat_agnes")
                if agnes_config:
                    logger.warning("router.agnes_fallback", original_task=task_type)
                    agnes_tools = self._filter_tools_for_model(tools, agnes_config.get("model", ""))
                    return await self._route_with_retry(
                        "chat_agnes", agnes_config, messages, temperature,
                        agnes_config.get("max_tokens", 2000), stream,
                        agnes_tools, tool_choice, timeout, user_openid, session_id,
                        extra_headers=extra_headers,
                    )
            except (RuntimeError, OSError, KeyError, ValueError) as agnes_err:
                logger.error("router.agnes_fallback_failed", error=str(agnes_err))

        # 3. 尝试已注册的自定义 provider（SiliconFlow/OpenRouter/ModelScope 等）
        if task_type.startswith("chat") and self._custom_clients:
            for cp_name, cp_client in list(self._custom_clients.items()):
                try:
                    cp_model = self._get_custom_provider_default_model(cp_name)
                    if not cp_model:
                        continue
                    cp_config = {"model": cp_model, "max_tokens": 1000, "client": cp_name}
                    logger.warning("router.custom_provider_fallback",
                                   original_task=task_type, provider=cp_name, model=cp_model)
                    cp_tools = self._filter_tools_for_model(tools, cp_model)
                    return await self._route_with_retry(
                        f"chat_{cp_name}", cp_config, messages, temperature,
                        1000, stream, cp_tools, tool_choice, timeout,
                        user_openid, session_id,
                        extra_headers=extra_headers,
                    )
                except (RuntimeError, OSError, KeyError, ValueError) as cp_err:
                    logger.error("router.custom_provider_fallback_failed",
                                 provider=cp_name, error=str(cp_err))
                    continue
        return None

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
        config = ROUTE_TABLE.get(task_type, ROUTE_TABLE["chat"])
        mt = max_tokens or config.get("max_tokens", 1500)
        if timeout is None:
            timeout = self.TASK_TIMEOUTS.get(task_type, 30)

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
        except (RuntimeError, OSError, KeyError, ValueError, TypeError, _openai_mod.APIError) as e:
            metrics.inc(f"model_route.{task_type}.failure")
            metrics.observe(f"model_route.{task_type}.duration", time.time() - _start)
            metrics.maybe_report()
            # 结构化日志：LLM 调用失败
            logger.warning("llm.call_failed", event="llm_call", model=config.get("model", ""),
                           task=task_type, duration_ms=int((time.time() - _start) * 1000),
                           user_id=user_openid, session_id=session_id,
                           error=f"{type(e).__name__}: {e}")
            fb_result = await self._try_fallback_chain(
                e, task_type, messages, temperature, stream,
                tools, tool_choice, timeout, user_openid, session_id, extra_headers
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

    def _apply_prompt_caching(self, provider: str, messages: list[dict]) -> list[dict]:
        """应用 Prompt Caching（MiMo 直接启用；其他 provider 在 PROMPT_CACHING_ENABLED 时尝试）。"""
        if provider == "mimo":
            return apply_cache_control(messages)
        if not PROMPT_CACHING_ENABLED:
            return messages
        # P6: 对硅基流动/Qwen 等 OpenAI 兼容端点尝试启用 cache_control，
        # 不支持时由 API 端返回 400，下方的错误处理会静默降级。
        try:
            messages = apply_cache_control(messages)
            logger.debug("router.cache_control_applied provider={}", provider)
        except (KeyError, ValueError, TypeError) as ce:
            logger.debug("router.cache_control_skip provider={} error={}", provider, str(ce))
        return messages

    async def _select_client_for_provider(self, provider: str) -> Any:
        """选择指定 provider 的客户端（含懒注册和凭证锁）。无可用客户端时 raise LLMError。"""
        lock = self._get_credential_lock(provider)
        async with lock:
            client = self._client
            if provider == "agnes" and self._agnes_client:
                client = self._agnes_client
            elif provider not in ("mimo", "agnes"):
                custom = getattr(self, "_custom_clients", {}).get(provider)
                if custom is None:
                    # 懒注册：从 config_service 恢复未注册的自定义 provider
                    self._lazy_register_provider(provider)
                    custom = getattr(self, "_custom_clients", {}).get(provider)
                if custom is None:
                    raise LLMError(
                        f"自定义 provider {provider} 未注册或缺少 API Key",
                        error_code=ErrorCodeEnum.E_LLM006,
                    )
                client = custom
        if not client:
            raise LLMError(
                "MiMo client not initialized, check MIMO_API_KEY",
                error_code=ErrorCodeEnum.E_LLM006,
            )
        return client

    @staticmethod
    def _build_stream_kwargs(model: str, messages: list[dict], temperature: float,
                             mt: int, extra_headers: dict | None,
                             config: dict, provider: str) -> dict:
        """构造流式调用 kwargs。"""
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": mt,
            "stream": True,
        }
        if extra_headers:
            kwargs["extra_headers"] = extra_headers
        # 支持 thinking 参数（通用）
        thinking_config = config.get("thinking")
        if thinking_config:
            if provider == "agnes":
                kwargs["extra_body"] = {
                    "chat_template_kwargs": {"enable_thinking": thinking_config.get("type") == "enabled"}
                }
            else:
                kwargs["extra_body"] = {"thinking": thinking_config}
        return kwargs

    async def chat_stream(self, messages: list, task_type: str = "chat",
                          temperature: float = 0.7, max_tokens: int = 2000,
                          user_openid: str = "", session_id: str = "",
                          extra_headers: dict | None = None,
                          tools: list[dict] | None = None,
                          tool_choice: str | None = None) -> AsyncIterator[str]:
        """流式调用 LLM，yield 每个 chunk 的 delta content。

        复用 _route_with_retry 的重试/错误分类/凭证轮换逻辑，
        不再独立实现一套调用路径，保证行为一致性。
        """
        config = ROUTE_TABLE.get(task_type, ROUTE_TABLE["chat"])
        model = config["model"]
        mt = max_tokens or config.get("max_tokens", 1500)
        provider = config.get("client", _CFG_DEFAULT_PROVIDER)
        timeout = self.TASK_TIMEOUTS.get(task_type, 30)

        messages = self._apply_prompt_caching(provider, messages)
        extra_headers = self._apply_caching_headers(extra_headers)
        tools = self._filter_tools_for_model(tools, model)

        _start = time.time()
        stream = None
        last_error = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                client = await self._select_client_for_provider(provider)
                kwargs = self._build_route_kwargs(
                    model, messages, temperature, mt, True,
                    tools, tool_choice, extra_headers, config, provider,
                )
                stream = await asyncio.wait_for(
                    client.chat.completions.create(**kwargs),
                    timeout=timeout,
                )
                async for chunk in stream:
                    try:
                        delta = chunk.choices[0].delta.content
                    except (AttributeError, IndexError):
                        delta = None
                    if delta:
                        yield delta
                metrics.inc(f"model_route.{task_type}.success")
                metrics.observe(f"model_route.{task_type}.duration", time.time() - _start)
                metrics.maybe_report()
                logger.info("llm.call", event="llm_call", model=model,
                            task=task_type, duration_ms=int((time.time() - _start) * 1000),
                            user_id=user_openid, session_id=session_id, stream=True)
                return
            except (RuntimeError, OSError, KeyError, ValueError, _openai_mod.APIError) as e:
                last_error = e
                if stream:
                    with contextlib.suppress(AttributeError, OSError):
                        await stream.close()
                    stream = None
                should_retry = await self._handle_route_exception(
                    e, provider, task_type, model, attempt,
                )
                if not should_retry:
                    break

        metrics.inc(f"model_route.{task_type}.failure")
        metrics.observe(f"model_route.{task_type}.duration", time.time() - _start)
        metrics.maybe_report()
        raise last_error or LLMError("流式调用失败")

    @staticmethod
    def _classify_error(exc: Exception) -> str:
        """将异常分类为可重试/不可重试错误类型。"""
        exc_name = type(exc).__name__.lower()
        exc_msg = str(exc).lower()
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
        kwargs = {
            "model": model,
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
        fp = config.get("frequency_penalty", 0.3)
        if fp:
            kwargs["frequency_penalty"] = fp
        # 论文验证有效值为 1.2，对条件模式重复有效；对结构化重复效果有限但无副作用
        pp = config.get("presence_penalty", 1.0)
        if pp:
            kwargs["presence_penalty"] = pp
        # 退化兜底停止序列：当模型开始输出工具定义泄露时立即停止
        kwargs["stop"] = ["Never use this AI assistant tool", "\"Never use"]
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
            # 诊断日志：记录发送给 LLM 的工具名称列表
            tool_names = [t.get("function", {}).get("name", "?") for t in tools]
            logger.debug("router.tools_sent provider=%s model=%s count=%d names=%s",
                         provider, model, len(tools), tool_names)
        if extra_headers:
            kwargs["extra_headers"] = extra_headers

        # 支持 thinking 参数（通用）
        thinking_config = config.get("thinking")
        if thinking_config:
            if provider == "agnes":
                kwargs["extra_body"] = {
                    "chat_template_kwargs": {"enable_thinking": thinking_config.get("type") == "enabled"}
                }
            else:
                kwargs["extra_body"] = {"thinking": thinking_config}
        return kwargs

    async def _handle_route_response(self, response: Any, task_type: str, model: str,
                                     stream: bool, user_openid: str, session_id: str,
                                     provider: str, tools: list[dict] | None) -> str | object:
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
        if not content and rc:
            content = rc
        return content

    async def _rotate_credential_on_error(self, provider: str, classified: Any) -> None:
        """当 ErrorClassifier 建议轮换凭证时，尝试获取新凭证并更新客户端。"""
        new_cred = await self._credential_pool.get_credential(provider)
        rotate_lock = self._get_credential_lock(provider)
        async with rotate_lock:
            current_key = ""
            if provider == "mimo" and self._client:
                current_key = self._client.api_key or ""
            elif provider == "agnes" and self._agnes_client:
                current_key = self._agnes_client.api_key or ""
            if new_cred and new_cred.api_key != current_key:
                logger.info("router.credential_rotated",
                            provider=provider,
                            key_suffix=new_cred.api_key[-6:])
                # 更新客户端使用新凭证
                new_client = AsyncOpenAI(
                    api_key=new_cred.api_key,
                    base_url=new_cred.base_url or (MIMO_BASE_URL if provider == "mimo" else AGNES_BASE_URL),
                )
                if provider == "mimo":
                    self._client = new_client
                else:
                    self._agnes_client = new_client

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
            logger.warning("router.retry", task=task_type, model=model,
                           attempt=attempt + 1, reason=classified.reason.value,
                           action=classified.action.value,
                           backoff=f"{backoff:.1f}s",
                           error=f"{type(e).__name__}: {e}")
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

        messages = self._apply_prompt_caching(provider, messages)
        # 主路由路径也需过滤工具，防止小模型收到工具定义后输出退化
        tools = self._filter_tools_for_model(tools, model)

        for attempt in range(MAX_RETRIES + 1):
            try:
                client = await self._select_client_for_provider(provider)
                kwargs = self._build_route_kwargs(
                    model, messages, temperature, max_tokens, stream,
                    tools, tool_choice, extra_headers, config, provider,
                )

                response = await asyncio.wait_for(
                    client.chat.completions.create(**kwargs),
                    timeout=timeout,
                )
                return await self._handle_route_response(
                    response, task_type, model, stream,
                    user_openid, session_id, provider, tools,
                )

            except (RuntimeError, OSError, KeyError, ValueError, _openai_mod.APIError) as e:
                last_error = e
                should_retry = await self._handle_route_exception(
                    e, provider, task_type, model, attempt,
                )
                if not should_retry:
                    break
        raise last_error

    def _track_cache(self, response: Any) -> None:
        try:
            usage = response.usage
            if not usage:
                return
            # MiMo 格式：prompt_cache_hit_tokens / prompt_cache_miss_tokens
            mimo_hit = 0
            if hasattr(usage, "prompt_cache_hit_tokens"):
                mimo_hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
                self._cache_stats["hit_tokens"] += mimo_hit
                self._cache_stats["miss_tokens"] += getattr(usage, "prompt_cache_miss_tokens", 0) or 0

            # P6 Task 27.1: OpenAI 兼容格式 cached_tokens
            # 优先 prompt_tokens_details.cached_tokens（标准 OpenAI 协议），
            # 仅当其为 0 或缺失时才回退到顶层 cached_tokens（部分 provider 简化字段），
            # 避免同一缓存命中值被两个字段同时累加导致统计翻倍。
            cached_from_details = 0
            prompt_details = getattr(usage, "prompt_tokens_details", None)
            if prompt_details is not None:
                cached_from_details = getattr(prompt_details, "cached_tokens", 0) or 0
            cached_top = getattr(usage, "cached_tokens", 0) or 0
            cached_tokens = cached_from_details if cached_from_details > 0 else cached_top

            # 去重：若 MiMo 的 prompt_cache_hit_tokens 与 OpenAI 的 cached_tokens 同时存在，
            # 只累加一次（避免 hit_tokens 重复计数）。
            if cached_tokens > 0 and mimo_hit == 0:
                self._cache_stats["hit_tokens"] += cached_tokens
            # _cached_tokens_total 只累加一次（已通过 cached_tokens 去重）
            if cached_tokens > 0:
                self._cached_tokens_total += cached_tokens

            # P6 Task 27.2: 每 100 次请求输出一次缓存命中统计
            self._request_count += 1
            if self._request_count % 100 == 0:
                logger.info("prompt_cache.stats requests={} cached_tokens={}",
                            self._request_count, self._cached_tokens_total)
        except (KeyError, ValueError, OSError) as e:
            logger.debug(f"缓存统计追踪失败: {e}")

    def _check_cache_health(self) -> None:
        now = time.time()
        if now - self._last_cache_warning < 300:
            return
        total = self._cache_stats["hit_tokens"] + self._cache_stats["miss_tokens"]
        if total > 10000:
            ratio = self._cache_stats["hit_tokens"] / total
            if ratio < 0.5:
                self._last_cache_warning = now
                logger.warning("router.cache_hit_low",
                               hit_ratio=f"{ratio:.1%}",
                               suggestion="考虑固定系统 prompt 前缀以提高缓存命中率")

    def get_cache_stats(self) -> dict:
        total = self._cache_stats["total_calls"]
        hit = self._cache_stats["hit_tokens"]
        miss = self._cache_stats["miss_tokens"]
        total_tokens = hit + miss
        return {
            "total_calls": total,
            "hit_tokens": hit,
            "miss_tokens": miss,
            "hit_ratio": round(hit / total_tokens, 3) if total_tokens > 0 else 0.0,
        }

    # 参数量 <= 14B 的小模型在接收大量工具定义时容易输出退化（乱码/JSON循环）
    _SMALL_MODEL_PATTERNS = (
        "7b", "8b", "4b", "3b", "1.5b", "1.8b", "0.5b",
        "mini", "tiny", "small",
    )

    def _is_small_model(self, model: str) -> bool:
        """判断是否为小模型（参数量 <= 14B），小模型不适合接收大量工具定义。"""
        model_lower = model.lower()
        # 明确的大模型标记
        for big in ("72b", "70b", "67b", "104b", "236b", "pro", "max", "plus", "large"):
            if big in model_lower:
                return False
        return any(small in model_lower for small in self._SMALL_MODEL_PATTERNS)

    def _filter_tools_for_model(self, tools: list[dict] | None, model: str) -> list[dict] | None:
        """检查工具列表与目标模型的兼容性，对小模型移除工具定义防止输出退化。

        根因：Qwen2.5-7B 等小模型在接收 30+ 个工具定义时，输出严重退化
        （循环输出 JSON 片段乱码），导致对话不可用。
        """
        if not tools:
            return tools

        # agnes 系列模型可能不支持工具调用，记录警告
        agnes_models = {AGENT_CONFIG.get("model") for AGENT_CONFIG in [ROUTE_TABLE.get("chat_agnes")] if AGENT_CONFIG}
        if model in agnes_models:
            tool_names = [t.get("function", {}).get("name", "?") for t in tools]
            logger.warning("router.tools_may_not_be_supported",
                           model=model, tool_count=len(tools), tools=tool_names)

        # 小模型不发送工具定义，防止输出退化
        if self._is_small_model(model):
            logger.warning("router.tools_stripped_for_small_model model={} tool_count={}", model, len(tools))
            return None

        return tools

    def pop_reasoning_content(self) -> str | None:
        rc = _reasoning_content_var.get("")
        _reasoning_content_var.set("")
        return rc if rc else None