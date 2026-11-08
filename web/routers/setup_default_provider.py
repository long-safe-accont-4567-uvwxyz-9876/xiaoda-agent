"""首次引导页的默认 LLM provider 二选一（DeepSeek / 小米 MiMo）。

背景（2026-11-08 用户要求，前后端一并修）：
  - 默认模型从 MiMo 切到 DeepSeek；
  - 首次引导页不再写死 MiMo，改成 DeepSeek / MiMo 二选一，默认预选 DeepSeek；
  - 「不只是前端」：选择必须真正落到后端默认值——写用户配置目录的
    provider_metadata.json 的 default_provider，并同步运行时
    config.DEFAULT_PROVIDER / MODEL_NAME 与 ConfigService models.chat_model。

单一事实源：provider_metadata.json（用户目录优先、仓库 config/ 兜底）。
本模块只做「校验选择 + 落盘 + 同步运行时」，base_url / protocol / chat_path /
models_path 全部来自 catalog，不在此复制任何 provider 元数据。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from loguru import logger

SUPPORTED_DEFAULT_PROVIDERS: tuple[str, ...] = ("deepseek", "mimo")
DEFAULT_PROVIDER_CHOICE = "deepseek"
PROVIDER_KEY_ENV: dict[str, str] = {"deepseek": "DEEPSEEK_API_KEY", "mimo": "MIMO_API_KEY"}
PROVIDER_LABEL: dict[str, str] = {"deepseek": "DeepSeek", "mimo": "小米 MiMo"}


def describe_provider_choices() -> list[dict[str, Any]]:
    """给引导页渲染的两个选项（顺序即展示顺序，DeepSeek 在前）。"""
    return [
        {
            "id": pid,
            "label": PROVIDER_LABEL[pid],
            "key_env": PROVIDER_KEY_ENV[pid],
            "default": pid == DEFAULT_PROVIDER_CHOICE,
        }
        for pid in SUPPORTED_DEFAULT_PROVIDERS
    ]


def get_current_default_provider() -> str:
    """当前生效的默认 provider（环境变量 > 用户配置 > 仓库默认）。"""
    try:
        from config_providers import get_default_provider

        return get_default_provider() or DEFAULT_PROVIDER_CHOICE
    except (ImportError, RuntimeError):
        return DEFAULT_PROVIDER_CHOICE


def normalize_provider_choice(raw: Any) -> str:
    """把请求体里的 provider 规整为受支持的 id；空值表示「不改」。"""
    val = str(raw or "").strip().lower()
    if not val:
        return ""
    if val not in SUPPORTED_DEFAULT_PROVIDERS:
        raise ValueError(
            f"不支持的默认模型: {val}（只允许 {' / '.join(SUPPORTED_DEFAULT_PROVIDERS)}）"
        )
    return val


def _has_key(provider: str, updates: dict | None = None) -> bool:
    env_name = PROVIDER_KEY_ENV.get(provider, "")
    if not env_name:
        return False
    if updates and str(updates.get(env_name, "") or "").strip():
        return True
    return bool(os.getenv(env_name, "").strip())


def check_choice(provider: str, updates: dict | None = None) -> str:
    """返回空串表示通过；否则返回可直接展示给用户的中文原因。"""
    if not provider or provider not in SUPPORTED_DEFAULT_PROVIDERS:
        return ""
    if not _has_key(provider, updates):
        return (
            f"选择 {PROVIDER_LABEL[provider]} 作为默认模型需要先填写 "
            f"{PROVIDER_KEY_ENV[provider]}，本次未保存默认模型"
        )
    return ""


def _load_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def user_metadata_path() -> Path:
    from config_paths import get_config_dir

    return Path(get_config_dir()) / "provider_metadata.json"


def bundled_metadata_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "provider_metadata.json"


def persist_default_provider(provider: str) -> Path:
    """把 default_provider 写进用户配置目录的 provider_metadata.json。

    只改这一个顶层字段，其余内容按「用户文件 > 仓库内置」合并保留。
    """
    target = user_metadata_path()
    merged = _load_json(target) or _load_json(bundled_metadata_path())
    merged["default_provider"] = provider
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(merged, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    os.replace(tmp, target)
    return target


def clear_catalog_caches() -> None:
    """改完 metadata 后让 catalog/派生缓存失效，避免运行时还看到旧默认值。"""
    try:
        import config_providers as cp

        cp._PROVIDER_METADATA_CACHE = None
        cp._PROVIDER_CATALOG_CACHE = None
        cp._BUILTIN_PROVIDERS_CACHE = None
    except (ImportError, AttributeError) as e:
        logger.warning("setup.default_provider.cache_reset_failed error={}", str(e))


def apply_runtime_default_provider(provider: str) -> str:
    """同步运行时默认值，返回该 provider 的默认模型 ID。"""
    import config

    model_id = ""
    try:
        from config_providers import get_default_model_for_provider

        model_id = get_default_model_for_provider(provider) or ""
    except (ImportError, RuntimeError) as e:
        logger.warning("setup.default_provider.model_lookup_failed error={}", str(e))
    config.set_default_provider(provider)
    if model_id:
        config.MODEL_NAME = model_id
    try:
        from core_runtime.config_service import get_config_service

        get_config_service().set("models.chat_model", {"provider": provider, "model_id": model_id})
    except (ImportError, RuntimeError, ValueError) as e:
        logger.warning("setup.default_provider.config_service_failed error={}", str(e))
    logger.info("setup.default_provider_applied provider={} model={}", provider, model_id)
    return model_id


def _choice_from(body: dict | None) -> str:
    if not isinstance(body, dict):
        return ""
    return normalize_provider_choice(body.get("default_provider"))


def validate_default_provider_choice(body: dict | None, updates: dict | None = None) -> Any:
    """保存前校验：选择需要对应 Key。返回错误 Envelope 或 None。"""
    from web.schemas import Envelope

    try:
        provider = _choice_from(body)
    except ValueError as e:
        return Envelope(ok=False, error={"code": "INVALID_PROVIDER", "message": str(e)})
    reason = check_choice(provider, updates)
    if reason:
        return Envelope(ok=False, error={"code": "MISSING_PROVIDER_KEY", "message": reason})
    return None


def apply_default_provider_choice(body: dict | None, updates: dict | None = None) -> str:
    """保存后落地：写用户配置 + 同步运行时。返回生效的 provider（空串=未变更）。"""
    try:
        provider = _choice_from(body)
    except ValueError as e:
        logger.warning("setup.default_provider.invalid_choice error={}", str(e))
        return ""
    if not provider:
        return ""
    if check_choice(provider, updates):
        return ""
    try:
        persist_default_provider(provider)
    except (OSError, ValueError, RuntimeError) as e:
        logger.error("setup.default_provider.persist_failed provider={} error={}", provider, str(e))
        return ""
    clear_catalog_caches()
    apply_runtime_default_provider(provider)
    return provider
