# test_http_pool.py — G4: HTTP 连接池复用测试.
"""G4: HTTP 连接池复用测试.

P2-4 加固: 所有测试在 finally 中调用 close_shared_client，避免断言失败时 singleton 泄漏
影响后续测试（pytest-asyncio 模式下全局 singleton 会跨测试用例残留）。
"""
import asyncio
import httpx
import pytest

from utils.http_pool import get_shared_client, close_shared_client


@pytest.fixture(autouse=True)
async def _ensure_close_shared_client():
    """每个测试结束后确保关闭共享 client，无论断言是否通过。"""
    yield
    try:
        await close_shared_client()
    except Exception:
        pass


async def test_shared_client_is_singleton():
    """多次调用返回同一实例."""
    try:
        c1 = get_shared_client()
        c2 = get_shared_client()
        assert c1 is c2
    finally:
        await close_shared_client()


async def test_shared_client_has_pool_limits():
    """共享 client 应有连接池配置.

    httpx 0.28 的 AsyncClient 不暴露公开的 ``limits`` 属性，
    通过 transport pool 内部 ``_max_connections`` 等标志位验证。
    """
    try:
        client = get_shared_client()
        pool = client._transport._pool
        assert pool._max_connections == 50
        assert pool._max_keepalive_connections == 20
        assert pool._keepalive_expiry == 30
    finally:
        await close_shared_client()


async def test_shared_client_http2_enabled():
    """应启用 HTTP/2.

    httpx 0.28 的 AsyncClient 不暴露公开的 ``http2`` 属性，
    通过 transport pool 内部 ``_http2`` 标志位验证（同 httpx 内部测试用法）。
    """
    try:
        client = get_shared_client()
        assert client._transport._pool._http2 is True
    finally:
        await close_shared_client()


async def test_close_resets_singleton():
    """关闭后下次获取是新实例."""
    c1 = get_shared_client()
    await close_shared_client()
    c2 = get_shared_client()
    assert c1 is not c2
    # fixture teardown 会再次 close
