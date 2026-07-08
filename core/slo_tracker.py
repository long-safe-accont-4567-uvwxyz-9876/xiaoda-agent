"""SLO 四指标 + 三级限流 (Q3)

参考:
- Google SRE: SLO/SLI/Error Budget
- Token Bucket / Sliding Window 算法

特性:
- 4 个 SLO 指标: availability / latency / error_rate / throughput
- 三级限流: 全局 / 用户 / 端点
- 燃烧率 (burn rate) 计算
- 错误预算 (error budget) 跟踪
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional



@dataclass
class SLOTarget:
    """SLO 目标"""
    availability: float = 0.999        # 99.9%
    p99_latency_ms: float = 500       # P99 < 500ms
    error_rate: float = 0.01          # 错误率 < 1%
    throughput_rps: float = 100       # 100 req/s


@dataclass
class SLOMeasurement:
    """SLO 测量值"""
    timestamp: float
    success: bool
    latency_ms: float
    error_code: str | None = None


class SLOTracker:
    """SLO 追踪器

    用法:
        tracker = SLOTracker(SLOTarget())
        tracker.record(SLOMeasurement(timestamp=time.time(), success=True, latency_ms=120))
        burn = tracker.burn_rate()
        if burn > 1:
            alert("SLO burning too fast")
    """

    def __init__(self, target: SLOTarget, window: int = 3600) -> None:
        """初始化 SLO 追踪器.

        Args:
            target: SLO 目标配置
            window: 统计窗口秒数, 默认 3600
        """
        self.target = target
        self._window = window
        self._measurements: deque = deque(maxlen=10000)
        self._errors: deque = deque(maxlen=10000)
        self._latencies: deque = deque(maxlen=10000)
        self._started_at = time.time()

    def record(self, m: SLOMeasurement) -> None:
        """记录一次请求测量值.

        Args:
            m: SLO 测量值 (成功/延迟/错误码)
        """
        self._measurements.append(m)
        if not m.success:
            self._errors.append(m.timestamp)
        self._latencies.append(m.latency_ms)

    def availability(self) -> float:
        """返回当前可用率 (0.0~1.0)."""
        if not self._measurements:
            return 1.0
        ok = sum(1 for m in self._measurements if m.success)
        return ok / len(self._measurements)

    def error_rate(self) -> float:
        """返回当前错误率 (0.0~1.0)."""
        if not self._measurements:
            return 0.0
        errs = sum(1 for m in self._measurements if not m.success)
        return errs / len(self._measurements)

    def p99_latency(self) -> float:
        """返回 P99 延迟 (毫秒)."""
        if not self._latencies:
            return 0.0
        sorted_l = sorted(self._latencies)
        idx = int(len(sorted_l) * 0.99)
        return sorted_l[min(idx, len(sorted_l) - 1)]

    def throughput(self) -> float:
        """返回当前吞吐量 (req/s)."""
        if not self._measurements:
            return 0.0
        elapsed = max(1.0, time.time() - self._started_at)
        return len(self._measurements) / elapsed

    def burn_rate(self) -> float:
        """燃烧率: 1 = 按预算消耗, >1 = 过快, <1 = 健康"""
        budget = 1.0 - self.target.availability
        actual = 1.0 - self.availability()
        if budget <= 0:
            return float('inf') if actual > 0 else 0.0
        return actual / budget

    def error_budget_remaining(self) -> float:
        """错误预算剩余比例"""
        budget = 1.0 - self.target.availability
        if budget <= 0:
            return 0.0
        used = 1.0 - self.availability()
        return max(0.0, (budget - used) / budget)

    def health(self) -> dict:
        """返回 SLO 健康状态摘要 (各指标实际值/目标值/燃烧率/预算)."""
        return {
            "availability": self.availability(),
            "target_availability": self.target.availability,
            "p99_latency_ms": self.p99_latency(),
            "target_p99_ms": self.target.p99_latency_ms,
            "error_rate": self.error_rate(),
            "target_error_rate": self.target.error_rate,
            "throughput_rps": self.throughput(),
            "target_throughput": self.target.throughput_rps,
            "burn_rate": self.burn_rate(),
            "error_budget_remaining": self.error_budget_remaining(),
            "sample_count": len(self._measurements),
        }


# ============================================================
# 三级限流: 全局 / 用户 / 端点
# ============================================================

class TokenBucket:
    """令牌桶算法"""

    def __init__(self, capacity: float, refill_rate: float) -> None:
        """初始化令牌桶.

        Args:
            capacity: 桶容量 (允许的突发量)
            refill_rate: 每秒补充令牌数
        """
        self.capacity = capacity
        self.refill_rate = refill_rate  # tokens per second
        self.tokens = capacity
        self.last_refill = time.time()
        self._lock = asyncio.Lock()

    async def consume(self, n: float = 1.0) -> bool:
        """消耗 n 个令牌, 不够则返回 False.

        Args:
            n: 需要消耗的令牌数, 默认 1.0

        Returns:
            True 表示成功获取令牌, False 表示被限流
        """
        async with self._lock:
            now = time.time()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now
            if self.tokens >= n:
                self.tokens -= n
                return True
            return False


class RateLimiter:
    """三级限流器

    用法:
        rl = RateLimiter()
        rl.set_global(100)         # 全局 100 rps
        rl.set_user("u1", 10)      # 用户 u1 10 rps
        rl.set_endpoint("/api/v1/chat", 30)  # 端点 30 rps

        if await rl.allow(user_id="u1", endpoint="/api/v1/chat"):
            handle_request()
        else:
            return 429
    """

    def __init__(self) -> None:
        """初始化三级限流器 (无默认限流)."""
        self._global_bucket: TokenBucket | None = None
        self._user_buckets: dict[str, TokenBucket] = {}
        self._endpoint_buckets: dict[str, TokenBucket] = {}
        self._rejected_count = 0
        self._allowed_count = 0

    def set_global(self, rps: float) -> None:
        """设置全局速率限制.

        Args:
            rps: 每秒允许请求数
        """
        self._global_bucket = TokenBucket(capacity=rps * 2, refill_rate=rps)

    def set_user(self, user_id: str, rps: float) -> None:
        """为指定用户设置速率限制.

        Args:
            user_id: 用户标识
            rps: 每秒允许请求数
        """
        self._user_buckets[user_id] = TokenBucket(capacity=rps * 2, refill_rate=rps)

    def set_endpoint(self, endpoint: str, rps: float) -> None:
        """为指定端点设置速率限制.

        Args:
            endpoint: 端点路径
            rps: 每秒允许请求数
        """
        self._endpoint_buckets[endpoint] = TokenBucket(capacity=rps * 2, refill_rate=rps)

    async def allow(self, user_id: str = "", endpoint: str = "") -> bool:
        """检查是否允许通过"""
        checks = []
        if self._global_bucket:
            checks.append(self._global_bucket.consume(1.0))
        if user_id and user_id in self._user_buckets:
            checks.append(self._user_buckets[user_id].consume(1.0))
        if endpoint and endpoint in self._endpoint_buckets:
            checks.append(self._endpoint_buckets[endpoint].consume(1.0))

        if not checks:
            self._allowed_count += 1
            return True

        results = await asyncio.gather(*checks)
        if all(results):
            self._allowed_count += 1
            return True
        self._rejected_count += 1
        return False

    def stats(self) -> dict:
        """返回限流统计 (允许数/拒绝数/各级别限流配置数)."""
        return {
            "allowed": self._allowed_count,
            "rejected": self._rejected_count,
            "global_limit": self._global_bucket is not None,
            "user_limits": len(self._user_buckets),
            "endpoint_limits": len(self._endpoint_buckets),
        }


# 全局单例
_slo: SLOTracker | None = None
_rl: RateLimiter | None = None


def get_slo_tracker() -> SLOTracker:
    """获取全局 SLO 追踪器单例."""
    global _slo
    if _slo is None:
        _slo = SLOTracker(SLOTarget())
    return _slo


def get_rate_limiter() -> RateLimiter:
    """获取全局三级限流器单例 (默认全局 100 rps)."""
    global _rl
    if _rl is None:
        _rl = RateLimiter()
        _rl.set_global(100)  # 默认全局 100 rps
    return _rl
