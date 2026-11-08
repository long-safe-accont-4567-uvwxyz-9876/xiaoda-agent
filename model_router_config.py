"""model_router 的 provider 配置块 — 自 model_router.py 拆分（上帝文件 Phase 1）。

内容：provider 元数据加载（provider_metadata.json）、base_url / API Key 解析、
MIMO 常量、定价表、per-provider max_tokens cap、Ollama 模型名映射与翻译、
跨 provider 兜底映射。函数体自 model_router.py 逐字节搬移，仅缩进调整。

兼容契约（tests/test_model_router_config.py）：
    - 本模块不得 import model_router（防循环依赖）
    - model_router 同名 re-export，外部 `from model_router import MIMO_MODEL`
      与 `patch("model_router.MIMO_MODEL")` 用法不受影响
"""
from __future__ import annotations

import os

from loguru import logger

from config import MIMO_MODEL as _CFG_MIMO_MODEL


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
def _ollama_model_map_from_file() -> tuple[dict, str]:
    """从 provider_metadata.json 读 ollama.model_name_map + default_model。"""
    _meta = _PROVIDER_CAPS_FROM_FILE.get("ollama", {}) if isinstance(_PROVIDER_CAPS_FROM_FILE, dict) else {}
    _map: dict = {}
    _default = ""
    if isinstance(_meta, dict):
        _mm = _meta.get("model_name_map")
        if isinstance(_mm, dict):
            # 过滤以下划线开头的元字段（如 "_comment"）
            _map = {k: v for k, v in _mm.items() if not k.startswith("_")}
        _default = str(_meta.get("default_model", "") or "")
    return _map, _default


def _ollama_model_map_from_env(file_map: dict) -> dict:
    """环境变量 OLLAMA_MODEL_MAP（JSON 字典）覆盖文件映射；解析失败告警并忽略。"""
    _env_map = os.getenv("OLLAMA_MODEL_MAP", "").strip()
    if not _env_map:
        return file_map
    try:
        import json as _json
        _parsed = _json.loads(_env_map)
        if isinstance(_parsed, dict):
            return {k: v for k, v in _parsed.items() if isinstance(v, str) and v}
    except (ValueError, TypeError):
        logger.warning("router.ollama_model_map_env_invalid raw={}", _env_map)
    return file_map


def _ollama_default_model_from_env(file_default: str) -> str:
    """环境变量 OLLAMA_DEFAULT_MODEL 覆盖文件默认模型名。"""
    _env_default = os.getenv("OLLAMA_DEFAULT_MODEL", "").strip()
    return _env_default if _env_default else file_default


def _load_ollama_model_map() -> tuple[dict, str]:
    """从 provider_metadata.json + 环境变量加载 Ollama 模型名映射。"""
    _map, _default = _ollama_model_map_from_file()
    _map = _ollama_model_map_from_env(_map)
    _default = _ollama_default_model_from_env(_default)
    return _map, _default


_OLLAMA_MODEL_MAP, _OLLAMA_DEFAULT_MODEL = _load_ollama_model_map()

# 本地 ONNX Runtime GenAI chat 提供商标识：选中本地实例时，chat_stream 直接
# 经 LocalChatService 流式推理，不经过云端 OpenAI 客户端，也不做跨 provider 回退。
_LOCAL_ORT_PROVIDER = "local-ort"


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


def _resolve_provider_key(name: str) -> str:
    """统一解析 provider API Key：密文（enc:v1:）自动解密，明文原样返回。

    与 config.MIMO_API_KEY 的内存态保护链路一致：get_secret 实时解密 enc:v1: 密文，
    reveal_credential 还原明文。取代各处 os.getenv 直读（直读会把密文当 Key → 401）。
    """
    from config import get_secret
    from utils.encrypted_credential import reveal_credential
    return reveal_credential(get_secret(name, ""))


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
MIMO_API_KEY = _resolve_provider_key("MIMO_API_KEY")

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


# P0 修复：per-provider max_tokens 上限（从配置文件 + 环境变量读取，无硬编码）
# 根因：ROUTE_TABLE 中 chat/chat_agnes/chat_mimo 都设了 131072，
#       但 agnes-2.0-flash 实际上限是 65536，超过会返回 500 InternalServerError
#       "max_tokens exceeds the limit of 65536"（日志中 286 次错误根因）。
#       之前的"一刀切"提升 max_tokens 到 131072 反而打破了 agnes provider。
# 修复：在 _build_route_kwargs 中按 provider 取 min(mt, cap)。
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


def _env_max_tokens_cap(env_var: str) -> int | None:
    """从环境变量读 max_tokens cap；空/无效值返回 None（而非 0）。"""
    from utils.common import safe_int as _safe_int
    v = os.getenv(env_var)
    if v is None:
        return None
    # CodeRabbit #5 修复：空/无效值返回 None 而非 0
    # 原 implementation _safe_int(v, 0) 解析失败返回 0，被当作合法 cap
    # 导致 PROVIDER_MAX_TOKENS_CAP[p] = 0，所有请求 max_tokens=0，LLM 无法生成
    parsed = _safe_int(v, 0)
    return parsed if parsed > 0 else None


def _file_max_tokens_cap(provider: str) -> int | None:
    """从 provider_metadata.json 读 max_tokens cap；缺失/无效返回 None。"""
    meta = _PROVIDER_CAPS_FROM_FILE.get(provider, {})
    v = meta.get("max_tokens_cap") if isinstance(meta, dict) else None
    if v is None or v == 0:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _load_provider_max_tokens_cap() -> dict[str, int | None]:
    """加载各 provider 的 max_tokens 上限。

    优先级：环境变量 > provider_metadata.json > None（不裁剪）。
    未配置的 provider 返回 None（不裁剪）。
    """
    _env_var_map = {"agnes": "AGNES_MAX_TOKENS_CAP", "mimo": "MIMO_MAX_TOKENS_CAP", "ollama": "OLLAMA_MAX_TOKENS_CAP"}
    # 已知 provider 列表（从元数据文件取，找不到则空）
    _known_providers = set(_PROVIDER_CAPS_FROM_FILE.keys()) | {"agnes", "mimo", "ollama"}
    cap: dict[str, int | None] = {}
    for p in _known_providers:
        env_v = _env_max_tokens_cap(_env_var_map.get(p, f"{p.upper()}_MAX_TOKENS_CAP"))
        cap[p] = env_v if env_v is not None else _file_max_tokens_cap(p)
    return cap

PROVIDER_MAX_TOKENS_CAP: dict[str, int | None] = _load_provider_max_tokens_cap()

# 跨 provider 映射：主 provider 故障时，flash/mini 切换到不同 provider
# P0 修复：从 provider_metadata.json 的 _cross_provider_fallback 加载（不再硬编码）
def _cross_provider_map_from_file() -> dict[str, tuple[str, str]]:
    """从 provider_metadata.json 的 _cross_provider_fallback 读跨 provider 兜底映射。"""
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
    return _result


def _apply_env_cross_provider_fallback(result: dict[str, tuple[str, str]]) -> dict[str, tuple[str, str]]:
    """用环境变量推导的兜底映射补齐（用户未配置 JSON 时仍可用）。"""
    if "agnes" not in result and os.getenv("MIMO_MODEL_NAME"):
        result["agnes"] = ("mimo", os.getenv("MIMO_MODEL_NAME"))
    if "mimo" not in result and os.getenv("AGNES_TEXT_MODEL"):
        result["mimo"] = ("agnes", os.getenv("AGNES_TEXT_MODEL"))
    return result


def _load_cross_provider_map() -> dict[str, tuple[str, str]]:
    """从 provider_metadata.json 加载跨 provider 兜底映射。

    优先级：provider_metadata.json > 环境变量推导 > 空字典（不兜底）。
    """
    return _apply_env_cross_provider_fallback(_cross_provider_map_from_file())

_CROSS_PROVIDER_MAP: dict[str, tuple[str, str]] = _load_cross_provider_map()


# ── 兜底矩阵：task → provider → 同家兜底档（严重-2 + 轻微-15，2026-11-08）─────
# 旧设计只有 FALLBACK_ROUTE 一条 task 链（chat → chat_agnes），跨 provider 又被
# 硬性禁止，导致主模型不是 agnes 时没有任何「同家兜底」可走，直接掉进硅基流动
# 免费 9B 车道且不留痕迹（用户只看到「能用但变差」）。
#
# 新设计：
#   - 为声明过降级链的 task（FALLBACK_ROUTE 的 key）派生
#     matrix[task][provider] = 该 provider 的同家兜底档；
#   - 档位取值优先级：
#       1. 路由表里已存在的同家 task（如 chat + agnes → chat_agnes），
#          完全保留 agnes/agnes-2.5-flash 现网兜底行为；
#       2. provider_metadata.providers[provider].default_model
#          （deepseek → deepseek-chat、mimo → mimo-v2.5 …），
#          让每个 provider 都有自己的同家档。
#   - 跨家只在 _CROSS_PROVIDER_MAP 白名单内、且 CROSS_PROVIDER_FALLBACK_POLICY
#     显式打开时才允许（默认关闭，保持 2026-08-04「禁止自动跨家切换」约束）。
#
# 本模块是叶子模块（不得 import model_router，见 tests/test_model_router_config.py
# 契约），矩阵由 model_router 在 ROUTE_TABLE 定义后调用 set_fallback_matrix() 注入。

_TRUTHY_STRINGS = frozenset({"1", "true", "yes", "on", "y", "t"})


def _as_bool(value: object, default: bool = False) -> bool:
    """宽松真值解析（环境变量 / JSON 布尔）。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in _TRUTHY_STRINGS


def _cross_provider_policy_from_file() -> bool | None:
    """从 provider_metadata.json 的 _fallback_policy.cross_provider_enabled 取值。"""
    meta = _PROVIDER_METADATA if isinstance(_PROVIDER_METADATA, dict) else {}
    policy = meta.get("_fallback_policy")
    if isinstance(policy, dict) and "cross_provider_enabled" in policy:
        return _as_bool(policy.get("cross_provider_enabled"), False)
    return None


def load_cross_provider_fallback_policy() -> bool:
    """CROSS_PROVIDER_FALLBACK_POLICY 开关：env > provider_metadata.json > False。

    默认 False（关闭）= 保持 2026-08-04「禁止自动跨家切换」的用户硬约束。
    """
    env = os.getenv("CROSS_PROVIDER_FALLBACK_POLICY", "").strip()
    if env:
        return _as_bool(env, False)
    from_file = _cross_provider_policy_from_file()
    return from_file if from_file is not None else False


CROSS_PROVIDER_FALLBACK_POLICY: bool = load_cross_provider_fallback_policy()


def _provider_default_model(provider: str, providers: dict | None = None) -> str:
    """取某 provider 的 default_model（同家兜底档候选）。"""
    _providers = providers
    if not isinstance(_providers, dict):
        _providers = (_PROVIDER_CAPS_FROM_FILE
                      if isinstance(_PROVIDER_CAPS_FROM_FILE, dict) else {})
    meta = _providers.get(provider, {})
    if isinstance(meta, dict):
        return str(meta.get("default_model", "") or "")
    return ""


def build_fallback_matrix(route_table: dict, fallback_route: dict | None = None,
                          provider_metadata: dict | None = None) -> dict[str, dict[str, dict]]:
    """派生 task → provider → 同家兜底档 矩阵。

    只为「声明过降级链的 task」建矩阵（fallback_route 的 key，当前仅 chat），
    其余 task 继续走旧的 FALLBACK_ROUTE task 链，行为不变。

    Args:
        route_table: 路由表（model_router.ROUTE_TABLE）
        fallback_route: 降级链表（model_router_registry.FALLBACK_ROUTE）
        provider_metadata: provider 元数据（默认用本模块加载的 _PROVIDER_METADATA）

    Returns:
        {task: {provider: {"client":…, "model":…, "task":…, "source":…}}}
    """
    _route = route_table if isinstance(route_table, dict) else {}
    _chain = fallback_route if isinstance(fallback_route, dict) else {}
    meta = provider_metadata if isinstance(provider_metadata, dict) else _PROVIDER_METADATA
    providers = meta.get("providers", {}) if isinstance(meta, dict) else {}
    if not isinstance(providers, dict):
        providers = {}
    matrix: dict[str, dict[str, dict]] = {}
    for task in _chain:
        tiers: dict[str, dict] = {}
        for provider in providers:
            explicit_task = f"{task}_{provider}"
            explicit = _route.get(explicit_task)
            if isinstance(explicit, dict) and explicit.get("client") == provider:
                tier = dict(explicit)
                tier["task"] = explicit_task
                tier["source"] = "route_table"
                tiers[provider] = tier
                continue
            default_model = _provider_default_model(provider, providers)
            if not default_model:
                continue
            tiers[provider] = {
                "client": provider,
                "model": default_model,
                "task": explicit_task,
                "source": "provider_metadata",
            }
        if tiers:
            matrix[task] = tiers
    return matrix


FALLBACK_MATRIX: dict[str, dict[str, dict]] = {}


def set_fallback_matrix(matrix: dict) -> None:
    """注入兜底矩阵（由 model_router 在 ROUTE_TABLE 定义后调用）。"""
    global FALLBACK_MATRIX
    FALLBACK_MATRIX = matrix if isinstance(matrix, dict) else {}


def get_fallback_matrix() -> dict:
    """取当前兜底矩阵（只读语义；调用方不要原地改）。"""
    return FALLBACK_MATRIX


def get_fallback_tier(task: str, provider: str) -> dict | None:
    """取 task × provider 的同家兜底档；没有则 None（调用方回落到旧 task 链）。"""
    if not provider:
        return None
    tiers = FALLBACK_MATRIX.get(task)
    if not isinstance(tiers, dict):
        return None
    tier = tiers.get(provider)
    return dict(tier) if isinstance(tier, dict) else None


def get_cross_provider_tier(provider: str) -> tuple[str, str] | None:
    """取白名单 _CROSS_PROVIDER_MAP 里的跨家兜底档 (provider, model)；无则 None。"""
    entry = _CROSS_PROVIDER_MAP.get(provider) if isinstance(_CROSS_PROVIDER_MAP, dict) else None
    if not entry:
        return None
    fb_provider, fb_model = entry
    if not fb_provider or not fb_model:
        return None
    return (fb_provider, fb_model)
