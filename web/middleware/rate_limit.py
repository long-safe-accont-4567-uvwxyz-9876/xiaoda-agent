"""Web API 全端点速率限制 — Token Bucket 三级限流 (全局 / 用户 / 写端点)

FastAPI/Starlette 中间件, 防止 API 滥用与 DDoS。

设计说明:
    core/slo_tracker.py 中已有 TokenBucket / RateLimiter 类, 但其采用 rps 语义且
    使用 time.time() (墙上时钟), 不返回 retry_after, 不适合 HTTP 限流场景。
    本模块是独立的 Web 中间件版本: 采用 per-minute 语义 + time.monotonic() (单调时钟,
    测试稳定), 并支持返回 Retry-After。不修改 core/slo_tracker.py (独立 SLO 模块),
    设计参考其三级 (全局 / 用户 / 端点) 模式。

三级限流:
    1. 全局: 所有请求共享一个桶, 默认 600 req/min
    2. 用户: 按 (client_ip, user_id) 分桶, 默认 60 req/min
    3. 写端点: 对 POST/PUT/DELETE/PATCH 请求额外限流, 默认 30 req/min
请求需同时通过全局桶与用户桶; 若为写操作还需通过写端点桶, 任一失败返回 429。

白名单: localhost (127.0.0.1 / ::1) 与内网 IP (10/8、172.16/12、192.168/16) 自动放行。

配置覆盖: 环境变量 RATE_LIMIT_GLOBAL / RATE_LIMIT_USER / RATE_LIMIT_WRITE 调整默认值,
构造参数优先级最高 (便于测试)。
"""
import asyncio
import ipaddress
import math
import os
import time
from collections import defaultdict
from typing import Any, Iterable, Optional, Tuple

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


# ── 写操作 HTTP 方法 (应用更严的写端点限制) ──
_WRITE_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})

# ── 不限流的路径: 健康检查 / WebSocket / favicon / 根入口 ──
_DEFAULT_EXEMPT_PATHS = frozenset({
    "/health", "/health/self", "/api/v1/health", "/api/health",
    "/ws", "/favicon.ico", "/",
})

# ── localhost 主机名集合 ──
_LOCALHOST_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0:0:0:0:0:0:0:1"})


def _is_private_ip(host: str) -> bool:
    """判断 host 是否为回环/内网/链路本地 IP (白名单放行)。"""
    if not host or host in _LOCALHOST_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        # 非合法 IP (如 "testclient"), 不视为内网
        return False


class TokenBucket:
    """轻量令牌桶 (per-minute 语义, 单调时钟)

    rate_per_min: 每分钟补充令牌数; capacity: 桶容量 (默认等于 rate, 允许整段突发)。
    每个桶内部自带 asyncio.Lock, 单桶内串行消费, 避免并发超额。
    """

    __slots__ = ("capacity", "rate_per_min", "_tokens", "_last", "_lock")

    def __init__(self, rate_per_min: float, capacity: Optional[float] = None) -> None:
        self.capacity = float(capacity if capacity is not None else rate_per_min)
        self.rate_per_min = float(rate_per_min)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n: float = 1.0) -> Tuple[bool, float]:
        """尝试消费 n 个令牌。

        返回 (是否成功, retry_after_seconds):
            - 成功: (True, 0.0)
            - 失败: (False, 凑够 n 个令牌需等待的秒数)
        """
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            # 按每分钟速率补充令牌
            self._tokens = min(self.capacity, self._tokens + elapsed * (self.rate_per_min / 60.0))
            self._last = now
            if self._tokens >= n:
                self._tokens -= n
                return True, 0.0
            # 计算等待时间: 缺口 / 每秒补充速率
            deficit = n - self._tokens
            wait = deficit / (self.rate_per_min / 60.0)
            return False, wait


class RateLimitMiddleware(BaseHTTPMiddleware):
    """三级速率限制中间件: 全局 / 用户 / 写端点。

    用法 (server.py):
        from web.middleware.rate_limit import RateLimitMiddleware
        app.add_middleware(RateLimitMiddleware)         # 路由之前注册, 尽早拦截

    超限响应:
        HTTP 429
        Header: Retry-After: <seconds>
        Body:   {"detail": "Rate limit exceeded", "retry_after": <seconds>}
    """

    def __init__(
        self,
        app: Any,
        global_limit: Optional[float] = None,
        user_limit: Optional[float] = None,
        write_limit: Optional[float] = None,
        exempt_paths: Optional[Iterable[str]] = None,
        whitelist: Optional[Iterable[str]] = None,
    ) -> None:
        super().__init__(app)
        # 显式参数优先, 其次环境变量, 最后默认值
        self._global_limit = float(global_limit if global_limit is not None
                                   else os.environ.get("RATE_LIMIT_GLOBAL", 600))
        self._user_limit = float(user_limit if user_limit is not None
                                 else os.environ.get("RATE_LIMIT_USER", 60))
        self._write_limit = float(write_limit if write_limit is not None
                                  else os.environ.get("RATE_LIMIT_WRITE", 30))
        self._exempt_paths = set(exempt_paths) if exempt_paths else set(_DEFAULT_EXEMPT_PATHS)
        # 额外白名单 host (测试/运维可追加); 默认已含 localhost/内网 (见 _is_whitelisted)
        self._whitelist = set(whitelist) if whitelist else set()

        # 三级桶: 全局单桶; 用户桶与写端点桶按 (ip:user_id) 分桶
        self._global_bucket = TokenBucket(self._global_limit)
        self._user_buckets: dict = defaultdict(lambda: TokenBucket(self._user_limit))
        self._write_buckets: dict = defaultdict(lambda: TokenBucket(self._write_limit))

        logger.info(
            "rate_limit.middleware_init global={}/min user={}/min write={}/min",
            self._global_limit, self._user_limit, self._write_limit,
        )

    # ── 辅助方法 ──

    @staticmethod
    def _client_host(request: Request) -> str:
        client = request.client
        return client.host if client else "unknown"

    @staticmethod
    def _user_id(request: Request) -> str:
        """可选 user_id: 优先 X-User-ID header, 其次 request.state.user_id。"""
        uid = request.headers.get("X-User-ID")
        if uid:
            return uid.strip()
        return getattr(request.state, "user_id", "") or ""

    @staticmethod
    def _bucket_key(host: str, user_id: str) -> str:
        return f"{host}:{user_id}" if user_id else host

    def _is_whitelisted(self, host: str) -> bool:
        return host in self._whitelist or _is_private_ip(host)

    # ── 主分发逻辑 ──

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        path = request.url.path
        # 豁免路径直接放行 (健康检查 / WebSocket / favicon)
        if path in self._exempt_paths or path.startswith("/ws"):
            return await call_next(request)

        host = self._client_host(request)
        # 白名单放行 (localhost / 内网 / 显式配置)
        if self._is_whitelisted(host):
            return await call_next(request)

        user_id = self._user_id(request)
        key = self._bucket_key(host, user_id)
        is_write = request.method.upper() in _WRITE_METHODS

        # 1) 全局桶
        ok, retry = await self._global_bucket.acquire()
        if not ok:
            return self._too_many_requests(retry, scope="global", host=host, path=path)

        # 2) 用户桶 (按 ip+user_id 分桶, 同步获取 defaultdict 在单线程 asyncio 下安全)
        user_bucket = self._user_buckets[key]
        ok, retry = await user_bucket.acquire()
        if not ok:
            return self._too_many_requests(retry, scope="user", host=host, path=path)

        # 3) 写端点桶 (仅写操作)
        if is_write:
            write_bucket = self._write_buckets[key]
            ok, retry = await write_bucket.acquire()
            if not ok:
                return self._too_many_requests(retry, scope="write", host=host, path=path)

        return await call_next(request)

    def _too_many_requests(self, retry_after: float, *, scope: str, host: str, path: str) -> JSONResponse:
        """构造 429 响应: Retry-After header + JSON 错误体。"""
        wait = max(1, int(math.ceil(retry_after)))
        logger.warning(
            "rate_limit.exceeded scope={} host={} path={} retry_after={}s",
            scope, host, path, wait,
        )
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded", "retry_after": wait},
            headers={"Retry-After": str(wait)},
        )


class TokenBucketLimiter:
    """向后兼容: 按 key 分桶的限流器 (旧接口)。

    保留以兼容既有测试与代码; 新代码请使用 RateLimitMiddleware。
    内部基于 TokenBucket 实现, rate 为每分钟补充令牌数, capacity 为桶容量。
    """

    def __init__(self, rate: float = 30, capacity: int = 30) -> None:
        self._rate = rate
        self._capacity = capacity
        self._buckets: dict = defaultdict(
            lambda: TokenBucket(rate_per_min=rate, capacity=capacity)
        )

    async def acquire(self, key: str) -> bool:
        """消费 1 个令牌, 成功返回 True。"""
        ok, _ = await self._buckets[key].acquire()
        return ok
