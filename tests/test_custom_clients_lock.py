"""_custom_clients 统一锁保护测试。

验证 `ModelRouter._custom_clients` 的所有读写都通过受锁保护的方法进行，
而不是让 ProviderService（自有 _locks）与 ModelRouter（_credential_locks）
各自无锁/独立加锁地访问同一份 dict。

并发竞态难以稳定复现，因此以结构性测试为主：
- ModelRouter 暴露统一的锁保护方法
- 读写点不再直接访问底层 dict 而无锁
- 并发 set/get 不抛异常且最终值一致（辅助）
"""
from __future__ import annotations

import inspect
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest


def _minimal_router():
    from model_router import ModelRouter

    router = ModelRouter.__new__(ModelRouter)
    router._custom_clients = {}
    router._custom_clients_lock = threading.Lock()
    return router


def test_router_exposes_lock_protected_custom_client_methods():
    router = _minimal_router()
    for name in ("get_custom_client", "set_custom_client", "remove_custom_client"):
        assert callable(getattr(router, name))


def test_set_get_remove_custom_client_roundtrip():
    router = _minimal_router()
    client = object()

    assert router.get_custom_client("prov") is None
    router.set_custom_client("prov", client)
    assert router.get_custom_client("prov") is client
    router.remove_custom_client("prov")
    assert router.get_custom_client("prov") is None


def test_has_custom_client_reflects_membership():
    router = _minimal_router()
    assert router.has_custom_client("prov") is False
    router.set_custom_client("prov", object())
    assert router.has_custom_client("prov") is True


def test_concurrent_set_get_custom_client_serialized():
    router = _minimal_router()
    writers = 8
    rounds = 300

    def writer(index: int) -> None:
        for _ in range(rounds):
            router.set_custom_client("shared", index)
            router.get_custom_client("shared")

    with ThreadPoolExecutor(max_workers=writers) as pool:
        list(pool.map(writer, range(writers)))

    assert router.get_custom_client("shared") in set(range(writers))


def test_concurrent_distinct_providers_consistent():
    router = _minimal_router()
    providers = [f"prov-{i}" for i in range(16)]

    def worker(provider_id: str) -> None:
        for _ in range(100):
            router.set_custom_client(provider_id, provider_id)
        assert router.get_custom_client(provider_id) == provider_id

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, providers))

    for provider_id in providers:
        assert router.get_custom_client(provider_id) == provider_id


def test_custom_clients_access_routes_through_lock_methods():
    import llm_gateway.provider_service as provider_service
    import web.custom_providers as custom_providers

    # ProviderService 不再持有无锁的 _runtime_clients() 后门
    assert not hasattr(provider_service.ProviderService, "_runtime_clients")

    register_src = inspect.getsource(custom_providers.register_into_router)
    assert "set_custom_client" in register_src
    assert "_custom_clients[" not in register_src

    unregister_src = inspect.getsource(custom_providers.unregister_from_router)
    assert "remove_custom_client" in unregister_src
    assert "_custom_clients" not in unregister_src


@pytest.mark.asyncio
async def test_close_tolerates_custom_clients_without_close_method():
    """close() 不应因没有 .close() 方法的自定义客户端而抛 AttributeError。"""
    router = _minimal_router()
    router._client = None
    router._agnes_client = None

    class NoCloseClient:
        pass

    router.set_custom_client("compat", NoCloseClient())

    # 不应抛 AttributeError
    await router.close()

    assert router.get_custom_client("compat") is None
