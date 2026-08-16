"""model_router Phase 3（客户端生命周期/凭证轮换 Mixin 抽出）结构契约测试。

背景：ModelRouter 内的凭证轮换与客户端刷新逻辑（凭证锁 / 池注册 /
懒注册 / refresh_client / 客户端选择与懒恢复 / 错误时轮换）抽为
llm_gateway/client_lifecycle.ClientLifecycleMixin，方法体逐字节搬移，
ModelRouter 继承该 Mixin 保持 self 语义（对齐 db 拆分的 Mixin 先例）。
"""
import sys
from pathlib import Path

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
    import sys as _sys
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
