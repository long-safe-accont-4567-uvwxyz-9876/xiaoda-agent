"""自我诊断与主动报告 (A6) — 主动报告异常

参考:
- Self-Awareness in LLM Agents (Anthropic Constitutional AI)
- SLO-based self-reporting

特性:
- 周期性自检: 性能 / 错误率 / 资源 / 行为
- 主动报告: 异常时通过 WebUI/IM 通知 owner
- 不阻塞主流程
- 报告分级: info / warning / critical
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from loguru import logger


class ReportLevel(str, Enum):
    """自我诊断报告级别枚举，标识信息/警告/严重三档。"""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class SelfReport:
    """自我报告"""
    level: ReportLevel
    category: str             # performance / error / resource / behavior
    message: str
    metrics: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    resolved: bool = False


class SelfDiagnostic:
    """自我诊断器

    用法:
        diag = SelfDiagnostic()
        diag.start(interval=60)  # 每 60s 自检一次
        # 注册报告处理器
        diag.on_report(lambda r: print(r.level, r.message))
    """

    def __init__(self, check_interval: float = 60.0) -> None:
        self._interval = check_interval
        self._reports: deque = deque(maxlen=200)
        self._callbacks: list[Callable[[SelfReport], None]] = []
        self._task: Optional[asyncio.Task] = None
        self._checks: list[Callable] = []
        self._last_check_at = 0
        self._consecutive_failures = 0

        # 注册内置检查
        self._checks.append(self._check_error_rate)
        self._checks.append(self._check_response_time)
        self._checks.append(self._check_memory_usage)
        self._checks.append(self._check_active_state)

    def add_check(self, check: Callable[[], Optional[SelfReport]]) -> None:
        """注册自定义检查"""
        self._checks.append(check)

    def on_report(self, callback: Callable[[SelfReport], None]) -> None:
        """注册报告回调"""
        self._callbacks.append(callback)

    async def _check_error_rate(self) -> Optional[SelfReport]:
        """检查错误率"""
        try:
            from core.slo_tracker import get_slo_tracker
            slo = get_slo_tracker()
            err_rate = slo.error_rate()
            target = slo.target.error_rate
            if err_rate > target * 2:
                return SelfReport(
                    level=ReportLevel.CRITICAL,
                    category="error",
                    message=f"Error rate {err_rate:.1%} exceeds 2x target {target:.1%}",
                    metrics={"error_rate": err_rate, "target": target},
                )
            if err_rate > target:
                return SelfReport(
                    level=ReportLevel.WARNING,
                    category="error",
                    message=f"Error rate {err_rate:.1%} above target {target:.1%}",
                    metrics={"error_rate": err_rate, "target": target},
                )
        except Exception as e:
            logger.debug(f"SelfDiag.error_rate_check_failed: {e}")
        return None

    async def _check_response_time(self) -> Optional[SelfReport]:
        """检查响应时间"""
        try:
            from core.slo_tracker import get_slo_tracker
            slo = get_slo_tracker()
            p99 = slo.p99_latency()
            target = slo.target.p99_latency_ms
            if p99 > target * 2:
                return SelfReport(
                    level=ReportLevel.CRITICAL,
                    category="performance",
                    message=f"P99 latency {p99:.0f}ms exceeds 2x target {target:.0f}ms",
                    metrics={"p99_ms": p99, "target_ms": target},
                )
            if p99 > target:
                return SelfReport(
                    level=ReportLevel.WARNING,
                    category="performance",
                    message=f"P99 latency {p99:.0f}ms above target {target:.0f}ms",
                    metrics={"p99_ms": p99, "target_ms": target},
                )
        except Exception as e:
            logger.debug(f"SelfDiag.latency_check_failed: {e}")
        return None

    async def _check_memory_usage(self) -> Optional[SelfReport]:
        """检查内存使用"""
        try:
            import psutil
            proc = psutil.Process()
            mem = proc.memory_info().rss / 1024 / 1024  # MB
            if mem > 1024:  # > 1GB
                return SelfReport(
                    level=ReportLevel.WARNING,
                    category="resource",
                    message=f"Memory usage high: {mem:.0f}MB",
                    metrics={"memory_mb": mem},
                )
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"SelfDiag.memory_check_failed: {e}")
        return None

    async def _check_active_state(self) -> Optional[SelfReport]:
        """检查活跃状态 (是否进入 zombie)"""
        try:
            from doctor.behavioral_health import get_behavioral_health
            bh = get_behavioral_health()
            score = bh.score()
            if score and score.get("level") == "critical":
                return SelfReport(
                    level=ReportLevel.CRITICAL,
                    category="behavior",
                    message=f"Behavioral health critical: {score.get('score', 0):.2f}",
                    metrics=score,
                )
        except Exception as e:
            logger.debug(f"SelfDiag.bh_check_failed: {e}")
        return None

    async def run_checks(self) -> list[SelfReport]:
        """执行所有检查"""
        reports = []
        for check in self._checks:
            try:
                r = check()
                if asyncio.iscoroutine(r):
                    r = await r
                if r:
                    reports.append(r)
                    self._reports.append(r)
                    self._notify(r)
            except Exception as e:
                self._consecutive_failures += 1
                logger.warning(f"SelfDiag.check_failed: {e}")
        self._last_check_at = time.time()
        return reports

    def _notify(self, report: SelfReport) -> None:
        """通知所有回调"""
        log_fn = (logger.info if report.level == ReportLevel.INFO else
                    logger.warning if report.level == ReportLevel.WARNING else
                    logger.error)
        log_fn(f"SelfDiag.report level={report.level.value} "
                f"category={report.category} msg={report.message}")
        for cb in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    _diag_cb = asyncio.create_task(cb(report))  # noqa: RUF006
                else:
                    cb(report)
            except Exception as e:
                logger.warning(f"SelfDiag.callback_failed: {e}")

    def start(self) -> Optional[asyncio.Task]:
        """启动周期性自检"""
        async def _loop() -> None:
            while True:
                try:
                    await self.run_checks()
                except Exception as e:
                    logger.error(f"SelfDiag.loop_error: {e}")
                await asyncio.sleep(self._interval)

        try:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(_loop())
            logger.info(f"SelfDiag.started interval={self._interval}s")
            return self._task
        except RuntimeError:
            return None

    def stop(self) -> None:
        """取消周期性自检任务."""
        if self._task:
            self._task.cancel()
            self._task = None

    def get_recent_reports(self, limit: int = 20) -> list[SelfReport]:
        """返回最近 N 条自我报告.

        Args:
            limit: 返回的最大报告数, 默认 20

        Returns:
            最近的自检报告列表 (按时间升序)
        """
        return list(self._reports)[-limit:]

    def stats(self) -> dict:
        """返回自检统计 (含最近检查时间/报告数/失败次数/分级计数)."""
        return {
            "last_check_at": self._last_check_at,
            "total_reports": len(self._reports),
            "consecutive_failures": self._consecutive_failures,
            "critical_count": sum(1 for r in self._reports if r.level == ReportLevel.CRITICAL),
            "warning_count": sum(1 for r in self._reports if r.level == ReportLevel.WARNING),
        }


# 全局单例
_diag: Optional[SelfDiagnostic] = None


def get_self_diagnostic() -> SelfDiagnostic:
    """获取全局 SelfDiagnostic 单例, 不存在时创建."""
    global _diag
    if _diag is None:
        _diag = SelfDiagnostic()
    return _diag