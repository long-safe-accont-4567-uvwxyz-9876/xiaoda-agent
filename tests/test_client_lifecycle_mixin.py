"""model_router Phase 3（客户端生命周期/凭证轮换 Mixin 抽出）结构契约测试。

背景：ModelRouter 内的凭证轮换与客户端刷新逻辑（凭证锁 / 池注册 /
懒注册 / refresh_client / 客户端选择与懒恢复 / 错误时轮换）抽为
llm_gateway/client_lifecycle.ClientLifecycleMixin，方法体逐字节搬移，
ModelRouter 继承该 Mixin 保持 self 语义（对齐 db 拆分的 Mixin 先例）。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import model_router
from llm_gateway.client_lifecycle import ClientLifecycleMixin


def test_mixin_imports_standalone():
    import importlib
    mod = importlib.import_module("llm_gateway.client_lifecycle")
    for name in ("_get_credential_lock", "_register_credential_pool_providers",
                 "_lazy_register_provider", "refresh_client",
                 "_ensure_credential_in_pool", "_select_client_for_provider",
                 "_rotate_credential_on_error"):
        assert hasattr(mod.ClientLifecycleMixin, name), f"缺少方法 {name}"


def test_model_router_inherits_mixin():
    """ModelRouter 必须继承 ClientLifecycleMixin（方法经 MRO 生效）"""
    assert issubclass(model_router.ModelRouter, ClientLifecycleMixin)
    # 未在 ModelRouter 体内重写（保证搬移后仍是同一实现）
    assert ("ClientLifecycleMixin._select_client_for_provider"
            not in "")  # placeholder
    assert model_router.ModelRouter._select_client_for_provider \
        is ClientLifecycleMixin._select_client_for_provider
    assert model_router.ModelRouter.refresh_client \
        is ClientLifecycleMixin.refresh_client


def test_mixin_does_not_import_model_router():
    """防循环依赖：client_lifecycle 不得 import model_router"""

    import llm_gateway.client_lifecycle as mod
    assert "model_router" not in getattr(mod, "__dict__", {})


def test_credential_lock_per_provider_isolated():
    """同一 provider 复用锁，不同 provider 隔离（搬移后行为不变）"""

    class FakeRouter(ClientLifecycleMixin):
        def __init__(self):
            self._credential_locks = {}

    r = FakeRouter()
    assert r._get_credential_lock("mimo") is r._get_credential_lock("mimo")
    assert r._get_credential_lock("mimo") is not r._get_credential_lock("agnes")


@pytest.mark.asyncio
async def test_rotate_credential_updates_client():
    """错误时轮换：池返回新凭证 → 替换对应 provider 客户端"""

    class FakeCred:
        def __init__(self, key, base_url=""):
            self.api_key = key
            self.base_url = base_url

    class FakePool:
        def __init__(self, cred):
            self._cred = cred

        async def get_credential(self, provider):
            return self._cred

    class FakeClassifier:
        pass

    class FakeRouter(ClientLifecycleMixin):
        def __init__(self, cred):
            self._credential_locks = {}
            self._credential_pool = FakePool(cred)
            self._client = None
            self._agnes_client = None

    router = FakeRouter(FakeCred("sk-new", "https://api.example.com/v1"))
    await router._rotate_credential_on_error("mimo", FakeClassifier())
    assert router._client is not None
    assert router._client.api_key == "sk-new"
    # 相同 key 不轮换（幂等）
    router2 = FakeRouter(FakeCred("sk-same"))
    router2._client = type("C", (), {"api_key": "sk-same"})()
    await router2._rotate_credential_on_error("mimo", FakeClassifier())
    assert router2._client.api_key == "sk-same"


def _make_router_with_custom_clients(pool):
    """构造带 _custom_clients 的最小 FakeRouter（自定义 provider 归属测试用）"""

    class FakeCustomRouter(ClientLifecycleMixin):
        def __init__(self, pool):
            self._credential_locks = {}
            self._credential_pool = pool
            self._client = None
            self._agnes_client = None
            self._custom_clients = {}

        def get_custom_client(self, provider_id):
            return self._custom_clients.get(provider_id)

        def set_custom_client(self, provider_id, client):
            self._custom_clients[provider_id] = client

        def has_custom_client(self, provider_id):
            return provider_id in self._custom_clients

    return FakeCustomRouter(pool)


@pytest.mark.asyncio
async def test_rotate_credential_updates_custom_provider_own_client(monkeypatch):
    """修复（2026-08-29 审计）：自定义 provider 轮换替换 _custom_clients 自身客户端。

    原实现把非 mimo 的替换一律写入 _agnes_client，导致 openrouter 等轮换时
    自身客户端不动、Agnes 被污染为自定义 endpoint/key。
    """
    built = []

    def fake_build_client(fmt, base_url, api_key):
        client = SimpleNamespace(api_key=api_key, base_url=base_url, fmt=fmt)
        built.append(client)
        return client

    # 按注册记录返回 anthropic 格式，验证重建走 format 感知的 build_client
    monkeypatch.setattr(
        "core_runtime.custom_providers.build_client", fake_build_client)
    monkeypatch.setattr(
        "core_runtime.config_service.get_config_service",
        lambda: SimpleNamespace(get=lambda _key: {"format": "anthropic"}))

    class FakePool:
        def __init__(self, cred):
            self._cred = cred

        async def get_credential(self, provider):
            return self._cred

    router = _make_router_with_custom_clients(FakePool(
        SimpleNamespace(api_key="sk-new", base_url="https://api.openrouter.ai/v1")))
    agnes_sentinel = SimpleNamespace(api_key="sk-agnes")
    router._agnes_client = agnes_sentinel
    old_custom = SimpleNamespace(api_key="sk-old", base_url="https://api.openrouter.ai/v1")
    router.set_custom_client("openrouter", old_custom)

    rotated = await router._rotate_credential_on_error(
        "openrouter", SimpleNamespace())

    assert rotated is True
    # 自身客户端被替换为新 key 的新客户端（按注册 format 重建）
    assert router.get_custom_client("openrouter") is built[0]
    assert router.get_custom_client("openrouter").api_key == "sk-new"
    assert router.get_custom_client("openrouter").fmt == "anthropic"
    assert router.get_custom_client("openrouter").base_url == "https://api.openrouter.ai/v1"
    # agnes / mimo 客户端不被污染
    assert router._agnes_client is agnes_sentinel
    assert router._client is None


@pytest.mark.asyncio
async def test_rotate_credential_skips_custom_provider_without_client(monkeypatch):
    """无已注册客户端的自定义 provider：优雅跳过，不代创建、不污染其他客户端"""
    monkeypatch.setattr(
        "core_runtime.custom_providers.build_client",
        lambda *_a: pytest.fail("无客户端时不应构建新客户端"))

    class FakePool:
        async def get_credential(self, provider):
            return SimpleNamespace(api_key="sk-new", base_url="https://x.example/v1")

    router = _make_router_with_custom_clients(FakePool())
    agnes_sentinel = SimpleNamespace(api_key="sk-agnes")
    router._agnes_client = agnes_sentinel

    rotated = await router._rotate_credential_on_error(
        "unknown-provider", SimpleNamespace())

    assert rotated is False
    assert router._custom_clients == {}
    assert router._agnes_client is agnes_sentinel


@pytest.mark.asyncio
async def test_rotate_credential_agnes_still_updates_agnes_client(monkeypatch):
    """agnes 轮换仍写入 _agnes_client（共享 httpx 配置保持不变）"""
    monkeypatch.setattr(
        "llm_gateway.client_lifecycle._get_agnes_http_client", lambda: None)

    class FakePool:
        async def get_credential(self, provider):
            return SimpleNamespace(api_key="sk-agnes-new", base_url="")

    router = _make_router_with_custom_clients(FakePool())
    old_agnes = SimpleNamespace(api_key="sk-agnes-old")
    router._agnes_client = old_agnes
    router.set_custom_client("agnes", SimpleNamespace(api_key="sk-custom-agnes"))

    rotated = await router._rotate_credential_on_error("agnes", SimpleNamespace())

    assert rotated is True
    assert router._agnes_client is not old_agnes
    assert router._agnes_client.api_key == "sk-agnes-new"
    # 自定义注册表不被写入（agnes 归属 _agnes_client）
    assert router.get_custom_client("agnes").api_key == "sk-custom-agnes"
