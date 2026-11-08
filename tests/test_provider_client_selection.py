"""严重-1 回归测试：内置 provider 的客户端选择不得把请求发到 MiMo 端点。

Bug 现象：把 chat 路由切到 DeepSeek 后，实际请求打到 https://api.xiaomimimo.com/v1
（MiMo 的端点 + MiMo 的 Key），用户只看到「能用但变差」。

根因：llm_gateway/client_lifecycle.py 的 _select_client_for_provider 把内置
provider 只分成两支——provider == "agnes" 走 _agnes_client，其余（else 分支
注释写着 provider == "mimo"）一律用 self._client。config/provider_metadata.json
里 builtin=true 的有 agnes / mimo / deepseek 三个，于是 provider=deepseek 落进
else，拿到的是 MiMo 的 AsyncOpenAI 实例。

修复：内置 provider 按 provider id 从 catalog 解析 api_key_env / base_url_default，
在 _builtin_clients 里各自独立缓存；mimo 仍复用 self._client（视觉链路等既有
调用方依赖它），其他内置 provider 绝不回退到 MiMo 客户端；无凭据时抛 E_LLM006。
"""
from __future__ import annotations

from urllib.parse import urlparse

import pytest
from openai import AsyncOpenAI

from core.app_exception import LLMError
from core.error_codes import ErrorCodeEnum

MIMO_HOST = "api.xiaomimimo.com"
DEEPSEEK_HOST = "api.deepseek.com"


def _make_router() -> "object":
    """构造一个只含客户端选择所需属性的 ModelRouter 实例。

    不用 __init__：构造函数会建真实客户端、注册凭证池、加载路由表，
    与本缺陷无关，且会把测试变成集成测试。
    """
    from model_router import ModelRouter

    router = ModelRouter.__new__(ModelRouter)
    router._client = AsyncOpenAI(api_key="sk-mimo-test", base_url=f"https://{MIMO_HOST}/v1")
    router._agnes_client = None
    router._custom_clients = {}
    router._builtin_clients = {}
    router._credential_locks = {}
    return router


def _host(client: object) -> str:
    return urlparse(str(getattr(client, "base_url"))).netloc


def _reset_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空环境变量，让 base_url / api_key 回到 catalog 默认值（避免 .env 干扰）。"""
    for name in (
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "MIMO_API_KEY",
        "MIMO_BASE_URL",
        "AGNES_API_KEY",
        "AGNES_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.asyncio
async def test_deepseek_client_uses_deepseek_endpoint(monkeypatch):
    """provider=deepseek 时，客户端 base_url 必须是 api.deepseek.com。

    回归：旧实现落进 else 分支（注释写着 provider == "mimo"），复用 self._client，
    实际 base_url 是 api.xiaomimimo.com。
    """
    _reset_provider_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-unit-test")

    router = _make_router()
    client = await router._select_client_for_provider("deepseek")

    assert _host(client) == DEEPSEEK_HOST, (
        f"provider=deepseek 的客户端应指向 {DEEPSEEK_HOST}，实际 {_host(client)}"
    )
    assert str(client.base_url).startswith("https://api.deepseek.com/v1")
    assert client.api_key == "sk-deepseek-unit-test"


@pytest.mark.asyncio
async def test_deepseek_client_does_not_reuse_mimo_client(monkeypatch):
    """provider=deepseek 不得复用 MiMo 的客户端对象（即使 MiMo 客户端已存在）。"""
    _reset_provider_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-unit-test")

    router = _make_router()
    mimo_client = router._client
    client = await router._select_client_for_provider("deepseek")

    assert client is not mimo_client, "deepseek 不得返回 MiMo 客户端（旧实现的静默降级）"
    assert client.api_key != mimo_client.api_key
    # MiMo 客户端本身不被污染
    assert _host(mimo_client) == MIMO_HOST


@pytest.mark.asyncio
async def test_mimo_client_unchanged(monkeypatch):
    """provider=mimo 仍返回 self._client，base_url 仍为 api.xiaomimimo.com。"""
    _reset_provider_env(monkeypatch)

    router = _make_router()
    mimo_client = router._client
    client = await router._select_client_for_provider("mimo")

    assert client is mimo_client
    assert _host(client) == MIMO_HOST


@pytest.mark.asyncio
async def test_deepseek_without_key_raises_llm006_without_mimo_fallback(monkeypatch):
    """deepseek 无凭据时抛 E_LLM006，绝不退回 MiMo 客户端。"""
    _reset_provider_env(monkeypatch)

    router = _make_router()
    with pytest.raises(LLMError) as exc_info:
        await router._select_client_for_provider("deepseek")

    assert exc_info.value.error_code == ErrorCodeEnum.E_LLM006
    assert "deepseek" in str(exc_info.value).lower()
    # 关键：失败不得静默回退到 MiMo（否则又变成“能用但变差”）
    assert router._builtin_clients.get("deepseek") is None


@pytest.mark.asyncio
async def test_agnes_client_unchanged(monkeypatch):
    """agnes 行为不变：已有的 _agnes_client 优先级最高。"""
    _reset_provider_env(monkeypatch)

    router = _make_router()
    agnes_client = AsyncOpenAI(api_key="sk-agnes-test", base_url="https://apihub.agnes-ai.cn/v1")
    router._agnes_client = agnes_client

    client = await router._select_client_for_provider("agnes")

    assert client is agnes_client


@pytest.mark.asyncio
async def test_builtin_client_cached_per_provider(monkeypatch):
    """每个内置 provider 各自缓存一个客户端；重复选择返回同一实例。"""
    _reset_provider_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-unit-test")

    router = _make_router()
    first = await router._select_client_for_provider("deepseek")
    second = await router._select_client_for_provider("deepseek")

    assert first is second
    assert router._builtin_clients == {"deepseek": first}


def test_custom_provider_still_raises_when_unregistered(monkeypatch):
    """非内置、未注册的自定义 provider 仍抛 E_LLM006（不被新分支吞掉）。"""
    _reset_provider_env(monkeypatch)

    router = _make_router()
    # 懒注册依赖 config_service，这里让它不产生客户端
    router._lazy_register_provider = lambda provider: None

    with pytest.raises(LLMError) as exc_info:
        import asyncio
        asyncio.run(router._select_client_for_provider("no-such-provider"))

    assert exc_info.value.error_code == ErrorCodeEnum.E_LLM006
