"""Web API 全端点速率限制 — Token Bucket + 滑动窗口

FastAPI 中间件, 防止 DDoS/滥用。
"""
from fastapi import Request, HTTPException
from collections import defaultdict
import time, asyncio
from loguru import logger


class TokenBucketLimiter:
    """Token Bucket 限流器"""

    def __init__(self, rate: float = 30, capacity: int = 30):
        self._rate = rate          # 每分钟补充令牌数
        self._capacity = capacity   # 桶容量
        self._buckets: dict[str, tuple[float, float]] = defaultdict(
            lambda: (capacity, time.monotonic())
        )
        self._lock = asyncio.Lock()

    async def acquire(self, key: str) -> bool:
        async with self._lock:
            tokens, last_time = self._buckets[key]
            now = time.monotonic()
            elapsed = now - last_time
            tokens = min(self._capacity, tokens + elapsed * (self._rate / 60))
            if tokens >= 1:
                self._buckets[key] = (tokens - 1, now)
                return True
            self._buckets[key] = (tokens, now)
            return False


# 全局限流器实例
_limiter = TokenBucketLimiter(rate=30, capacity=30)

# 不需要限速的路径
_EXEMPT_PATHS = {"/health", "/health/self", "/api/health", "/ws", "/favicon.ico"}


async def rate_limit_middleware(request: Request, call_next):
    """FastAPI 速率限制中间件"""
    # 跳过健康检查和 WebSocket
    path = request.url.path
    if path in _EXEMPT_PATHS or path.startswith("/ws"):
        return await call_next(request)

    # 按客户端 IP 限速
    client_id = request.client.host if request.client else "unknown"
    if not await _limiter.acquire(client_id):
        logger.warning(f"速率限制触发: client={client_id}, path={path}")
        raise HTTPException(429, "请求过于频繁,请稍后再试")

    return await call_next(request)


def get_limiter() -> TokenBucketLimiter:
    return _limiter
