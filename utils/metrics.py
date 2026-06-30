import time
from collections import defaultdict
from loguru import logger


class Metrics:
    def __init__(self) -> None:
        """初始化指标采集器 (counter/timer/gauge/histogram)."""
        self._counters = defaultdict(int)
        self._timers = defaultdict(list)
        self._gauges = defaultdict(float)
        self._histograms = defaultdict(list)
        self._last_report = time.time()
        # F6: 限制指标键数量，防止动态键导致内存无限增长
        self._max_keys = 500

    def inc(self, name: str, value: int = 1) -> None:
        """递增计数器.

        Args:
            name: 指标名
            value: 增量, 默认 1
        """
        # F6: 超出键数量上限时不再记录新键（已有键正常递增）
        if name not in self._counters and len(self._counters) >= self._max_keys:
            return
        self._counters[name] += value

    def observe(self, name: str, duration: float) -> None:
        """记录耗时样本 (保留最近 100 个).

        Args:
            name: 指标名
            duration: 耗时秒数
        """
        # F6: 超出键数量上限时不再记录新键
        if name not in self._timers and len(self._timers) >= self._max_keys:
            return
        self._timers[name].append(duration)
        if len(self._timers[name]) > 100:
            self._timers[name] = self._timers[name][-100:]

    def gauge(self, name: str, value: float) -> None:
        """设置仪表盘指标（最新值覆盖）"""
        # F6: 超出键数量上限时不再记录新键
        if name not in self._gauges and len(self._gauges) >= self._max_keys:
            return
        self._gauges[name] = value

    def histogram(self, name: str, value: float) -> None:
        """记录直方图样本（保留最近 200 个）"""
        # F6: 超出键数量上限时不再记录新键
        if name not in self._histograms and len(self._histograms) >= self._max_keys:
            return
        self._histograms[name].append(value)
        if len(self._histograms[name]) > 200:
            self._histograms[name] = self._histograms[name][-200:]

    def get_snapshot(self) -> dict:
        """获取当前指标快照（用于 /debug 命令和持久化）"""
        snapshot = {
            "timestamp": time.time(),
            "counters": dict(self._counters),
            "gauges": dict(self._gauges),
        }
        for name, durations in self._timers.items():
            if durations:
                snapshot[f"timer.{name}"] = {
                    "avg": round(sum(durations) / len(durations), 3),
                    "p95": round(sorted(durations)[int(len(durations) * 0.95)], 3) if len(durations) >= 2 else round(durations[0], 3),
                    "samples": len(durations),
                }
        for name, values in self._histograms.items():
            if values:
                snapshot[f"hist.{name}"] = {
                    "min": round(min(values), 3),
                    "max": round(max(values), 3),
                    "avg": round(sum(values) / len(values), 3),
                    "samples": len(values),
                }
        return snapshot

    def maybe_report(self, interval: float = 300) -> None:
        """按间隔阈值输出指标快照日志.

        Args:
            interval: 最小报告间隔秒, 默认 300
        """
        now = time.time()
        if now - self._last_report < interval:
            return
        self._last_report = now
        for name, count in self._counters.items():
            logger.info(f"metrics.{name}", count=count)
        for name, durations in self._timers.items():
            if durations:
                avg = sum(durations) / len(durations)
                logger.info(f"metrics.{name}.avg_seconds", avg=round(avg, 3), samples=len(durations))
        for name, value in self._gauges.items():
            logger.info(f"metrics.{name}", value=round(value, 3))
        self._counters.clear()
        self._timers.clear()
        self._gauges.clear()
        # 注意：histogram 不清除，保留用于趋势分析


# 全局单例
metrics = Metrics()
