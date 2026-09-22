"""从 .env 注册「已知免费模型平台」provider。

拆分动机（巨型文件止血液轮）：本块逻辑原在 web/server.py 内联，属启动期
provider 装配职责，与 server 的 FastAPI 装配/生命周期无关。抽到独立模块后
server.py 不再因 provider 注册细节而增长。

单一事实源：id / base_url / label / env 别名全部来自
``config.get_provider_catalog()``（即 config/provider_metadata.json）。
本模块只叠加两件事：
  1. 注册顺序（_ENV_PROVIDER_ORDER）
  2. 「本地 URL 型 provider 走 *_BASE_URL」的 url_keyed 策略

注意：Jev（TypeSafe System One 决策模型）不是 LLM，不经 provider catalog
注册，不参与 LLM 路由/降级，因此不在本清单中。
"""
from __future__ import annotations

from typing import Any

from loguru import logger

# 本地 URL 型 provider：无 Key 接口，由 *_BASE_URL 环境变量显式驱动注册。
# 展示顺序（本模块的排序策略；字段值一律以 catalog 为单一事实源）。
# 不含/含哪些 provider 由 catalog 派生（auth.required=false 即本地 URL 型）。
_ENV_PROVIDER_ORDER = ("siliconflow", "openrouter", "modelscope", "agnes", "ollama", "llama.cpp")


def _derive_known_env_providers(env_values: Any) -> list[dict[str, Any]]:
    """从 provider catalog 派生「.env 已知免费平台」注册表。

    单一事实源：id/base_url/label/env 别名全部来自 config.get_provider_catalog()，
    本函数只叠加注册顺序与 url_keyed 策略。catalog 加载失败（元数据 JSON 缺失，
    降级为空 catalog）时返回空列表，调用方跳过注册但不炸。
    """
    from config import get_provider_catalog
    from config_providers import get_provider_env_prefix, get_provider_label
    from llm_gateway.contracts import ProviderProtocol

    catalog = get_provider_catalog()
    derived: list[dict[str, Any]] = []
    for pid in _ENV_PROVIDER_ORDER:
        try:
            definition = catalog.get(pid)
        except KeyError:
            continue
        aliases = definition.auth.environment_aliases
        env_prefix = get_provider_env_prefix(pid)
        if not definition.auth.required:
            env_key = f"{env_prefix}_BASE_URL"
        else:
            try:
                resolved = catalog.resolve_environment_alias(pid, env_values)
            except KeyError:
                resolved = None
            env_key = resolved[0] if resolved else (aliases[0] if aliases else "")
        if not env_key:
            continue
        default_url = (
            env_values.get(f"{env_prefix}_BASE_URL") or definition.endpoint.base_url or ""
        ).strip().rstrip("/")
        derived.append({
            "env_key": env_key,
            "id": pid,
            # ollama 协议本地端点同样走 OpenAI 兼容客户端（与 ProviderService._record 规则一致）
            "format": "anthropic" if definition.protocol is ProviderProtocol.ANTHROPIC else "openai",
            "default_url": default_url,
            "label": get_provider_label(pid),
            "url_keyed": not definition.auth.required,
        })
    return derived


def builtin_provider_ids() -> set[str]:
    """内置 provider id 集合（单一事实源：provider catalog 的 builtin 标记）。

    catalog 加载失败时返回空集：调用方据此写入 builtin=False 兜底，
    不影响启动流程（前端仍会从 /providers 的 catalog 派生结果纠正显示）。
    """
    try:
        from config import get_builtin_providers
        return set(get_builtin_providers())
    except (ImportError, OSError, ValueError):
        return set()


def _ensure_provider_key_file(pid: Any, api_key: Any, os_module: Any) -> None:
    """确保证书文件存在且内容正确（加密存储，非明文）。"""
    from llm_gateway.provider_service import ProviderCredentialStore

    credentials = ProviderCredentialStore()
    if credentials.read(pid) != api_key:
        credentials.write(pid, api_key)


def _resolve_key_file_writer() -> Any:
    """解析写凭证的函数，优先经 web.server 取。

    历史 patch 点契约：测试以 ``monkeypatch.setattr(server,
    "_ensure_provider_key_file", ...)`` 拦截写盘，本模块抽离后必须继续
    尊重这一 seam —— web.server 从本模块 re-export 同名符号，若被替换
    （与原函数不是同一对象）则用替换后的版本，从而实现 monkeypatch 生效。
    """
    try:
        from web import server as _server
    except ImportError:
        return _ensure_provider_key_file
    patched = getattr(_server, "_ensure_provider_key_file", None)
    if patched is not None and patched is not _ensure_provider_key_file:
        return patched
    return _ensure_provider_key_file


def register_env_providers(cfg: Any, env_values: Any, os_module: Any) -> None:
    """从 .env 注册已知免费模型平台 provider（元数据单一来源：provider catalog）。"""
    write_key_file = _resolve_key_file_writer()
    known_env_providers = _derive_known_env_providers(env_values)
    if not known_env_providers:
        logger.warning("webui.env_providers_derive_empty reason=provider_catalog_unavailable")
    builtin_ids = builtin_provider_ids()
    for order, entry in enumerate(known_env_providers):
        env_key = entry["env_key"]
        pid = entry["id"]
        label = entry["label"]
        fmt = entry["format"]
        if entry["url_keyed"]:
            # 本地无 key 接口：仅当 .env 显式配置 base_url 时才注册
            api_key = pid
            base_url = env_values.get(env_key, "").strip()
            if not base_url:
                continue
        else:
            api_key = env_values.get(env_key, "").strip()
            base_url = entry["default_url"]
            if not api_key:
                continue
        existing = cfg.get("models.providers", {}) or {}
        if pid not in existing:
            cfg.set(f"models.providers.{pid}", {
                "label": label, "format": fmt, "base_url": base_url,
                "default_model": "", "enabled": True,
                "order": order,
                # 内置 provider 标记随记录落盘：供前端模型配置 Tab 区分
                # 内置/自定义区（catalog 仍是运行时单一事实源，此为兜底冗余）。
                "builtin": pid in builtin_ids,
            })
        write_key_file(pid, api_key, os_module)


__all__ = [
    "_ENV_PROVIDER_ORDER",
    "_derive_known_env_providers",
    "builtin_provider_ids",
    "register_env_providers",
]
