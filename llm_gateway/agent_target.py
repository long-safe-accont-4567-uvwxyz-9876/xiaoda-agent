"""子 Agent 目标三元组（provider / model / client 凭据）的统一解析。

2026-11-08 路由一致性修复（中等-5、6）：

provider、model、base_url/api_key_env 之前由 core/bootstrap.py 与
web/agent_registry.py 各自解析，缺省回退互不相同——
- bootstrap 的 model 缺省取「默认 provider 的模型」，provider 却取持久化里的那个；
- provider 解析失败时只有 base_url/api_key_env 退回默认，model 不跟着退。
两者都能拼出 provider=deepseek + model=agnes-2.5-flash + base_url=agnes 的错配。

本模块是唯一入口：三者同源解析，任一环节无法确定就整体回退到默认三元组，
并把回退原因留在返回值的 fallback_reasons 里（可观测）。只读，不写盘。
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from loguru import logger


@dataclass(frozen=True)
class AgentTarget:
    """一个子 Agent 的完整目标：provider / model / base_url / api_key_env 同源。"""

    provider: str
    model: str
    base_url: str
    api_key_env: str
    fallback_reasons: tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        return bool(self.fallback_reasons)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "fallback_reasons": list(self.fallback_reasons),
        }


def runtime_default_provider() -> str:
    """运行期读取默认 provider（不在 import 时冻结成常量）。"""
    import config as _config_mod

    return str(_config_mod.DEFAULT_PROVIDER or "").strip().lower()


def _catalog():
    from config import get_provider_catalog

    return get_provider_catalog()


def provider_in_catalog(provider: str) -> bool:
    try:
        return _catalog().get(provider) is not None
    except (KeyError, ValueError):
        return False
    except (OSError, RuntimeError, TypeError, AttributeError):
        # catalog 文件损坏 / 结构异常时按未知 provider 处理（窄捕获，便于定位）
        logger.debug("agent_target.catalog_lookup_failed provider={}", provider, exc_info=True)
        return False


def _resolve_custom_provider(provider: str) -> tuple[str, str] | None:
    """自定义 provider：从 config_service 读 base_url，并把文件里的 key 注入环境变量。"""
    try:
        from web._provider_keys import load_provider_key
        from web.config_service import get_config_service
    except ImportError:
        return None
    try:
        record = get_config_service().get(f"models.providers.{provider}")
    except (KeyError, ValueError, RuntimeError, OSError):
        return None
    if not record:
        return None
    base_url = str(record.get("base_url") or "").strip()
    if not base_url:
        return None
    try:
        key = load_provider_key(provider)
    except (KeyError, ValueError, OSError):
        key = ""
    if not key:
        return None
    env_name = f"PROVIDER_{provider.upper().replace('-', '_')}_KEY"
    os.environ[env_name] = key
    return base_url, env_name


def provider_credentials(provider: str) -> tuple[str, str] | None:
    """返回某 provider 的 (base_url, api_key_env)；未知或不可解析返回 None。"""
    provider = (provider or "").strip().lower()
    if not provider:
        return None
    if provider_in_catalog(provider):
        definition = _catalog().get(provider)
        base_url = os.getenv(f"{provider.upper()}_BASE_URL", definition.endpoint.base_url)
        aliases = list(definition.auth.environment_aliases)
        api_key_env = next(
            (alias for alias in aliases if os.getenv(alias, "").strip()),
            aliases[0] if aliases else "",
        )
        return base_url, api_key_env
    return _resolve_custom_provider(provider)


def provider_known(provider: str) -> bool:
    """provider 是否可解析（catalog 内置或 config_service 自定义）。"""
    provider = (provider or "").strip().lower()
    if not provider:
        return False
    if provider_in_catalog(provider):
        return True
    return _resolve_custom_provider(provider) is not None


def default_model_for(provider: str, fallback: str = "") -> str:
    from config import get_default_model_for_provider

    model = str(get_default_model_for_provider(provider) or "").strip()
    return model or fallback


def resolve_agent_target(
    persisted: Mapping[str, Any] | None,
    *,
    default_provider: str | None = None,
    default_model: str = "",
    source: str = "agent",
) -> AgentTarget:
    """把持久化配置解析成同源的 provider/model/base_url/api_key_env。

    回退规则（与旧实现的差别就是这些）：
    1. provider 为空 → 用默认 provider；provider 不可解析 → 整体回退默认三元组，
       模型跟着换成默认 provider 的模型，绝不留「provider 换了、model 没换」。
    2. model 为空 → 用该 provider 自己的默认模型（不是默认 provider 的模型）；
       该 provider 没有默认模型 → 整体回退默认三元组。
    3. base_url / api_key_env 只在该 provider 未发生回退时沿用持久化值，
       否则从同一 provider 重新解析，避免沿用到别的 provider 的端点。
    """
    data: Mapping[str, Any] = persisted if isinstance(persisted, Mapping) else {}
    reasons: list[str] = []
    base_default = (default_provider or "").strip().lower() or runtime_default_provider()
    base_default_model = (default_model or "").strip()

    raw_provider = str(data.get("provider") or "").strip().lower()
    provider = raw_provider or base_default
    provider_fell_back = False
    if not raw_provider:
        reasons.append("provider_empty")
    elif not provider_known(provider):
        reasons.append("provider_unregistered")
        provider = base_default
        provider_fell_back = True

    def _fallback_to_default(reason: str) -> tuple[str, str, str, str]:
        nonlocal provider
        if provider != base_default:
            reasons.append(reason)
        provider = base_default
        creds = provider_credentials(base_default) or ("", "")
        model = default_model_for(base_default, base_default_model)
        if not model:
            reasons.append("default_model_unresolved")
        return provider, model, creds[0], creds[1]

    model = str(data.get("model") or "").strip()
    if provider_fell_back and model:
        # provider 已整体回退，旧 provider 的模型名不能沿用（否则又是三元组错配）
        model = ""
        reasons.append("model_reset_with_provider")
    if not model:
        model = default_model_for(provider, base_default_model if provider == base_default else "")
        if model:
            reasons.append("model_empty")

    creds = provider_credentials(provider)
    if creds is None:
        provider, model, base_url, api_key_env = _fallback_to_default("provider_unresolvable")
    elif not model:
        provider, model, base_url, api_key_env = _fallback_to_default("model_unresolvable")
    else:
        base_url, api_key_env = creds
        persisted_base = str(data.get("base_url") or "").strip()
        persisted_env = str(data.get("api_key_env") or "").strip()
        if persisted_base and raw_provider == provider:
            base_url = persisted_base
        if persisted_env and raw_provider == provider:
            api_key_env = persisted_env

    target = AgentTarget(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        fallback_reasons=tuple(dict.fromkeys(reasons)),
    )
    if target.degraded:
        logger.warning(
            "agent_target.fallback source={} provider={} model={} reasons={}",
            source, target.provider, target.model, ",".join(target.fallback_reasons),
        )
    return target
