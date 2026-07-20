"""G4: 全局共享 httpx.AsyncClient 单例（连接池复用 + HTTP/2）.

40+ 处 ``async with httpx.AsyncClient(timeout=N)`` 每次新建连接，
TLS 握手 200-500ms。本模块提供全局共享 client，复用 TCP/TLS 连接并启用 HTTP/2 多路复用，
显著降低高频 HTTP 调用点（reranker / query_transform / memory_distiller 等）的尾延迟。

使用约束：
- 禁止修改 ``client.timeout`` 等全局属性（会污染共享 client）。
- 仅通过 ``client.get(url, timeout=...)`` / ``client.post(url, timeout=...)``
  在单次请求级别覆盖超时。
- 保留 ``event_hooks``（如 SSRF 检查）的临时实例化，不池化。
- 应用退出时调用 :func:`close_shared_client` 释放连接池。
"""
from typing import Optional

import httpx

_shared_client: Optional[httpx.AsyncClient] = None


def get_shared_client() -> httpx.AsyncClient:
    """获取全局共享 httpx.AsyncClient 单例.

    特性：
    - ``max_connections=50``, ``max_keepalive_connections=20``, ``keepalive_expiry=30s``
    - HTTP/2 启用（多路复用）
    - 默认 timeout 30s（connect 5s），单次请求可通过 ``timeout=`` 参数覆盖

    Returns:
        httpx.AsyncClient: 共享 client（如已关闭则自动重建）
    """
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=20,
                keepalive_expiry=30,
            ),
            timeout=httpx.Timeout(30.0, connect=5.0),
            http2=True,
        )
    return _shared_client


async def close_shared_client() -> None:
    """关闭共享 client（应用退出时调用）.

    幂等：多次调用安全。关闭后再次 :func:`get_shared_client` 会重建实例。
    """
    global _shared_client
    if _shared_client and not _shared_client.is_closed:
        await _shared_client.aclose()
    _shared_client = None
