"""Agnes Transport - 适配 Agnes AI API"""
import os
import asyncio
import httpx
from openai import AsyncOpenAI
from loguru import logger
from transports.base import ProviderTransport, TransportResponse

# agnes API max_tokens 上限 65536，超出返回 500 invalid_request
# 直接调用 transport.chat() 的路径（绕过 model_router._build_route_kwargs）
# 必须在此处 clamp，否则会触发 agnes 服务端 500 错误并进入 fallback 链
AGNES_MAX_TOKENS_LIMIT = 65535  # 留 1 token 余量

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

# 模块级共享 httpx client：所有 AgnesTransport 实例复用同一连接池，
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
            logger.warning("agnes_transport.close_shared_client_failed error={}", str(e))
    _agnes_http_client = None


def _clamp_agnes_max_tokens(max_tokens: int) -> int:
    """将 max_tokens 限制在 agnes API 上限内。"""
    if max_tokens > AGNES_MAX_TOKENS_LIMIT:
        return AGNES_MAX_TOKENS_LIMIT
    return max_tokens


class AgnesTransport(ProviderTransport):
    """Agnes AI API 的传输适配器。"""

    def __init__(self) -> None:
        """初始化 Agnes 传输适配器。"""
        # 从 os.getenv() 实时读取，避免使用 config 模块级冻结变量
        _key = os.getenv("AGNES_API_KEY", "")
        _url = os.getenv("AGNES_BASE_URL", "https://apihub.agnes-ai.cn/v1")
        if _key:
            self._client = AsyncOpenAI(
                api_key=_key,
                base_url=_url,
                http_client=_get_agnes_http_client(),
                timeout=AGNES_HTTP_TIMEOUT,
                max_retries=0,  # 禁用 SDK 内部盲重试，由 model_router 统一控制重试
            )
        else:
            self._client = None

    @property
    def provider_name(self) -> str:
        """返回 provider 名称 'agnes'。"""
        return "agnes"

    def is_available(self) -> bool:
        """返回 Agnes 客户端是否已初始化。"""
        return self._client is not None

    async def chat(self, model: str, messages: list[dict],
                   temperature: float = 0.7, max_tokens: int = 4096,
                   tools: list[dict] | None = None,
                   tool_choice: str | None = None,
                   stream: bool = False,
                   timeout: int = 60,
                   thinking: dict | None = None) -> TransportResponse:
        """调用 Agnes 对话接口，返回统一格式的 TransportResponse。"""
        if not self._client:
            raise RuntimeError("Agnes client not initialized")

        # 防御性 clamp：即便上层（如 agent_dispatcher/task_orchestrator）
        # 直接以 ROUTE_TABLE 默认值 131072 调用，也不会触发 agnes 500 错误
        max_tokens = _clamp_agnes_max_tokens(max_tokens)

        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        # Agnes 可能不支持工具调用，谨慎处理
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"

        # 支持 thinking 参数（Agnes Thinking 模式）
        # 修复：必须显式传递 enable_thinking=False，否则 agnes-2.0-flash 在边界条件下仍返回 reasoning_content
        # thinking 可能是 {"type": "enabled"} / {"type": "disabled"} / None
        _thinking_cfg = thinking or {}
        _thinking_enabled = _thinking_cfg.get("type") == "enabled"
        kwargs["extra_body"] = {
            "chat_template_kwargs": {"enable_thinking": _thinking_enabled}
        }

        response = await asyncio.wait_for(
            self._client.chat.completions.create(**kwargs),
            timeout=timeout,
        )

        msg = response.choices[0].message

        usage = None
        if response.usage:
            usage = {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(response.usage, "completion_tokens", 0) or 0,
            }

        tool_calls = None
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            tool_calls = [
                {
                    "id": str(tc.id),
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": str(tc.function.arguments) if tc.function.arguments else "{}",
                    },
                }
                for tc in msg.tool_calls
            ]

        return TransportResponse(
            content=msg.content or "",
            tool_calls=tool_calls,
            reasoning_content=None,
            usage=usage,
            raw_response=response,
        )
