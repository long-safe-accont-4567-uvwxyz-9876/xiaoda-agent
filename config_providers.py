"""config.py 的 provider 目录块 — 自 config.py 拆分（上帝文件 Phase 2）。

内容：provider_metadata.json 缓存加载、ProviderCatalog、默认模型/base_url
派生（env > metadata > 空串）、内置 provider 集合（builtin: true 派生）、
MIMO_MODEL、get_provider_config 配置映射。函数体自 config.py 逐字节搬移。

可变状态（DEFAULT_PROVIDER / set_default_provider）**保留在 config.py**：
模块级标量经 import 别名后重赋值不会同步，留在原模块保证
`config.set_default_provider()` 后所有 `from config import DEFAULT_PROVIDER`
读取方看到的都是最新值（与 ModelRouteRegistry 的 _table 身份保持同一原则）。

兼容契约（tests/test_config_providers_module.py）：
    - 本模块不得 import config（防循环依赖；仅依赖 config_paths + llm_gateway）
    - config 同名 re-export，from config import MIMO_MODEL / get_builtin_providers
      等既有用法不受影响
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from config_paths import get_config_dir

logger = logging.getLogger(__name__)

# ── 默认模型解析（从 provider_metadata.json 读，无硬编码）──
# 用户约束：默认用 MiMo，但模型 ID 不在代码里硬编码
# 优先级：环境变量 > provider_metadata.json > 空串
_PROVIDER_METADATA_CACHE: dict | None = None
_PROVIDER_CATALOG_CACHE = None


def _load_provider_metadata_cached() -> dict:
    """加载 provider_metadata.json（带缓存，避免每次调用都读盘）。

    CodeRabbit#4 + C2 修复：与 model_router._load_provider_metadata 统一查找顺序——
      1. 用户配置目录 get_config_dir()/provider_metadata.json（用户可编辑覆盖）
      2. 打包/源码 config/provider_metadata.json（内置默认值）
      3. 空字典（极端兜底）

    旧实现只读打包目录，导致用户编辑 ~/.ai-agent/config/provider_metadata.json 后
    model_router（读用户目录）与 config.get_default_model_for_provider（读打包目录）
    返回不同模型 ID，产生两套真相源，启动 fallback 时拿到错误模型。
    """
    global _PROVIDER_METADATA_CACHE
    if _PROVIDER_METADATA_CACHE is not None:
        return _PROVIDER_METADATA_CACHE
    # 1. 用户配置目录（用户可编辑覆盖）
    try:
        user_path = get_config_dir() / "provider_metadata.json"
        if user_path.exists():
            with open(user_path, "r", encoding="utf-8") as fp:
                _PROVIDER_METADATA_CACHE = json.load(fp)
                return _PROVIDER_METADATA_CACHE
    except (OSError, ValueError) as e:
        logger.warning("config.provider_metadata_user_load_failed error={}", str(e))
    # 2. 打包/源码目录（内置默认值）
    try:
        meta_path = Path(__file__).resolve().parent / "config" / "provider_metadata.json"
        if meta_path.exists():
            with open(meta_path, "r", encoding="utf-8") as fp:
                _PROVIDER_METADATA_CACHE = json.load(fp)
                return _PROVIDER_METADATA_CACHE
    except (OSError, ValueError) as e:
        logger.warning("config.provider_metadata_load_failed error={}", str(e))
    # 3. 极端兜底
    logger.error("config.provider_metadata_all_load_failed using empty dict")
    _PROVIDER_METADATA_CACHE = {}
    return _PROVIDER_METADATA_CACHE


def get_provider_catalog():
    global _PROVIDER_CATALOG_CACHE
    if _PROVIDER_CATALOG_CACHE is None:
        from llm_gateway.provider_catalog import ProviderCatalog

        user_path = get_config_dir() / "provider_metadata.json"
        bundled_path = Path(__file__).resolve().parent / "config" / "provider_metadata.json"
        _PROVIDER_CATALOG_CACHE = ProviderCatalog.from_paths(user_path, bundled_path)
        for source_path, error in _PROVIDER_CATALOG_CACHE.load_errors:
            logger.warning(f"config.provider_catalog_load_failed source={source_path} error={error}")
    return _PROVIDER_CATALOG_CACHE


def get_default_model_for_provider(provider: str) -> str:
    """返回指定 provider 的默认模型 ID。

    优先级：
      1. 环境变量 {PROVIDER}_MODEL_NAME / {PROVIDER}_TEXT_MODEL（最高）
      2. provider_metadata.json 中 providers.{provider}.default_model
      3. 空串（调用方负责兜底）

    Args:
        provider: provider 名称（如 "mimo", "agnes"）

    Returns:
        默认模型 ID 字符串，未知 provider 返回空串
    """
    provider_lower = provider.strip().lower()
    # 1. 环境变量（兼容已有的 MIMO_MODEL_NAME / AGNES_TEXT_MODEL）
    env_var_map = {
        "mimo": "MIMO_MODEL_NAME",
        "agnes": "AGNES_TEXT_MODEL",
        "deepseek": "DEEPSEEK_MODEL_NAME",
    }
    env_var = env_var_map.get(provider_lower, f"{provider_lower.upper()}_MODEL_NAME")
    env_val = os.getenv(env_var, "").strip()
    if env_val:
        return env_val
    try:
        return get_provider_catalog().get(provider_lower).default_model
    except KeyError:
        return ""


def get_base_url_for_provider(provider: str) -> str:
    """返回指定 provider 的 base_url。

    优先级：
      1. 环境变量 {PROVIDER}_BASE_URL（最高）
      2. provider_metadata.json 中 providers.{provider}.base_url_default
      3. 空串（调用方负责兜底）

    Args:
        provider: provider 名称（如 "mimo", "agnes", "deepseek"）

    Returns:
        base_url 字符串，未知 provider 返回空串
    """
    provider_lower = provider.strip().lower()
    env_val = os.getenv(f"{provider_lower.upper()}_BASE_URL", "").strip()
    if env_val:
        return env_val
    try:
        return get_provider_catalog().get(provider_lower).endpoint.base_url or ""
    except KeyError:
        return ""


# Provider base_url 统一从 provider_metadata.json 派生（环境变量优先），消除硬编码 fallback
DEEPSEEK_BASE_URL = get_base_url_for_provider("deepseek")
MIMO_BASE_URL = get_base_url_for_provider("mimo")


def get_default_provider() -> str:
    """返回默认 provider。

    优先级：
      1. 环境变量 DEFAULT_PROVIDER（最高）
      2. provider_metadata.json 的顶层 default_provider
      3. 空串（调用方负责兜底）

    默认 mimo 通过 provider_metadata.json 的 default_provider 字段表达，
    代码里不再硬编码 "mimo" 字符串。
    """
    env_val = os.getenv("DEFAULT_PROVIDER", "").strip()
    if env_val:
        return env_val.lower()
    meta = _load_provider_metadata_cached()
    return str(meta.get("default_provider", "") or "").strip().lower()


# ── 内置 Provider 集合（从 provider_metadata.json 派生，无硬编码）──
# N-2 修复：原代码多处硬编码 ("mimo", "agnes") 判断是否为内置 provider，
# 新增第三个内置 provider 时需改多处代码。改为从 metadata 的 builtin: true 字段派生。
# 带 _BUILTIN_PROVIDERS_CACHE 避免每次调用都解析 metadata。
_BUILTIN_PROVIDERS_CACHE: frozenset[str] | None = None


def get_builtin_providers() -> frozenset[str]:
    """返回内置 provider 集合（从 provider_metadata.json 的 builtin: true 字段派生）。

    内置 provider 指有内置 transport 代码支持的 provider（如 mimo/agnes），
    不需要通过 _custom_clients 注册即可使用。新增内置 provider 时，
    只需在 provider_metadata.json 标记 builtin: true，无需改代码。

    Returns:
        内置 provider 名称的 frozenset（从 metadata builtin: true 派生）
    """
    global _BUILTIN_PROVIDERS_CACHE
    if _BUILTIN_PROVIDERS_CACHE is not None:
        return _BUILTIN_PROVIDERS_CACHE
    builtin = {provider.id for provider in get_provider_catalog().list() if provider.builtin}
    _BUILTIN_PROVIDERS_CACHE = frozenset(builtin)
    return _BUILTIN_PROVIDERS_CACHE


MIMO_MODEL = get_default_model_for_provider("mimo")


# ── Provider 配置映射（base_url / api_key_env）──
# 子代理注册时根据 provider 自动选择正确的连接参数
def get_provider_config(provider: str) -> dict:
    """返回 provider 对应的 base_url 和 api_key_env。"""
    try:
        definition = get_provider_catalog().get(provider)
    except KeyError:
        return {"base_url": "", "api_key_env": ""}
    api_key_env = next(
        (alias for alias in definition.auth.environment_aliases if os.getenv(alias, "").strip()),
        definition.auth.environment_aliases[0] if definition.auth.environment_aliases else "",
    )
    base_url_env = f"{definition.id.upper()}_BASE_URL"
    return {
        "base_url": os.getenv(base_url_env, definition.endpoint.base_url),
        "api_key_env": api_key_env,
    }
