"""切换模型前的可用性校验与客户端物化。

2026-11-08 路由一致性修复（中等-3、4）：

旧实现 web/agent_registry.py::set_agent_model 只解析 base_url/api_key_env 后
调 SubAgent.reload_model_config，而后者只改写 config 四个字段、清掉降级标记
就返回 True：既没为该 provider 物化客户端，也没发出过任何请求。于是接口回
success 无法证明切换生效，切换后的真实请求还可能落到别的 provider 的客户端上。

本模块提供两个能力：
- materialize_provider_client：按 provider 建/取客户端并登记到 router，
  无 Key/无 base_url 就抛错，绝不静默回退到其他 provider 的客户端；
- probe_provider_model：轻量探活（优先 GET /models，必要时退化为 1 token chat），
  失败一律抛 ProviderProbeError，由调用方转成 4xx，不再「报告成功」。
"""
from __future__ import annotations

from typing import Any

from loguru import logger

PING_PROMPT = "ping"
DEFAULT_PROBE_TIMEOUT = 8.0


class ProviderProbeError(RuntimeError):
    """探活或客户端物化失败；message 直接面向接口调用者。"""


def _read_key(api_key_env: str) -> str:
    if not api_key_env:
        return ""
    try:
        from utils.env_reader import read_env_key

        return str(read_env_key(api_key_env) or "")
    except (ImportError, OSError, KeyError, ValueError):
        import os

        return os.getenv(api_key_env, "") or ""


def _build_client(base_url: str, api_key: str) -> Any:
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)


def provider_client_ready(router: Any, provider: str) -> bool:
    """router 上是否已经存在该 provider 可直接使用的客户端。"""
    if router is None:
        return False
    provider = (provider or "").strip().lower()
    if not provider:
        return False
    try:
        if router.has_custom_client(provider):
            return True
    except (AttributeError, KeyError):
        pass
    try:
        return router.get_builtin_client(provider) is not None
    except (AttributeError, ValueError, KeyError):
        return False


def materialize_provider_client(
    router: Any, provider: str, base_url: str, api_key_env: str,
) -> Any:
    """确保 router 上存在 provider 对应的客户端，并返回它。

    内置 provider 已存在则复用（不重复建连）；不存在则用当前 Key 建后 bind_builtin。
    自定义 provider 总是用新 base_url/Key 重建并 set_custom_client，
    保证热重载后运行时取到的是新客户端而不是旧凭证的缓存。
    """
    provider = (provider or "").strip().lower()
    if api_key_env:
        api_key_env = api_key_env.strip()
    if router is None:
        raise ProviderProbeError("路由未就绪，无法重建客户端")
    if not provider:
        raise ProviderProbeError("provider 为空，无法重建客户端")
    api_key = _read_key(api_key_env)
    if not api_key:
        raise ProviderProbeError(f"缺少 API Key（{api_key_env or '未配置环境变量名'}）")
    if not base_url:
        raise ProviderProbeError(f"provider {provider} 缺少 base_url")

    from config import get_builtin_providers

    if provider in get_builtin_providers():
        try:
            existing = router.get_builtin_client(provider)
        except (AttributeError, ValueError, KeyError):
            existing = None
        if existing is not None:
            return existing
        client = _build_client(base_url, api_key)
        try:
            router.bind_builtin(provider, client)
        except (AttributeError, KeyError, ValueError) as exc:
            raise ProviderProbeError(f"内置 provider {provider} 客户端绑定失败: {exc}") from exc
        logger.info("provider_switch.builtin_client_rebuilt provider={}", provider)
        return client

    client = _build_client(base_url, api_key)
    try:
        router.set_custom_client(provider, client)
    except (AttributeError, KeyError, ValueError) as exc:
        raise ProviderProbeError(f"provider {provider} 客户端登记失败: {exc}") from exc
    logger.info("provider_switch.custom_client_rebuilt provider={}", provider)
    return client


async def _chat_ping(base_url: str, api_key: str, model: str, timeout: float) -> dict:
    import httpx
    from openai import OpenAIError

    probe_errors = (OpenAIError, httpx.HTTPError, ValueError, TypeError, KeyError)
    client = _build_client(base_url, api_key)
    try:
        for max_tokens in (1, 16):
            try:
                resp = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": PING_PROMPT}],
                    max_tokens=max_tokens,
                    temperature=0,
                    timeout=timeout,
                )
                return {"probe": "chat", "max_tokens": max_tokens, "id": str(getattr(resp, "id", "") or "")}
            except probe_errors as exc:
                text = str(exc)
                if max_tokens == 1 and "max_tokens" in text.lower():
                    continue
                raise ProviderProbeError(f"探活请求失败（{type(exc).__name__}）: {text[:160]}") from exc
        raise ProviderProbeError("探活请求失败（max_tokens 不被接受）")
    finally:
        try:
            await client.close()
        except (OSError, RuntimeError, ValueError):
            logger.debug("provider_switch.probe_client_close_failed", exc_info=True)


async def probe_provider_model(
    provider: str,
    model: str,
    base_url: str,
    api_key_env: str,
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> dict:
    """切换前轻量探活；失败抛 ProviderProbeError。

    优先 GET {base_url}/models：便宜，且能同时校验证书/端点/模型名。
    模型不在返回列表时不算失败（不少聚合端点列表不全），退化为 1 token chat。
    /models 不可用（404/501 等）时同样退化为 chat 探活。
    凭证被拒（401/403）与网络不可达直接失败——这两种情形 chat 探活也不会成功。
    """
    provider = (provider or "").strip().lower()
    model = (model or "").strip()
    if not provider or not model:
        raise ProviderProbeError("provider 和 model 均不能为空")
    if not base_url:
        raise ProviderProbeError(f"provider {provider} 缺少 base_url")
    api_key = _read_key(api_key_env)
    if not api_key:
        raise ProviderProbeError(f"缺少 API Key（{api_key_env or '未配置环境变量名'}）")

    import httpx

    url = base_url.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        raise ProviderProbeError(f"端点不可达（{type(exc).__name__}）: {str(exc)[:120]}") from exc

    if resp.status_code in (401, 403):
        raise ProviderProbeError(f"凭证被拒（HTTP {resp.status_code}，{api_key_env}）")
    if resp.status_code == 200:
        try:
            ids = {str(m.get("id", "")) for m in (resp.json().get("data") or []) if isinstance(m, dict)}
        except ValueError:
            ids = set()
        if model in ids:
            return {"probe": "models", "status": 200, "model_listed": True}

    result = await _chat_ping(base_url, api_key, model, timeout)
    result["status"] = 200
    return result
