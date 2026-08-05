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

HTTP/2 优雅降级：
- 启用 HTTP/2 需要 ``h2`` 包（``pip install httpx[http2]``）。
- 生产环境（如 Windows 安装包）可能未安装 ``h2``，此时强制 ``http2=True``
  会在创建 client 时抛出 ``ImportError``，导致整个应用启动失败。
- 本模块在启动时检测 ``h2`` 是否可用，若不可用则降级为 HTTP/1.1 并记录
  warning 日志（一次性），保证应用可启动；HTTP/1.1 的连接池复用仍能提供
  主要性能收益（TLS 握手复用），仅失去多路复用。
"""
from typing import Optional

import httpx
from loguru import logger

_shared_client: Optional[httpx.AsyncClient] = None
_http2_warned: bool = False


def _detect_http2_available() -> bool:
    """检测 h2 包是否已安装（HTTP/2 前置依赖）.

    httpx 启用 http2=True 时会在内部 import h2，未安装则抛 ImportError。
    本函数提前检测，避免在创建 client 时崩溃。
    """
    try:
        import h2  # noqa: F401
        return True
    except ImportError:
        return False


def get_shared_client() -> httpx.AsyncClient:
    """获取全局共享 httpx.AsyncClient 单例.

    特性：
    - ``max_connections=50``, ``max_keepalive_connections=20``, ``keepalive_expiry=30s``
    - HTTP/2 启用（多路复用，需 h2 包；未安装时优雅降级为 HTTP/1.1）
    - 默认 timeout 30s（connect 15s），单次请求可通过 ``timeout=`` 参数覆盖

    根因修复（2026-07-29）：connect 5s → 15s。原 5s 对跨网 SiliconFlow API 过短，
    网络抖动期 TCP+TLS 握手失败导致 reranker/query_transform/distiller 调用慢，
    触发 memory_manager 多处外层 wait_for 超时（治标）。connect=15s 给握手 3 倍余量，
    与 agnes API 客户端保持一致，从根因消除外层超时兜底的必要性。

    Returns:
        httpx.AsyncClient: 共享 client（如已关闭则自动重建）
    """
    global _shared_client, _http2_warned
    if _shared_client is None or _shared_client.is_closed:
        http2_enabled = _detect_http2_available()
        if not http2_enabled and not _http2_warned:
            # 仅首次降级时记录 warning，避免每次创建 client 重复刷日志
            logger.warning(
                "http_pool.http2_unavailable",
                hint="h2 包未安装，降级为 HTTP/1.1。"
                     "运行 `pip install httpx[http2]` 启用 HTTP/2 多路复用。"
                     "HTTP/1.1 连接池复用仍生效，仅失去多路复用。",
            )
            _http2_warned = True
        _shared_client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=20,
                # 治本修复（2026-08-05）：30→300。与 agnes_transport 保持一致。
                # 根因：embed API 共用此 client，keepalive=30s 过期后重新握手 6s，
                #   导致 embed 首次冷启动慢 → memory retrieval 超时。
                #   日志 memory.retrieve_timeout_single 铁证。
                # 300s 覆盖正常对话间隔，连接保持热，embed 0.1s（无握手）。
                keepalive_expiry=300,
            ),
            timeout=httpx.Timeout(30.0, connect=15.0),
            http2=http2_enabled,
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
