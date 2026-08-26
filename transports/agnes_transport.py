"""Agnes 共享 httpx.AsyncClient 配置。

历史注：本模块原为双 transport 栈的一部分（ProviderTransport/MiMoTransport/
AgnesTransport ABC 体系，注册进 model_router._transports 后唯一读取点是
local-ort），transport 类主体已随死栈移除；仅保留仍被活代码使用的共享
HTTP client 基础设施：

- llm_gateway/client_lifecycle.py（agnes 客户端创建/刷新）
- llm_gateway/router_metrics.py（close_agnes_shared_client 退出清理）
- model_router.py（__init__ 构造 _agnes_client）
- web/server.py（启动预热 ping）

新 transport 实现一律走 llm_gateway/transports/ 体系，勿在此重建类。
"""
import httpx
from loguru import logger

# 根因修复：SDK 默认 connect=5.0 对跨网（经 Cloudflare）的 agnes API 过短，
# 网络抖动期 TCP+TLS 握手 5s 内无法完成 → APIConnectionError（实测 09:24-10:55 爆发 60 次）。
# 调整为 connect=15s 给握手 3 倍余量；max_retries=0 禁用 SDK 内部盲重试（对连接错误无效且放大延迟），
# 重试控制权统一交回 model_router（保留重试路径作为安全网，正常不触发）。
# 共享 httpx.AsyncClient 配置连接池 keepalive，避免 stale 连接复用导致首次请求失败。
#
# 治本修复（2026-08-05 用户"治标不治本"反馈）：read 8→15。
# 根因（实测铁证）：agnes-2.0-flash 服务端强制 thinking，所有 enable_thinking=False
#   参数变体（chat_template_kwargs / enable_thinking / thinking.type=disabled）均无效，
#   全部返回 reasoning_content，正常响应 6-7s（POST /chat/completions 实测 6.98s）。
#   这是 agnes API 的硬约束，客户端无法关闭。
# read=15s 卡边缘：agnes 直接测试 6.5-13s 波动，偶发 13s+ → 15s timeout 超时。
# read=30s 治本：覆盖 agnes 偶发慢 13s + 17s 余量，正常调用永不超时。
AGNES_HTTP_TIMEOUT = httpx.Timeout(connect=15.0, read=30.0, write=15.0, pool=10.0)
AGNES_HTTP_LIMITS = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=20,
    # 治本修复（2026-08-05 用户"治标不治本"反馈）：30→300。
    # 根因：keepalive_expiry=30s，用户 30s 内无消息 → 连接关闭 →
    #   下次请求重新 TCP+TLS 握手 6s + agnes thinking 6.5s = 12.5s（实测铁证）。
    #   12s timeout 卡边缘 → TimeoutError → 用户收不到回复。
    # 300s（5分钟）覆盖用户正常对话间隔，连接保持热，首次调用 6.5s（无握手）。
    # 配合启动预热（lifespan 中 ping agnes），彻底消除握手延迟。
    keepalive_expiry=300.0,
)

# 模块级共享 httpx client：所有 agnes AsyncOpenAI 实例复用同一连接池，
# 避免每次 new AsyncOpenAI 自建 client 导致连接池碎片化。
_agnes_http_client: httpx.AsyncClient | None = None


def _get_agnes_http_client() -> httpx.AsyncClient:
    """返回共享的 httpx.AsyncClient，惰性初始化。"""
    global _agnes_http_client
    if _agnes_http_client is None or _agnes_http_client.is_closed:
        # P0 修复（2026-08-05 agnes 超时根因）：强制 IPv4（local_address='0.0.0.0'）。
        # 根因：本机 DNS 解析 agnes 返回 IPv6（2408:8752:...）+ IPv4（116.162.25.57），
        # 但本机 IPv6 路由不通（socket AF_INET6 connect 立即 OSError）。httpx 默认
        # happy-eyeballs 优先 IPv6 → IPv6 失败后 IPv4 回退卡住 → ConnectTimeout 10s+。
        # 实测：默认 httpx 超时；强制 IPv4 后 0.42s 成功（code=301）。
        # 这就是 agnes "偶发超时"的真正根因——不是 API 慢，是 IPv6 路由问题。
        # 修复：local_address='0.0.0.0' 绑定 IPv4，跳过 IPv6 直连 IPv4。
        _agnes_transport = httpx.AsyncHTTPTransport(
            http2=False,  # agnes API 无需 HTTP/2，避免 h2 协商开销
            local_address="0.0.0.0",  # 强制 IPv4，绕过不可用的 IPv6 路由
        )
        _agnes_http_client = httpx.AsyncClient(
            timeout=AGNES_HTTP_TIMEOUT,
            limits=AGNES_HTTP_LIMITS,
            transport=_agnes_transport,
        )
    return _agnes_http_client


async def close_agnes_shared_client() -> None:
    """关闭共享的 Agnes httpx client（应用退出时调用）。

    幂等：多次调用安全。关闭后再次 :func:`_get_agnes_http_client` 会重建实例。

    CodeRabbit 修复：共享 client 的关闭所有权归本模块，而非各 AsyncOpenAI wrapper。
    wrapper 调用 ``.close()`` 会连带关闭注入的共享 httpx client，影响其他复用该 client
    的实例（包括 ``refresh_client`` 新建的 agnes_client —— 它复用同一共享 client）。
    应用退出时由本函数统一关闭一次，wrapper 自身不关闭共享 client。
    """
    global _agnes_http_client
    if _agnes_http_client is not None and not _agnes_http_client.is_closed:
        try:
            await _agnes_http_client.aclose()
        except Exception as e:
            logger.warning("agnes_transport.close_shared_client_failed error={}", str(e), exc_info=True)
    _agnes_http_client = None
