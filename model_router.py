from typing import Any, ClassVar
from collections.abc import AsyncIterator
import hashlib
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


def _mask_api_key(key: str) -> str:
    """返回 API key 的 SHA-256 哈希前 8 字符，用于日志标识而不泄漏 key 片段。

    与 key_prefix/key_suffix 不同，hash 无法逆推出原始 key 内容；
    同一 key 的 hash 稳定，可用于日志中追踪凭证轮换。
    """
    if not key or len(key) < 8:
        # 短 key 通常是测试或无效值，直接返回占位符
        return "***"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


# ── Provider 元数据加载（替代代码中所有硬编码的 base_url / max_tokens_cap / 跨 provider 映射） ──
# P0 修复（用户要求"不要硬编码是任务的根本规则"）：
# 所有 provider 默认值（base_url、max_tokens_cap、default_model、跨 provider 兜底映射）
# 统一从 config/provider_metadata.json 加载，该文件标注每个值的 doc_url + doc_note 以便溯源。
#
# 优先级（从高到低）：
#   1. 环境变量（运维覆盖，如 AGNES_MAX_TOKENS_CAP / MIMO_BASE_URL）
#   2. provider_metadata.json（用户可编辑，含官方文档溯源）
#   3. config.py 中的 *_BASE_URL（向后兼容）
#   4. 空串 / None（安全兜底）
def _load_provider_metadata() -> dict:
    """加载 provider 元数据配置文件。

    查找顺序：
      1. 用户配置目录（get_config_dir()/provider_metadata.json）—— 用户可编辑覆盖
      2. 打包/源码 config/provider_metadata.json —— 内置默认值（含 doc_url 溯源）
      3. 空字典（极端兜底，所有 provider 用 None 不裁剪）
    """
    import json
    from pathlib import Path
    try:
        from config import get_config_dir as _get_config_dir
        user_path = _get_config_dir() / "provider_metadata.json"
        if user_path.exists():
            with open(user_path, "r", encoding="utf-8") as fp:
                return json.load(fp)
    except (ImportError, OSError, ValueError) as e:
        logger.debug("router.provider_metadata_user_load_failed: {}", e)
    # 兜底：源码/打包目录
    try:
        _bundled = Path(__file__).resolve().parent / "config" / "provider_metadata.json"
        if _bundled.exists():
            with open(_bundled, "r", encoding="utf-8") as fp:
                return json.load(fp)
    except (OSError, ValueError) as e:
        logger.warning("router.provider_metadata_bundled_load_failed: {}", e)
    return {}

_PROVIDER_METADATA: dict = _load_provider_metadata()
_PROVIDER_CAPS_FROM_FILE: dict = _PROVIDER_METADATA.get("providers", {}) if isinstance(_PROVIDER_METADATA, dict) else {}


def _load_provider_base_url(provider: str, env_var: str) -> str:
    """从环境变量 + provider_metadata.json 加载 provider base_url（无硬编码）。"""
    _env = os.getenv(env_var, "")
    if _env:
        return _env
    _meta = _PROVIDER_CAPS_FROM_FILE.get(provider, {}) if isinstance(_PROVIDER_CAPS_FROM_FILE, dict) else {}
    if isinstance(_meta, dict):
        _url = _meta.get("base_url_default", "")
        if _url:
            return _url
    return ""


MIMO_MODEL = os.getenv("MIMO_MODEL_NAME", "mimo-v2.5")
MIMO_PRO_MODEL = os.getenv("MIMO_PRO_MODEL_NAME", "mimo-v2.5-pro")
# P0 修复：MIMO_BASE_URL 从 provider_metadata.json 读取（不再硬编码 "https://api.xiaomimimo.com/v1"）
MIMO_BASE_URL = _load_provider_base_url("mimo", "MIMO_BASE_URL")
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
    "ollama": {
        "input_per_m": 0.0,
        "cache_hit_per_m": 0.0,
        "output_per_m": 0.0,
    },
    "default": {
        "input_per_m": 0.20,
        "cache_hit_per_m": 0.02,
        "output_per_m": 0.40,
    },
}

ROUTE_TABLE = {
    # chat 主路由：128K 上限，支撑长时间连贯对话，搭配滑动窗口+摘要压缩避免退化
    # 不再锁死 8192，避免长会话频繁截断历史导致记忆断裂
    "chat": {"model": _CFG_MODEL_NAME, "max_tokens": 131072, "client": _CFG_DEFAULT_PROVIDER, "thinking": {"type": "disabled"}},
    "chat_pro": {"model": _CFG_PRO_MODEL or _CFG_MODEL_NAME, "max_tokens": 131072, "client": _CFG_DEFAULT_PROVIDER, "thinking": {"type": "enabled", "budget_tokens": 4096}},
    # chat_flash：sub_agent 调用（如 xiaoli 转述），需要足够空间避免截断
    "chat_flash": {"model": _CFG_FLASH_MODEL or _CFG_MODEL_NAME, "max_tokens": 6144, "client": _CFG_DEFAULT_PROVIDER, "thinking": {"type": "disabled"}},
    "chat_mini": {"model": _CFG_FLASH_MODEL or _CFG_MODEL_NAME, "max_tokens": 4096, "client": _CFG_DEFAULT_PROVIDER, "thinking": {"type": "disabled"}},
    "chat_mimo": {"model": MIMO_MODEL, "max_tokens": 131072, "client": "mimo", "thinking": {"type": "disabled"}},
    # chat_ultra：1M 超大窗口，仅子代理临时调用（完整 Spec/源码/长文档分析），用完销毁
    "chat_ultra": {"model": _CFG_MODEL_NAME, "max_tokens": 1048576, "client": _CFG_DEFAULT_PROVIDER, "thinking": {"type": "disabled"}},
    "emotion_analysis": {"model": _CFG_FLASH_MODEL or _CFG_MODEL_NAME, "max_tokens": 1024, "client": _CFG_DEFAULT_PROVIDER, "thinking": {"type": "disabled"}},
    "tool_result_wrap": {"model": _CFG_FLASH_MODEL or _CFG_MODEL_NAME, "max_tokens": 2048, "client": _CFG_DEFAULT_PROVIDER, "thinking": {"type": "disabled"}},
    "memory_encoding": {"model": _CFG_FLASH_MODEL or _CFG_MODEL_NAME, "max_tokens": 4096, "client": _CFG_DEFAULT_PROVIDER, "thinking": {"type": "disabled"}},
    "chat_agnes": {"model": AGNES_TEXT_MODEL, "max_tokens": 131072, "client": "agnes", "thinking": {"type": "disabled"},
                   "presence_penalty": 0.3, "frequency_penalty": 0.0},
}

MODEL_PREFERENCES = {
    "mimo": {"label": "MiMo 模式", "desc": "使用小米 MiMo-V2.5 模型"},
    "mimo-pro": {"label": "MiMo Pro 模式", "desc": "使用小米 MiMo-V2.5-Pro 深度思考"},
    "mimo-flash": {"label": "MiMo Flash 模式", "desc": "使用小米 MiMo-V2.5 快速响应"},
    "mimo-mini": {"label": "MiMo Mini 模式", "desc": "使用小米 MiMo-V2.5 轻量任务"},
}

RETRYABLE_ERRORS = {'timeout', 'rate_limit', 'connection_error'}
MAX_RETRIES = 1
FALLBACK_ROUTE = {
    "chat_pro": "chat_flash",
    "chat_flash": "chat_mini",
    "chat_mini": "chat_agnes",
}

# P0 修复：per-provider max_tokens 上限（从配置文件 + 环境变量读取，无硬编码）
# 根因：ROUTE_TABLE 中 chat/chat_pro/chat_agnes/chat_mimo 都设了 131072，
#       但 agnes-2.0-flash 实际上限是 65536，超过会返回 500 InternalServerError
#       "max_tokens exceeds the limit of 65536"（日志中 286 次错误根因）。
#       之前的"一刀切"提升 max_tokens 到 131072 反而打破了 agnes provider。
# 修复：在 _build_route_kwargs / _build_stream_kwargs 中按 provider 取 min(mt, cap)。
#
# 上限来源（用户问"PROVIDER_MAX_TOKENS_CAP 上限来源呢？"）：
#   不再硬编码在代码里。来源链路如下（优先级从高到低）：
#     1. 环境变量 AGNES_MAX_TOKENS_CAP / MIMO_MAX_TOKENS_CAP / OLLAMA_MAX_TOKENS_CAP（最高优先级，运维覆盖）
#     2. config/provider_metadata.json 中各 provider 的 max_tokens_cap 字段（用户可编辑）
#        该文件标注了每个值的 doc_url + doc_note（官方文档链接 + 溯源说明）
#     3. None（不裁剪，安全兜底）
#   官方文档溯源：
#     - agnes-2.0-flash: 65536（https://docs.agnes-ai.com/，超过返回 500）
#     - mimo-v2.5:       131072（https://www.xiaomimimo.com/）
#     - ollama:          视本地模型而定，默认不裁剪
#
# 注：_load_provider_metadata / _PROVIDER_METADATA / _PROVIDER_CAPS_FROM_FILE
#     已在文件顶部（imports 之后）定义，此处直接复用。


def _load_provider_max_tokens_cap() -> dict[str, int | None]:
    """加载各 provider 的 max_tokens 上限。

    优先级：环境变量 > provider_metadata.json > None（不裁剪）。
    未配置的 provider 返回 None（不裁剪）。
    """
    from utils.common import safe_int as _safe_int

    def _env_cap(env_var: str) -> int | None:
        v = os.getenv(env_var)
        if v is None:
            return None
        # CodeRabbit #5 修复：空/无效值返回 None 而非 0
        # 原 implementation _safe_int(v, 0) 解析失败返回 0，被当作合法 cap
        # 导致 PROVIDER_MAX_TOKENS_CAP[p] = 0，所有请求 max_tokens=0，LLM 无法生成
        parsed = _safe_int(v, 0)
        return parsed if parsed > 0 else None

    def _file_cap(provider: str) -> int | None:
        meta = _PROVIDER_CAPS_FROM_FILE.get(provider, {})
        v = meta.get("max_tokens_cap") if isinstance(meta, dict) else None
        if v is None or v == 0:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    # 已知 provider 列表（从元数据文件取，找不到则空）
    _known_providers = set(_PROVIDER_CAPS_FROM_FILE.keys()) | {"agnes", "mimo", "ollama"}
    cap: dict[str, int | None] = {}
    for p in _known_providers:
        _env_var_map = {"agnes": "AGNES_MAX_TOKENS_CAP", "mimo": "MIMO_MAX_TOKENS_CAP", "ollama": "OLLAMA_MAX_TOKENS_CAP"}
        env_v = _env_cap(_env_var_map.get(p, f"{p.upper()}_MAX_TOKENS_CAP"))
        if env_v is not None:
            cap[p] = env_v
        else:
            cap[p] = _file_cap(p)
    return cap

PROVIDER_MAX_TOKENS_CAP: dict[str, int | None] = _load_provider_max_tokens_cap()

# 跨 provider 映射：主 provider 故障时，flash/mini 切换到不同 provider
# P0 修复：从 provider_metadata.json 的 _cross_provider_fallback 加载（不再硬编码）
def _load_cross_provider_map() -> dict[str, tuple[str, str]]:
    """从 provider_metadata.json 加载跨 provider 兜底映射。

    优先级：provider_metadata.json > 环境变量推导 > 空字典（不兜底）。
    """
    _result: dict[str, tuple[str, str]] = {}
    _cfg = _PROVIDER_METADATA.get("_cross_provider_fallback", {}) if isinstance(_PROVIDER_METADATA, dict) else {}
    for _p, _fb in _cfg.items():
        if _p.startswith("_"):
            continue  # 跳过 _comment 等元字段
        if isinstance(_fb, dict):
            _fp = _fb.get("fallback_provider", "")
            _fm = _fb.get("fallback_model", "")
            if _fp and _fm:
                _result[_p] = (_fp, _fm)
    # 兜底：环境变量推导（用户未配置 JSON 时仍可用）
    if "agnes" not in _result and os.getenv("MIMO_MODEL_NAME"):
        _result["agnes"] = ("mimo", os.getenv("MIMO_MODEL_NAME"))
    if "mimo" not in _result and os.getenv("AGNES_TEXT_MODEL"):
        _result["mimo"] = ("agnes", os.getenv("AGNES_TEXT_MODEL"))
    return _result

_CROSS_PROVIDER_MAP: dict[str, tuple[str, str]] = _load_cross_provider_map()

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
        "chat_flash": 30,
        "chat": 60,
        "chat_pro": 60,
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
        self._db = db
        self._model_preference = "mimo"
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
        self._agnes_client = AsyncOpenAI(api_key=_agnes_key, base_url=_agnes_url) if _agnes_key else None

        self._custom_clients: dict[str, AsyncOpenAI] = {}
        self._register_credential_pool_providers()
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

    def _register_credential_pool_providers(self) -> None:
        """从凭证池主动注册非 mimo/agnes 的 Provider 到 _custom_clients。

        确保本地 Provider（如 Ollama）和免费平台（如 SiliconFlow）在路由器
        初始化时即被注册，不依赖 Web 服务的 _register_env_providers 流程。
        """
        try:
            from web.custom_providers import register_into_router
        except ImportError:
            logger.debug("router.credential_pool_register_skip web module unavailable")
            return
        _BUILTIN_PROVIDERS = {"mimo", "agnes"}
        _PROVIDER_FORMAT = {
            "ollama": "openai",
            "siliconflow": "openai",
            "openrouter": "openai",
            "modelscope": "openai",
        }
        pool = self._credential_pool
        for provider, creds in pool._pool.items():
            if provider in _BUILTIN_PROVIDERS:
                continue
            if provider in self._custom_clients:
                continue
            if not creds:
                continue
            cred = creds[0]
            fmt = _PROVIDER_FORMAT.get(provider, "openai")
            if cred.base_url and cred.api_key:
                register_into_router(self, provider, fmt, cred.base_url, cred.api_key)
                logger.info("router.credential_pool_registered provider={} format={}", provider, fmt)

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
                        key_len=len(new_mimo_key),
                        key_hash=_mask_api_key(new_mimo_key))
        else:
            self._client = None

        new_agnes_key = os.getenv("AGNES_API_KEY", "")
        new_agnes_url = os.getenv("AGNES_BASE_URL", AGNES_BASE_URL)
        if new_agnes_key:
            _ssrf_check(new_agnes_url)  # SSRF 防护：校验 base_url
            self._agnes_client = AsyncOpenAI(api_key=new_agnes_key, base_url=new_agnes_url)
            logger.info("router.agnes_client_refreshed",
                        key_len=len(new_agnes_key),
                        key_hash=_mask_api_key(new_agnes_key))
        else:
            self._agnes_client = None

        # 关闭旧客户端释放连接
        _old_clients: list = []
        for old in (old_mimo, old_agnes):
            if old is not None and old not in (self._client, self._agnes_client):
                _old_clients.append(old)
        if _old_clients:
            try:
                import asyncio
                loop = asyncio.get_running_loop()

                async def _close_old() -> None:
                    await asyncio.gather(
                        *[c.close() for c in _old_clients],
                        return_exceptions=True,
                    )

                task = loop.create_task(_close_old())
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
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
        already_exists = any(c.api_key == api_key for c in existing)
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

        # 全量同步：所有聊天类 + 轻量任务 task_type 都跟随主 provider
        # 用户切换 provider 时，确保所有场景都用目标 provider，不再残留旧 provider
        _sync_tasks = ("chat_pro", "chat_flash", "chat_mini", "chat_mimo",
                       "chat_ultra", "emotion_analysis", "tool_result_wrap",
                       "memory_encoding")
        for _task in _sync_tasks:
            if _task in ROUTE_TABLE:
                ROUTE_TABLE[_task]["model"] = model_id
                ROUTE_TABLE[_task]["client"] = provider
                # agnes 不支持 thinking，切换到 agnes 时禁用 thinking
                if provider == "agnes" and "thinking" in ROUTE_TABLE[_task]:
                    ROUTE_TABLE[_task]["thinking"] = {"type": "disabled"}
        # P0 修复（用户反馈"UI 设置全部 Agnes，哪来的 Mimo v2.5"根因）：
        # 删除"chat_flash 跨 provider 降级"逻辑——它强制把 chat_flash 改成不同 provider，
        # 违背用户明确选择（用户选 agnes 时 chat_flash 被改成 mimo，触发 content_filter，
        # 导致 14 秒延迟 + verification retry + 工具格式泄漏）。
        # fallback 多样化应由 FALLBACK_ROUTE 在故障时处理，而非在用户主动切换时改 chat_flash。
        # if provider in _CROSS_PROVIDER_MAP:
        #     fb_provider, fb_model = _CROSS_PROVIDER_MAP[provider]
        #     if "chat_flash" in ROUTE_TABLE:
        #         ROUTE_TABLE["chat_flash"]["client"] = fb_provider
        #         ROUTE_TABLE["chat_flash"]["model"] = fb_model
        logger.info("router.all_tasks_synced",
                    provider=provider, model=model_id,
                    synced_tasks=list(_sync_tasks))

        self._current_chat_model = {"provider": provider, "model_id": model_id}
        # 持久化到 config_service，以便重启后恢复上次聊天模型
        # 必须同步写入 models.chat_model 与 models.routes.chat，避免两套数据不同步
        # 否则 _apply_route_overrides 与 _restore_chat_model 启动顺序会造成覆盖
        try:
            from web.config_service import get_config_service
            cfg = get_config_service()
            cfg.set(
                "models.chat_model",
                {"provider": provider, "model_id": model_id},
            )
            # P0 修复（用户反馈"UI 设置 Agnes 但 chat_flash 还显示 mimo"根因）：
            # 持久化所有同步过的 task 路由到 webui_overrides.json，避免 WebUI 显示与运行时不一致
            # 之前只持久化 chat，其他 task（chat_flash/chat_mini/chat_pro 等）保留旧配置，
            # 导致用户在 WebUI 看到 chat_flash 还是 mimo，误以为没生效
            for _task_name in _sync_tasks:
                _entry = ROUTE_TABLE.get(_task_name, {})
                if not _entry:
                    continue
                cfg.set(f"models.routes.{_task_name}", {
                    "model": _entry.get("model", model_id),
                    "client": _entry.get("client", provider),
                    "max_tokens": _entry.get("max_tokens"),
                    "thinking": bool(
                        _entry.get("thinking")
                        and isinstance(_entry.get("thinking"), dict)
                        and _entry["thinking"].get("type") == "enabled"
                    ),
                    "timeout": self.TASK_TIMEOUTS.get(_task_name, 60),
                })
            # chat 主路由单独持久化（确保 max_tokens 等字段完整）
            chat_entry = ROUTE_TABLE.get("chat", {})
            cfg.set("models.routes.chat", {
                "model": model_id,
                "client": provider,
                "max_tokens": chat_entry.get("max_tokens"),
                "thinking": bool(
                    chat_entry.get("thinking")
                    and isinstance(chat_entry.get("thinking"), dict)
                    and chat_entry["thinking"].get("type") == "enabled"
                ),
                "timeout": self.TASK_TIMEOUTS.get("chat"),
            })
        except (OSError, KeyError, ValueError, TypeError) as e:
            logger.warning("router.chat_model_persist_failed error={}", str(e))
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
        cfg = ROUTE_TABLE.get(task_type) or ROUTE_TABLE.get("chat", {})
        return int(cfg.get("max_tokens", 60000))

    def get_active_max_tokens(self) -> int:
        """获取当前激活模型偏好的实际上下文窗口大小。

        根据 _model_preference（mimo/mimo-pro/mimo-flash/mimo-mini/custom）解析对应 task_type，
        再从 ROUTE_TABLE 取 max_tokens。供 AgentContext 动态压缩阈值使用。
        """
        task_type = self.resolve_task_type("chat")
        return self.get_max_tokens_for_task(task_type)

    # 已知自定义 provider 的默认模型映射
    # 注意：这些是 fallback 值，当 provider 的 default_model 为空时使用
    # 建议通过 /models/health-check 端点定期验证这些模型ID是否仍然可用
    _CUSTOM_PROVIDER_DEFAULT_MODELS: ClassVar[dict[str, str]] = {
        "ollama": "qwen2.5",
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
                                  extra_headers: dict | None,
                                  original_max_tokens: int | None = None) -> str | object | None:
        """多级 fallback：FALLBACK_ROUTE → Agnes → 自定义 provider。全部失败返回 None。

        每一级降级前都会检查目标客户端是否已配置（有 API key 且有 base_url），
        未配置的目标会被跳过，避免向未初始化的客户端发起无意义调用。

        P0 修复（Task 1.3）：透传 original_max_tokens，避免 fallback 把 max_tokens 压到 1000。
        根因：原实现 fallback_config.get("max_tokens", 1000) 会把 Web UI 的 32768 压到 1000，
              起点太小 → 截断续写翻倍序列 1000→2000→...→128000 需 7 次递归。
        修复：fallback 时取 max(original_max_tokens, fallback_default)。
        """
        # 1. 降级到更便宜的模型
        fallback_type = FALLBACK_ROUTE.get(task_type)
        # P0 修复：content_filter 触发时跳过同 provider 的 fallback 目标
        # 根因：chat_flash (mimo) content_filter → fallback 到 chat_mini (也是 mimo) → 再次 content_filter
        # 浪费一次调用 + 触发 verification retry，导致 14 秒延迟
        # 修复：content_filter 时跳过同 provider 的 fallback，直接到不同 provider（如 agnes）
        _is_content_filter = "content_filter" in str(e) or "content_policy" in str(e)
        _original_provider = ""
        try:
            _original_provider = ROUTE_TABLE.get(task_type, {}).get("client", _CFG_DEFAULT_PROVIDER)
        except Exception:
            pass
        while fallback_type:
            fallback_config = ROUTE_TABLE.get(fallback_type)
            fallback_provider = fallback_config.get("client", _CFG_DEFAULT_PROVIDER) if fallback_config else _CFG_DEFAULT_PROVIDER
            # content_filter 时跳过同 provider（同样的过滤模型会再次拦截）
            if _is_content_filter and fallback_provider == _original_provider:
                logger.warning("router.fallback_skip_same_provider",
                               original_task=task_type, fallback_task=fallback_type,
                               reason="content_filter: same provider will filter again")
                # 跳到下一级 fallback
                fallback_type = FALLBACK_ROUTE.get(fallback_type)
                continue
            # D12: 降级前检查目标客户端是否已配置，未配置则跳过该降级目标
            if fallback_config and self._is_client_configured(fallback_provider):
                break
            fallback_type = FALLBACK_ROUTE.get(fallback_type)
        if fallback_type:
            logger.warning("router.fallback",
                           original_task=task_type, fallback_task=fallback_type,
                           error=f"{type(e).__name__}: {e}")
            try:
                fallback_tools = self._filter_tools_for_model(tools, fallback_config.get("model", ""))
                # P0 修复：透传 original_max_tokens，避免被 fallback_config 默认值压缩
                _fallback_max_tokens = fallback_config.get("max_tokens", 1000)
                if original_max_tokens:
                    _fallback_max_tokens = max(original_max_tokens, _fallback_max_tokens)
                return await self._route_with_retry(
                    fallback_type, fallback_config, messages, temperature,
                    _fallback_max_tokens, stream,
                    fallback_tools, tool_choice, timeout, user_openid, session_id,
                    extra_headers=extra_headers,
                )
            except (RuntimeError, OSError, KeyError, ValueError) as fb_err:
                logger.error("router.fallback_failed",
                             fallback_task=fallback_type,
                             error=f"{type(fb_err).__name__}: {fb_err}")

        # 2. 尝试 Agnes 作为最终降级
        if task_type not in ("chat_agnes",) and self._is_client_configured("agnes"):
            try:
                agnes_config = ROUTE_TABLE.get("chat_agnes")
                if agnes_config:
                    logger.warning("router.agnes_fallback", original_task=task_type)
                    agnes_tools = self._filter_tools_for_model(tools, agnes_config.get("model", ""))
                    # P0 修复：透传 original_max_tokens
                    _agnes_max_tokens = agnes_config.get("max_tokens", 2000)
                    if original_max_tokens:
                        _agnes_max_tokens = max(original_max_tokens, _agnes_max_tokens)
                    return await self._route_with_retry(
                        "chat_agnes", agnes_config, messages, temperature,
                        _agnes_max_tokens, stream,
                        agnes_tools, tool_choice, timeout, user_openid, session_id,
                        extra_headers=extra_headers,
                    )
            except (RuntimeError, OSError, KeyError, ValueError) as agnes_err:
                logger.error("router.agnes_fallback_failed", error=str(agnes_err))

        # 3. 尝试已注册的自定义 provider（SiliconFlow/OpenRouter/ModelScope 等）
        if task_type.startswith("chat") and self._custom_clients:
            for cp_name, _cp_client in list(self._custom_clients.items()):
                try:
                    cp_model = self._get_custom_provider_default_model(cp_name)
                    if not cp_model:
                        continue
                    cp_config = {"model": cp_model, "max_tokens": 1000, "client": cp_name}
                    logger.warning("router.custom_provider_fallback",
                                   original_task=task_type, provider=cp_name, model=cp_model)
                    cp_tools = self._filter_tools_for_model(tools, cp_model)
                    # P0 修复：透传 original_max_tokens，避免硬编码 1000 压缩
                    _cp_max_tokens = 1000
                    if original_max_tokens:
                        _cp_max_tokens = max(original_max_tokens, 1000)
                    return await self._route_with_retry(
                        f"chat_{cp_name}", cp_config, messages, temperature,
                        _cp_max_tokens, stream, cp_tools, tool_choice, timeout,
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
        mt = max_tokens or config.get("max_tokens", 4096)
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
        """选择指定 provider 的客户端（含懒注册和凭证锁）。无可用客户端时 raise LLMError。

        P0 修复（cannot read image 根因）：
        - refresh_client() 在凭证轮换时可能把 self._client / self._agnes_client 置 None
          （例如 Setup 页面保存空 Key、或并发刷新时 env var 暂时为空）。
        - 原实现直接 raise E_LLM006，导致 _describe_images 拿不到 client → "cannot read image"。
        - 修复：在锁内做"懒恢复"——若 client 为 None，从当前 os.environ 重新读取 Key 重建。
          仍无 Key 才 raise。这样凭证池/环境变量恢复后无需重启即可自愈。
        """
        lock = self._get_credential_lock(provider)
        async with lock:
            client = self._client
            if provider == "agnes":
                client = self._agnes_client
                # P0：agnes client 懒恢复（防止 refresh_client 把它置 None 后无法自愈）
                if client is None:
                    _agnes_key = os.getenv("AGNES_API_KEY", "")
                    _agnes_url = os.getenv("AGNES_BASE_URL", AGNES_BASE_URL)
                    if _agnes_key:
                        try:
                            _ssrf_check(_agnes_url)
                            self._agnes_client = AsyncOpenAI(
                                api_key=_agnes_key, base_url=_agnes_url)
                            client = self._agnes_client
                            logger.info("router.agnes_client_lazy_recovered",
                                        key_hash=_mask_api_key(_agnes_key))
                        except (ValueError, OSError) as ce:
                            logger.warning("router.agnes_client_lazy_recover_failed",
                                           error=str(ce))
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
            else:
                # provider == "mimo"
                # P0：mimo client 懒恢复（防止 refresh_client 把它置 None 后 vision API 全挂）
                if client is None:
                    _mimo_key = os.getenv("MIMO_API_KEY", "")
                    _mimo_url = os.getenv("MIMO_BASE_URL", MIMO_BASE_URL)
                    if _mimo_key:
                        try:
                            _ssrf_check(_mimo_url)
                            self._client = AsyncOpenAI(
                                api_key=_mimo_key, base_url=_mimo_url)
                            client = self._client
                            logger.info("router.mimo_client_lazy_recovered",
                                        key_hash=_mask_api_key(_mimo_key))
                        except (ValueError, OSError) as ce:
                            logger.warning("router.mimo_client_lazy_recover_failed",
                                           error=str(ce))
        if not client:
            raise LLMError(
                f"{provider} client not initialized, check API_KEY env var",
                error_code=ErrorCodeEnum.E_LLM006,
            )
        return client

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
            _chat_cfg = ROUTE_TABLE.get("chat", {})
            _main_provider = str(_chat_cfg.get("client", ""))
            _main_model = str(_chat_cfg.get("model", ""))
        except (KeyError, AttributeError):
            pass

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
                            if _p in ("mimo", "agnes") or _p in getattr(self, "_custom_clients", {}):
                                return _p, _m
                        except Exception:
                            pass

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

    @staticmethod
    def _build_stream_kwargs(model: str, messages: list[dict], temperature: float,
                             mt: int, extra_headers: dict | None,
                             config: dict, provider: str) -> dict:
        """构造流式调用 kwargs。"""
        # P0 修复：按 provider 上限裁剪 max_tokens（agnes 上限 65536）
        mt = ModelRouter._cap_max_tokens(mt, provider)
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
        # 关键修复：thinking 关闭时也要传递 enable_thinking: false，否则 agnes 模型使用默认行为
        thinking_config = config.get("thinking")
        # P0 修复：thinking_debug 从 INFO 降为 DEBUG（每次 stream 调用都触发，INFO 级别刷屏）
        logger.debug("router.thinking_debug provider={} thinking={}", provider, thinking_config)
        if provider == "agnes":
            # agnes 模型需要明确传递 enable_thinking 参数
            enabled = bool(thinking_config and thinking_config.get("type") == "enabled")
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": enabled}}
        elif thinking_config:
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

        P0 修复（截断检测根因）：
        原实现在 async for chunk in stream 循环中只 yield delta.content，
        从不读取 chunk.choices[0].finish_reason，导致：
          1. _stream_finish_reason_var 永远为 None
          2. verification loop 无法检测 finish_reason="length"（max_tokens 截断）
          3. 截断重试机制完全失效（用户反复反馈"截断问题又出现了"根因）
        修复：在流结束时捕获最后一个 chunk 的 finish_reason，写入 ContextVar。
        """
        config = ROUTE_TABLE.get(task_type, ROUTE_TABLE["chat"])
        model = config["model"]
        mt = max_tokens or config.get("max_tokens", 4096)
        provider = config.get("client", _CFG_DEFAULT_PROVIDER)
        timeout = self.TASK_TIMEOUTS.get(task_type, 30)

        messages = self._apply_prompt_caching(provider, messages)
        extra_headers = self._apply_caching_headers(extra_headers)
        tools = self._filter_tools_for_model(tools, model)

        _start = time.time()
        stream = None
        last_error = None
        _stream_finish_reason: str | None = None

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
                # P0 修复（qq_group 截断根因）：添加 stall timeout 检测死流
                # 根因：原实现在 async for chunk in stream 中无 stall timeout，
                # 如果 provider 中途关闭连接且不发送 finish_reason chunk，
                # 循环会正常结束（无异常），content 被静默截断，
                # _stream_finish_reason 保持 None → 不触发 length retry → 用户看到截断回复。
                # 修复：用 asyncio.wait_for 包装每个 chunk 的读取，15 秒无新 chunk → TimeoutError
                _stall_timeout = float(os.getenv("LLM_STREAM_STALL_TIMEOUT", "15"))
                _chunk_count = 0
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            stream.__anext__(),
                            timeout=_stall_timeout,
                        )
                    except StopAsyncIteration:
                        break  # 流正常结束
                    _chunk_count += 1
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
                        pass
                    # 截断诊断日志：finish_reason="length" 时记录 mt 和内容长度
                    if _stream_finish_reason == "length":
                        logger.warning("llm.stream_truncated_by_max_tokens",
                                       model=model, task=task_type,
                                       max_tokens=mt, provider=provider,
                                       finish_reason=_stream_finish_reason)
                metrics.inc(f"model_route.{task_type}.success")
                metrics.observe(f"model_route.{task_type}.duration", time.time() - _start)
                metrics.maybe_report()
                logger.info("llm.call", event="llm_call", model=model,
                            task=task_type, duration_ms=int((time.time() - _start) * 1000),
                            user_id=user_openid, session_id=session_id, stream=True,
                            finish_reason=_stream_finish_reason)
                return
            except (RuntimeError, OSError, KeyError, ValueError, _openai_mod.APIError,
                    asyncio.TimeoutError) as e:
                # P0 修复：捕获 stall timeout（asyncio.TimeoutError）
                # 根因：stream stall timeout 触发时抛出 asyncio.TimeoutError，
                # 原异常处理器不捕获此类型，导致流未被正确关闭 + 异常直接传播。
                # 修复：将 asyncio.TimeoutError 纳入捕获范围，正确关闭流并走重试逻辑。
                last_error = e
                if stream:
                    with contextlib.suppress(AttributeError, OSError):
                        await stream.close()
                    stream = None
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
        # P0 修复：按 provider 上限裁剪 max_tokens（agnes 上限 65536）
        max_tokens = ModelRouter._cap_max_tokens(max_tokens, provider)
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
                                content = content + retry_content
                                logger.info("llm.truncated_retry_success",
                                            final_len=len(content), model=model,
                                            retry_round=_retry_round + 1,
                                            finish_reason=_retry_finish,
                                            derecurse=_derecurse,
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
                            key_len=len(new_cred.api_key),
                            key_hash=_mask_api_key(new_cred.api_key))
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
                    messages=messages, temperature=temperature, max_tokens=max_tokens,
                    config=config,
                )

            except (RuntimeError, OSError, KeyError, ValueError, _openai_mod.APIError) as e:
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
        config = ROUTE_TABLE.get(task_type, ROUTE_TABLE["chat"])
        model = config["model"]
        provider = config.get("client", _CFG_DEFAULT_PROVIDER)
        mt = max_tokens or config.get("max_tokens", 4096)
        timeout = self.TASK_TIMEOUTS.get(task_type, 30)

        # 应用 prompt caching（与主路由保持一致）
        messages = self._apply_prompt_caching(provider, messages)

        try:
            client = await self._select_client_for_provider(provider)
            kwargs = self._build_route_kwargs(
                model, messages, temperature, mt, False,
                None, None, None, config, provider,
            )
            response = await asyncio.wait_for(
                client.chat.completions.create(**kwargs),
                timeout=timeout,
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

        # P0 修复：移除 agnes tools_may_not_be_supported 误告警
        # 根因：agnes-2.0-flash 实际支持工具调用（日志中 tool.calls_selected 多次成功），
        #       原告警每次 route 调用都触发，造成日志噪声 + 误导排查方向。
        #       工具兼容性实际由 _is_small_model + 工具调用结果兜底，无需提前告警。
        # 如需诊断工具发送情况，查看 router.tools_sent DEBUG 日志即可。

        # 小模型不发送工具定义，防止输出退化
        if self._is_small_model(model):
            logger.warning("router.tools_stripped_for_small_model model={} tool_count={}", model, len(tools))
            return None

        return tools

    def pop_reasoning_content(self) -> str | None:
        rc = _reasoning_content_var.get("")
        _reasoning_content_var.set("")
        return rc if rc else None