# test_http_pool.py — G4: HTTP 连接池复用测试.
"""G4: HTTP 连接池复用测试.

P2-4 加固: 所有测试在 finally 中调用 close_shared_client，避免断言失败时 singleton 泄漏
影响后续测试（pytest-asyncio 模式下全局 singleton 会跨测试用例残留）。

HTTP/2 优雅降级测试：验证 h2 包未安装时 client 仍可创建（降级为 HTTP/1.1），
防止生产环境（Windows 安装包）因缺少 h2 导致应用启动失败。
"""
import asyncio
import httpx
import pytest

import utils.http_pool as http_pool_mod
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
    """应启用 HTTP/2（当 h2 包已安装时）.

    httpx 0.28 的 AsyncClient 不暴露公开的 ``http2`` 属性，
    通过 transport pool 内部 ``_http2`` 标志位验证（同 httpx 内部测试用法）。

    若环境未安装 h2 包，本测试跳过（由 test_http2_graceful_degradation 覆盖降级路径）。
    """
    if not http_pool_mod._detect_http2_available():
        pytest.skip("h2 包未安装，跳过 HTTP/2 启用测试（降级路径已由其他测试覆盖）")
    try:
        client = get_shared_client()
        assert client._transport._pool._http2 is True
    finally:
        await close_shared_client()


async def test_http2_graceful_degradation_when_h2_missing(monkeypatch):
    """h2 包未安装时应优雅降级为 HTTP/1.1，不抛 ImportError.

    验证：模拟 h2 不可用时，get_shared_client 仍返回有效 client，
    且 http2 标志为 False（降级为 HTTP/1.1）。

    这是生产环境（Windows 安装包）防御性测试：防止缺少 h2 包导致应用启动崩溃。
    """
    # 模拟 h2 未安装：让 _detect_http2_available 返回 False
    monkeypatch.setattr(http_pool_mod, "_detect_http2_available", lambda: False)
    # 重置 warning 标志，确保本次测试能观察到降级日志
    monkeypatch.setattr(http_pool_mod, "_http2_warned", False)

    try:
        # 关键断言：不抛 ImportError，client 正常创建
        client = get_shared_client()
        assert client is not None
        assert not client.is_closed
        # http2 应为 False（降级为 HTTP/1.1）
        assert client._transport._pool._http2 is False
    finally:
        await close_shared_client()


async def test_http2_degradation_only_warns_once(monkeypatch):
    """降级 warning 日志只记录一次，避免重复刷日志.

    多次创建 client（close 后重建）时，warning 只在首次触发。
    CodeRabbit #10：捕获 loguru 日志，断言两次 client 创建仅产生 1 次降级
    warning；_http2_warned 状态作为补充断言保留。
    """
    monkeypatch.setattr(http_pool_mod, "_detect_http2_available", lambda: False)
    monkeypatch.setattr(http_pool_mod, "_http2_warned", False)

    # 捕获 loguru warning 日志到列表
    captured: list[str] = []
    handler_id = http_pool_mod.logger.add(
        lambda msg: captured.append(msg.record["message"]),
        level="WARNING",
    )
    try:
        # 第一次创建：应触发 warning
        c1 = get_shared_client()
        assert http_pool_mod._http2_warned is True

        # 关闭后第二次创建：不应再次 warning（_http2_warned 已为 True）
        await close_shared_client()
        c2 = get_shared_client()
        assert http_pool_mod._http2_warned is True  # 仍为 True，未重置

        # 主断言：两次 client 创建期间恰好 1 次 HTTP/2 降级 warning
        http2_warnings = [m for m in captured if "http2_unavailable" in m]
        assert len(http2_warnings) == 1, (
            f"期望恰好 1 次降级 warning，实际 {len(http2_warnings)}: {http2_warnings}"
        )
    finally:
        http_pool_mod.logger.remove(handler_id)
        await close_shared_client()


async def test_close_resets_singleton():
    """关闭后下次获取是新实例."""
    c1 = get_shared_client()
    await close_shared_client()
    c2 = get_shared_client()
    assert c1 is not c2
    # fixture teardown 会再次 close
