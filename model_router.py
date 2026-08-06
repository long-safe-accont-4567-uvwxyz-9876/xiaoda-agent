from typing import Any, ClassVar
from collections.abc import AsyncIterator
import copy
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
from config import MODEL_NAME as _CFG_MODEL_NAME
from config import FLASH_MODEL_NAME as _CFG_FLASH_MODEL, DEFAULT_PROVIDER as _CFG_DEFAULT_PROVIDER
from config import MIMO_MODEL as _CFG_MIMO_MODEL
from config import set_default_provider as _set_default_provider
from config import get_builtin_providers as _get_builtin_providers
from transports import ProviderTransport, MiMoTransport, AgnesTransport
# 根因修复：agnes API connect=5s 过短导致 APIConnectionError，统一从 agnes_transport 引入共享 httpx 配置
from transports.agnes_transport import _get_agnes_http_client, AGNES_HTTP_TIMEOUT, close_agnes_shared_client
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


# ── Ollama 模型名映射（真实代理转发本地 Ollama 的配套翻译机制） ──
# 背景：路由会把工作流/云平台（如硅基流动）写死的模型名（如 "deepseek-ai/DeepSeek-V3-0324"）
# 直接透传给 provider。本地 Ollama 只有用户 own 的模型，云模型名不存在会报 "model not found"。
# 因此在把请求真正转发给本地 Ollama（OLLAMA_BASE_URL，如 http://localhost:11434/v1）之前，
# 先做"翻译（映射）"：把云模型名映射为用户本地实际 pull 的模型名。
# 配置来源（无硬编码）：config/provider_metadata.json 的 ollama.model_name_map + ollama.default_model，
# 用户可编辑，也可用环境变量覆盖（见下方 _OLLAMA_MODEL_MAP_OVERRIDE）。
def _load_ollama_model_map() -> tuple[dict, str]:
    """从 provider_metadata.json + 环境变量加载 Ollama 模型名映射。"""
    _meta = _PROVIDER_CAPS_FROM_FILE.get("ollama", {}) if isinstance(_PROVIDER_CAPS_FROM_FILE, dict) else {}
    _map: dict = {}
    _default = ""
    if isinstance(_meta, dict):
        _mm = _meta.get("model_name_map")
        if isinstance(_mm, dict):
            # 过滤以下划线开头的元字段（如 "_comment"）
            _map = {k: v for k, v in _mm.items() if not k.startswith("_")}
        _default = str(_meta.get("default_model", "") or "")
    # 环境变量覆盖：OLLAMA_MODEL_MAP 为 JSON 字典，OLLAMA_DEFAULT_MODEL 为单个模型名
    _env_map = os.getenv("OLLAMA_MODEL_MAP", "").strip()
    if _env_map:
        try:
            import json as _json
            _parsed = _json.loads(_env_map)
            if isinstance(_parsed, dict):
                _map = {k: v for k, v in _parsed.items() if isinstance(v, str) and v}
        except (ValueError, TypeError):
            logger.warning("router.ollama_model_map_env_invalid raw={}", _env_map)
    _env_default = os.getenv("OLLAMA_DEFAULT_MODEL", "").strip()
    if _env_default:
        _default = _env_default
    return _map, _default


_OLLAMA_MODEL_MAP, _OLLAMA_DEFAULT_MODEL = _load_ollama_model_map()


def translate_model_for_provider(provider: str, model: str) -> str:
    """把工作流/云平台模型名翻译为该 provider 实际使用的模型名。

    仅对 Ollama 生效（本地模型名与云平台模型名不一致时会报 "model not found"）：
      1. 精确命中 model_name_map → 用映射后的本地模型名
      2. 形如 "org/model" 的云模型名（含 "/"，非本地模型）→ 用用户配置的 default_model
      3. 其余原样透传（用户可直接填本地模型名，如 "qwen2.5"）
    其他 provider 一律原样返回，不做任何改动。
    """
    if provider != "ollama":
        return model
    if not model:
        return _OLLAMA_DEFAULT_MODEL or model
    _mapped = _OLLAMA_MODEL_MAP.get(model)
    if _mapped:
        if _mapped != model:
            logger.debug("router.ollama_model_mapped from={} to={}", model, _mapped)
        return _mapped
    if "/" in model and _OLLAMA_DEFAULT_MODEL:
        # 云平台风格模型名（含组织/前缀，如 "deepseek-ai/DeepSeek-V3-0324"）通常不是本地模型
        logger.info("router.ollama_model_fallback from={} to={}", model, _OLLAMA_DEFAULT_MODEL)
        return _OLLAMA_DEFAULT_MODEL
    return model


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


# MIMO_MODEL/MIMO_PRO_MODEL 从 config.py + provider_metadata.json 读取（不再硬编码）
# 保留模块级变量名以兼容 `from model_router import MIMO_MODEL` 的调用方
MIMO_MODEL = _CFG_MIMO_MODEL
# MIMO_PRO_MODEL：环境变量优先，否则从 provider_metadata.json 的 mimo.default_pro_model 读
_mimo_meta = _PROVIDER_CAPS_FROM_FILE.get("mimo", {}) if isinstance(_PROVIDER_CAPS_FROM_FILE, dict) else {}
MIMO_PRO_MODEL = os.getenv("MIMO_PRO_MODEL_NAME", "")
if not MIMO_PRO_MODEL and isinstance(_mimo_meta, dict):
    MIMO_PRO_MODEL = _mimo_meta.get("default_pro_model", "")
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
    "emotion_analysis": {"model": _CFG_FLASH_MODEL or _CFG_MODEL_NAME, "max_tokens": 1024, "client": _CFG_DEFAULT_PROVIDER, "thinking": {"type": "disabled"}},
    "tool_result_wrap": {"model": _CFG_FLASH_MODEL or _CFG_MODEL_NAME, "max_tokens": 2048, "client": _CFG_DEFAULT_PROVIDER, "thinking": {"type": "disabled"}},
    "memory_encoding": {"model": _CFG_FLASH_MODEL or _CFG_MODEL_NAME, "max_tokens": 4096, "client": _CFG_DEFAULT_PROVIDER, "thinking": {"type": "disabled"}},
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
# chat_pro/chat_flash 已合并进 chat（同一 provider 同一 model，无区分意义）
# 降级链：chat 失败 → chat_agnes（agnes provider 作为独立兜底）
FALLBACK_ROUTE = {
    "chat": "chat_agnes",
}

# P0 修复：per-provider max_tokens 上限（从配置文件 + 环境变量读取，无硬编码）
# 根因：ROUTE_TABLE 中 chat/chat_agnes/chat_mimo 都设了 131072，
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
#     - agnes-2.0-flash: 65536（https://docs.agnes-ai.cn/，超过返回 500）
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


def _ssrf_check(url: str) -> None:
    """SSRF 防护：5步法校验 base_url 安全性（best-effort，本地 provider 如 Ollama 校验失败仅告警不阻塞）"""
    try:
        ok, reason = _ssrf_validate_url(url)
        if not ok:
            logger.warning("router.ssrf_blocked url={} reason={}", url, reason)
    except (ValueError, OSError) as e:
        logger.debug("router.ssrf_check_skip url={} error={}", url, str(e))


class ModelRouteRegistry:
    """路由表注册中心：ROUTE_TABLE 的唯一读写入口。

    设计原则：
    - 启动后 _table 是只读快照，所有修改必须走 update_route()
    - update_route() 是原子操作：构造新 entry → 写内存 → 持久化 → 失败回滚
    - get_task/snapshot_task 返回深拷贝，防引用污染
    - replace_table 用于启动时一次性填充（不持久化，由调用方负责）

    这样保证：
    1. 用户改过的配置不会被降级链/fallback 路径覆盖
    2. 持久化失败不留半成品状态
    3. 降级链读取的是独立快照，修改不影响全局
    """

    def __init__(self, initial_table: dict | None = None,
                 config_service: Any = None) -> None:
        # 直接引用传入的 table（不深拷贝）：
        # 生产中传入 ROUTE_TABLE，registry._table 就是 ROUTE_TABLE 本身，
        # update_route 修改 self._table[task] 即修改 ROUTE_TABLE[task]，
        # 保证 route() 读 ROUTE_TABLE 时拿到最新值（避免 registry 与 ROUTE_TABLE 脱节）。
        # 测试中传入局部 dict，修改不影响全局；如需隔离，调用方自行深拷贝后传入。
        self._table: dict[str, dict] = initial_table if initial_table is not None else {}
        # 延迟加载 ConfigService：测试时可注入 mock，生产时从 get_config_service() 取
        self._cfg = config_service

    def _get_cfg(self) -> Any:
        """延迟获取 ConfigService 实例（避免循环导入）。"""
        if self._cfg is not None:
            return self._cfg
        try:
            from web.config_service import get_config_service
            self._cfg = get_config_service()
        except (ImportError, RuntimeError) as e:
            logger.warning("registry.config_service_unavailable error={}", str(e))
            self._cfg = None
        return self._cfg

    def get_task(self, task: str) -> dict | None:
        """返回指定 task 路由的深拷贝（调用方修改不影响内部状态）。"""
        entry = self._table.get(task)
        return copy.deepcopy(entry) if entry is not None else None

    def snapshot_task(self, task: str) -> dict | None:
        """同 get_task，语义上表示"用于构造 fallback 的快照"。"""
        return self.get_task(task)

    def all_tasks(self) -> list[str]:
        """返回所有 task 名称。"""
        return list(self._table.keys())

    def replace_table(self, new_table: dict) -> None:
        """启动时一次性替换整个表（不触发持久化）。

        用于 _apply_route_overrides：从 ConfigService 加载用户配置后，
        用持久化值覆盖默认 ROUTE_TABLE。持久化由调用方决定（启动时一般不写回）。

        CodeRabbit#5 修复：保持 self._table 的对象身份不变（clear + update），
        而非重新赋值 self._table = new_dict。生产中 self._table 就是模块级
        ROUTE_TABLE 本身，route()/chat_stream() 等请求路径仍直接读 ROUTE_TABLE；
        若 here 重新赋值，registry 后续 update_route 作用于新 dict，而请求路径
        还在读旧 ROUTE_TABLE → 数据脱节，用户改的配置对在途请求不可见。
        """
        self._table.clear()
        self._table.update(copy.deepcopy(new_table))

    def get_task_ref(self, task: str) -> dict | None:
        """返回指定 task 路由的**引用**（不深拷贝）。

        供热路径 route()/chat_stream()/get_max_tokens_for_task 使用，避免每次
        调用都深拷贝（深拷贝虽是微秒级，但 chat 热路径每秒可能数十次调用）。
        调用方承诺只读不改返回的 dict；修改请走 update_route()。
        """
        return self._table.get(task)

    def update_route(self, task: str, model_id: str, provider: str,
                     max_tokens: int | None = None,
                     thinking: dict | None = None,
                     timeout: int | None = None,
                     persist: bool = True) -> dict:
        """原子地更新路由：内存 + 持久化。

        Args:
            task: 路由 task 名称（如 "chat"）
            model_id: 模型 ID
            provider: provider 名称
            max_tokens: 可选，max_tokens 上限
            thinking: 可选，{"type": "enabled"|"disabled", "budget_tokens": ...}
            timeout: 可选，超时秒数
            persist: 是否持久化到 ConfigService（启动恢复时设为 False）

        Returns:
            新的路由 entry（深拷贝）

        Raises:
            KeyError: task 不存在
            RuntimeError: 持久化失败（内存已回滚）
        """
        if task not in self._table:
            raise KeyError(f"未知路由 task: {task}")

        # 保留旧值用于回滚
        old_entry = copy.deepcopy(self._table[task])

        # 构造新 entry
        new_entry = copy.deepcopy(old_entry)
        new_entry["model"] = model_id
        new_entry["client"] = provider
        if max_tokens is not None:
            new_entry["max_tokens"] = max_tokens
        if thinking is not None:
            new_entry["thinking"] = copy.deepcopy(thinking)
        # CodeRabbit#7 修复：timeout 也合并进 new_entry，
        # 这样持久化时 new_entry.get("timeout") 能拿到有效值
        if timeout is not None:
            new_entry["timeout"] = timeout

        # 写内存
        self._table[task] = new_entry

        # 持久化（失败回滚）
        if persist:
            cfg = self._get_cfg()
            if cfg is not None:
                try:
                    # CodeRabbit#3+#9 + Qodo#3 修复：持久化用 new_entry 的有效值。
                    # new_entry 已在上方合并了 old_entry（max_tokens/thinking/timeout 继承自旧值），
                    # 所以这里直接序列化 new_entry 即可，不会再把 omitted 误存为 false/60。
                    _effective_thinking = new_entry.get("thinking")
                    _thinking_bool = bool(
                        _effective_thinking and isinstance(_effective_thinking, dict)
                        and _effective_thinking.get("type") == "enabled"
                    )
                    # timeout：new_entry 已继承 old_entry.timeout；若 new_entry 无则用入参兜底
                    _effective_timeout = new_entry.get("timeout")
                    if _effective_timeout is None and timeout is not None:
                        _effective_timeout = timeout
                    cfg.set(f"models.routes.{task}", {
                        "model": new_entry["model"],
                        "client": new_entry["client"],
                        "max_tokens": new_entry.get("max_tokens"),
                        "thinking": _thinking_bool,
                        "timeout": _effective_timeout,
                    })
                except Exception as e:
                    # 回滚内存
                    self._table[task] = old_entry
                    logger.error("registry.update_route_persist_failed task={} error={}",
                                 task, str(e))
                    raise RuntimeError(f"持久化路由 {task} 失败: {e}") from e

        return copy.deepcopy(new_entry)


class ModelRouter:
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
        self._agnes_client = (
            AsyncOpenAI(
                api_key=_agnes_key,
                base_url=_agnes_url,
                http_client=_get_agnes_http_client(),
                timeout=AGNES_HTTP_TIMEOUT,
                max_retries=0,
            ) if _agnes_key else None
        )

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
        # 改为 per-provider 锁（复用 _get_credential_lock），agnes 调用之间串行
        # （agnes 不支持并发），但不阻塞其他 provider。详见 _route_with_retry。
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
        old_mimo = self._client  # 旧 MiMo 客户端（独立 httpx，替换后 close 释放连接）

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
            self._agnes_client = AsyncOpenAI(
                api_key=new_agnes_key,
                base_url=new_agnes_url,
                http_client=_get_agnes_http_client(),
                timeout=AGNES_HTTP_TIMEOUT,
                max_retries=0,
            )
            logger.info("router.agnes_client_refreshed",
                        key_len=len(new_agnes_key),
                        key_hash=_mask_api_key(new_agnes_key))
        else:
            self._agnes_client = None

        # 关闭旧客户端释放连接
        # CodeRabbit 修复：old_agnes 注入了共享 httpx client（_get_agnes_http_client），
        # 调用其 .close() 会连带关闭共享 client，影响新 self._agnes_client（同样复用共享
        # client）。共享 client 生命周期归 agnes_transport 模块统一管理（close_agnes_shared_client），
        # 这里只关闭独立的 old_mimo（未注入共享 httpx，SDK 自建 client）。
        _old_clients: list = []
        if old_mimo is not None and old_mimo is not self._client:
            _old_clients.append(old_mimo)
        if _old_clients:
            try:
                import asyncio
                loop = asyncio.get_running_loop()

                async def _close_old() -> None:
                    await asyncio.gather(
                        *[c.close() for c in _old_clients],
                        return_exceptions=True,
                    )

                # 同类副作用修复：裸 create_task 无强引用会被 GC 回收导致
                # 旧客户端未关闭（连接泄漏）。用 _spawn 跟踪。
                from core.background_tasks import _spawn
                _spawn(_close_old())
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
        if provider not in _get_builtin_providers():
            if provider not in self._custom_clients:
                self._lazy_register_provider(provider)
            if provider not in self._custom_clients:
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
        """关闭所有 AsyncOpenAI 客户端, 释放 TCP 连接.

        CodeRabbit 修复：注入共享 httpx client 的 agnes wrapper 不调用 ``.close()`` ——
        ``.close()`` 会连带关闭共享 httpx client，影响其他复用该 client 的实例。
        共享 agnes client 由 ``close_agnes_shared_client()`` 统一关闭；MiMo 与非 agnes
        自定义 provider 客户端（未注入共享 httpx，SDK 自建 client）独立 close。
        """
        if self._client is not None:
            try:
                await self._client.close()
            except (RuntimeError, OSError, _openai_mod.APIError):
                logger.debug("model_router.close_client_error", exc_info=True)
        self._client = None
        # 关闭自定义 provider 客户端（跳过 agnes：它复用共享 httpx client，由下方统一关闭）
        if hasattr(self, "_custom_clients"):
            for cp_name, cp_client in list(self._custom_clients.items()):
                if cp_name == "agnes":
                    continue
                try:
                    await cp_client.close()
                except (RuntimeError, OSError, _openai_mod.APIError):
                    logger.debug("model_router.close_custom_client_error", exc_info=True)
            self._custom_clients.clear()
        # agnes 共享 httpx client：统一关闭一次（应用退出时调用，此时无在途请求）
        try:
            await close_agnes_shared_client()
        except (RuntimeError, OSError):
            logger.debug("model_router.close_agnes_shared_client_error", exc_info=True)
        self._agnes_client = None

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
                    or "agnes" in getattr(self, "_custom_clients", {}))
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
        # 治本修复（2026-08-05 用户"治标不治本"反馈）：timeout 错误跳过整个 fallback 链。
        # 根因：agnes-2.0-flash 服务端强制 thinking，正常响应 6-7s（实测铁证）。
        #   read timeout 触发后，fallback 链会再调同 provider 的 agnes（不同 task_type），
        #   agnes 慢时 fallback 也慢 → 8s+8s=16s 双倍延迟（日志 llm_verify=9012ms 铁证）。
        #   timeout 意味着服务端慢，同 provider fallback 再调一次必然也慢，纯叠加延迟。
        # 修复：timeout 错误直接返回 None，不执行 fallback，由上层降级返回提示。
        #   避免双倍等待，最坏单次 timeout 即降级，而非 timeout×2。
        try:
            _classified_for_fb = self._error_classifier.classify(e)
            if _classified_for_fb.reason.value == "timeout":
                logger.warning("router.fallback_skip_timeout",
                               task=task_type,
                               reason="timeout: same provider fallback would double latency",
                               error=f"{type(e).__name__}: {e}")
                return None
        except Exception:
            pass

        # 1. 降级到更便宜的模型
        fallback_type = FALLBACK_ROUTE.get(task_type)
        # P0 修复：content_filter 触发时跳过同 provider 的 fallback 目标
        # 根因：同 provider 的 fallback 目标会再次触发 content_filter
        # 浪费一次调用 + 触发 verification retry，导致 14 秒延迟
        # 修复：content_filter 时跳过同 provider 的 fallback，直接到不同 provider（如 agnes）
        _is_content_filter = "content_filter" in str(e) or "content_policy" in str(e)
        _original_provider = ""
        try:
            # Task 6: 通过 registry 快照读取，避免降级链污染全局 ROUTE_TABLE
            _orig_entry = self._registry.get_task(task_type) or {}
            _original_provider = _orig_entry.get("client", _CFG_DEFAULT_PROVIDER)
        except Exception:
            pass
        # 用户硬约束（2026-08-04）：禁止自动切换模型/provider。
        # 用户在 WebUI 切换到哪个 provider，就一直用该 provider，失败也不跨 provider 兜底。
        # 同 provider 内的重试（_route_with_retry）保留，不算"切换"。
        # 跨 provider fallback 只会叠加延迟（再调一次别的 API）并违背用户意图，故全部跳过。
        while fallback_type:
            # Task 6: 用 registry 快照（深拷贝），降级期间修改不影响全局 ROUTE_TABLE
            fallback_config = self._registry.snapshot_task(fallback_type)
            fallback_provider = fallback_config.get("client", _CFG_DEFAULT_PROVIDER) if fallback_config else _CFG_DEFAULT_PROVIDER
            # 禁止跨 provider 切换：fallback 目标 provider 必须与原 provider 一致
            if fallback_provider != _original_provider:
                logger.info("router.fallback_skip_cross_provider",
                            original_task=task_type, fallback_task=fallback_type,
                            original_provider=_original_provider,
                            fallback_provider=fallback_provider,
                            reason="user_disabled_cross_provider_fallback")
                fallback_type = FALLBACK_ROUTE.get(fallback_type)
                continue
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
            except (RuntimeError, OSError, KeyError, ValueError, LLMError) as fb_err:
                logger.error("router.fallback_failed",
                             fallback_task=fallback_type,
                             error=f"{type(fb_err).__name__}: {fb_err}")

        # 2. 尝试 Agnes 作为最终降级
        # 用户硬约束：禁止跨 provider 切换。仅当原 provider 本就是 agnes 时才允许
        # 走 agnes 内部的 chat_agnes task（同 provider，不算切换）。
        if _original_provider == "agnes" and task_type not in ("chat_agnes",) and self._is_client_configured("agnes"):
            try:
                # Task 6: 用 registry 快照读取 chat_agnes，避免污染全局
                agnes_config = self._registry.snapshot_task("chat_agnes")
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
            except (RuntimeError, OSError, KeyError, ValueError, LLMError) as agnes_err:
                logger.error("router.agnes_fallback_failed", error=str(agnes_err))

        # 3. 尝试已注册的自定义 provider（SiliconFlow/OpenRouter/ModelScope 等）
        # 用户硬约束：禁止跨 provider 切换。仅当原 provider 本就是该自定义 provider 时才执行。
        if task_type.startswith("chat") and self._custom_clients:
            for cp_name, _cp_client in list(self._custom_clients.items()):
                # 跨 provider 切换一律跳过
                if cp_name != _original_provider:
                    continue
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
                except (RuntimeError, OSError, KeyError, ValueError, LLMError) as cp_err:
                    # CR-Major-2 修复：补 LLMError 捕获。
                    # _route_with_retry 内部 _select_client_for_provider 在 client 未初始化时
                    # 抛 LLMError（继承 AppException，不属于 RuntimeError/OSError/ValueError），
                    # 原捕获集合漏掉它 → 自定义 provider fallback 链提前终止，异常逃逸到 route()。
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
        # CodeRabbit#5 + M5 修复：走 registry.get_task_ref 而非直接读 ROUTE_TABLE。
        # get_task_ref 返回引用（不深拷贝），供热路径使用；replace_table 已保持对象身份，
        # 所以 registry._table 与 ROUTE_TABLE 永远是同一对象，读哪个都行，
        # 但走 registry 是语义上的唯一入口，未来若 registry 改实现也不用改 route()。
        config = self._registry.get_task_ref(task_type) or self._registry.get_task_ref("chat") or {}
        mt = max_tokens or config.get("max_tokens", 4096)
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
                    # 优先检查 _custom_clients["agnes"]（用户通过 WebUI 注册的 agnes 客户端）
                    # 根因：旧实现直接走 env var 懒恢复，会绕过 _custom_clients["agnes"]
                    # 导致用户通过 WebUI 添加 agnes 后，调用仍走 env var 创建的新客户端，
                    # 而非用户注册的客户端（用户配置的 base_url/api_key 不生效）。
                    _custom_agnes = getattr(self, "_custom_clients", {}).get("agnes")
                    if _custom_agnes is not None:
                        client = _custom_agnes
                    else:
                        _agnes_key = os.getenv("AGNES_API_KEY", "")
                        _agnes_url = os.getenv("AGNES_BASE_URL", AGNES_BASE_URL)
                        if _agnes_key:
                            try:
                                _ssrf_check(_agnes_url)
                                self._agnes_client = AsyncOpenAI(
                                    api_key=_agnes_key,
                                    base_url=_agnes_url,
                                    http_client=_get_agnes_http_client(),
                                    timeout=AGNES_HTTP_TIMEOUT,
                                    max_retries=0,
                                )
                                client = self._agnes_client
                                logger.info("router.agnes_client_lazy_recovered",
                                            key_hash=_mask_api_key(_agnes_key))
                            except (ValueError, OSError) as ce:
                                logger.warning("router.agnes_client_lazy_recover_failed",
                                               error=str(ce))
            # N-2 修复收尾：内置 provider 集合从 provider_metadata.json 派生，不硬编码
            # （line 20 已 import _get_builtin_providers，line 686/1319 同款用法）
            elif provider not in _get_builtin_providers():
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
            _chat_cfg = self._registry.get_task_ref("chat") or {}
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
                            if _p in _get_builtin_providers() or _p in getattr(self, "_custom_clients", {}):
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
        # Ollama 模型名翻译：把工作流/云模型名映射为本地实际模型名（真实代理核心配套）
        _send_model = translate_model_for_provider(provider, model)
        kwargs = {
            "model": _send_model,
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
        # CR-Major-1: 在循环外初始化，except 分支才能安全引用（stall timeout 日志需要）
        _stall_timeout = float(os.getenv("LLM_STREAM_STALL_TIMEOUT", "15"))
        _chunk_count = 0
        _stream_usage: Any = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                client = await self._select_client_for_provider(provider)
                kwargs = self._build_route_kwargs(
                    model, messages, temperature, mt, True,
                    tools, tool_choice, extra_headers, config, provider,
                )
                # CR-Major-1 修复：stream_options include_usage，让 provider 在最后一个
                # chunk 返回 usage，供 _record_stream_usage 记录费用。
                # 不加此参数时流式调用 usage 为 None，费用统计漏算（用户反馈"流式调用
                # 不计费"根因）。OpenAI 兼容端点均支持，不支持时 provider 忽略此字段。
                kwargs["stream_options"] = {"include_usage": True}
                # per-provider 锁：agnes 不支持并发，create + stream 消费期间持锁，
                # 保证同 provider 的流式调用串行；不同 provider 之间不互斥。
                # 替代已删除的全局 _llm_call_gate（全局锁会阻塞所有 provider）。
                async with self._get_credential_lock(provider):
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
            pass
        if fp:
            kwargs["frequency_penalty"] = fp
        # 论文验证有效值为 1.2，对条件模式重复有效；对结构化重复效果有限但无副作用
        pp = config.get("presence_penalty", 1.0)
        # 优先 WebUI 全局设置（models.presence_penalty），回退模型配置
        try:
            from config import get_presence_penalty
            pp = get_presence_penalty(default=pp)
        except Exception:
            pass
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
                # agnes 复用共享 httpx client + connect=15s 配置（根因修复）；
                # mimo 保持默认（不在本次 APIConnectionError 根因范围）
                _new_base = new_cred.base_url or (MIMO_BASE_URL if provider == "mimo" else AGNES_BASE_URL)
                if provider == "agnes":
                    new_client = AsyncOpenAI(
                        api_key=new_cred.api_key,
                        base_url=_new_base,
                        http_client=_get_agnes_http_client(),
                        timeout=AGNES_HTTP_TIMEOUT,
                        max_retries=0,
                    )
                else:
                    new_client = AsyncOpenAI(
                        api_key=new_cred.api_key,
                        base_url=_new_base,
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

                # per-provider 锁：agnes 不支持并发，同 provider 的 create 串行；
                # 不同 provider 之间不互斥（替代已删除的全局 _llm_call_gate）。
                async with self._get_credential_lock(provider):
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
            # per-provider 锁：agnes 不支持并发，同 provider create 串行
            async with self._get_credential_lock(provider):
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
