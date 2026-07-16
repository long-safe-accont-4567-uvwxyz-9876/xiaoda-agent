"""行为健康评分 (Behavioral Health Score, BHS) — Dr2 P1 Doctor

5 级健康度评分系统, 用于自检/降级/恢复决策:
- EXCELLENT (5): 一切正常, 响应快, 成功率高
- GOOD (4):      轻微波动, 但整体正常
- FAIR (3):      明显退化, 部分功能受影响
- POOR (2):      严重退化, 多个功能异常
- CRITICAL (1):  濒临崩溃, 基本功能受影响

评分维度:
- 响应延迟 (p50/p99)
- 成功率
- 错误率
- 内存使用
- 工具成功率

用法:
    scorer = BehavioralHealthScorer()
    score = scorer.calculate({
        "p50_latency_ms": 800,
        "p99_latency_ms": 2500,
        "success_rate": 0.97,
        "error_rate": 0.01,
        "memory_usage": 0.55,
        "tool_success_rate": 0.98,
    })
    print(score.level.name, score.score, score.recommendations)
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from enum import IntEnum

from loguru import logger

_pending_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> asyncio.Task:
    """安全创建后台 Task，持有引用防止 GC 回收。"""
    task = asyncio.create_task(coro)
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)
    return task

# J-Space Hook: 行为信号流采集 (非阻塞, 失败不影响主流程)
try:
    from config import ENABLE_J_SPACE_HOOKS
    if ENABLE_J_SPACE_HOOKS:
        from core.behavioral_signal import BehavioralSignalStream
        _signal_stream: "BehavioralSignalStream | None" = None
    else:
        _signal_stream = None
except ImportError:
    _signal_stream = None


class HealthLevel(IntEnum):
    """5 级健康度 (使用 IntEnum 便于数值比较)"""
    EXCELLENT = 5  # 一切正常, 响应快, 成功率高
    GOOD = 4       # 轻微波动, 但整体正常
    FAIR = 3       # 明显退化, 部分功能受影响
    POOR = 2       # 严重退化, 多个功能异常
    CRITICAL = 1   # 濒临崩溃, 基本功能受影响


@dataclass
class HealthScore:
    """健康评分结果"""
    score: int                                          # 1-5
    level: HealthLevel                                  # 对应级别
    factors: dict = field(default_factory=dict)         # metric -> value (原始值)
    recommendations: list = field(default_factory=list)  # 建议列表


class BehavioralHealthScorer:
    """行为健康评分器

    通过多维度指标计算综合健康度, 并给出可执行建议。
    """

    def __init__(self) -> None:
        self._monitor_task: asyncio.Task | None = None

    # ── 维度评分阈值表 ──

    @staticmethod
    def _score_latency_ms(latency_ms: float) -> int:
        """响应延迟评分: <1s=5, <3s=4, <5s=3, <10s=2, >10s=1"""
        if latency_ms < 1000:
            return 5
        if latency_ms < 3000:
            return 4
        if latency_ms < 5000:
            return 3
        if latency_ms < 10000:
            return 2
        return 1

    @staticmethod
    def _score_success_rate(rate: float) -> int:
        """成功率评分: >95%=5, >90%=4, >80%=3, >70%=2, <70%=1"""
        if rate > 0.95:
            return 5
        if rate > 0.90:
            return 4
        if rate > 0.80:
            return 3
        if rate > 0.70:
            return 2
        return 1

    @staticmethod
    def _score_error_rate(rate: float) -> int:
        """错误率评分: <1%=5, <5%=4, <10%=3, <20%=2, >20%=1"""
        if rate < 0.01:
            return 5
        if rate < 0.05:
            return 4
        if rate < 0.10:
            return 3
        if rate < 0.20:
            return 2
        return 1

    @staticmethod
    def _score_memory_usage(usage: float) -> int:
        """内存使用评分: <50%=5, <70%=4, <85%=3, <95%=2, >95%=1"""
        if usage < 0.50:
            return 5
        if usage < 0.70:
            return 4
        if usage < 0.85:
            return 3
        if usage < 0.95:
            return 2
        return 1

    # ── 维度评分映射表 ──
    # (metric_key, score_fn) — 顺序与文档保持一致
    _DIMENSION_TABLE = (
        ("p50_latency_ms",       _score_latency_ms.__func__),
        ("p99_latency_ms",       _score_latency_ms.__func__),
        ("success_rate",         _score_success_rate.__func__),
        ("error_rate",           _score_error_rate.__func__),
        ("memory_usage",         _score_memory_usage.__func__),
        ("tool_success_rate",    _score_success_rate.__func__),  # 工具成功率复用成功率阈值
    )

    def calculate(self, metrics: dict) -> HealthScore:
        """计算综合健康评分

        Args:
            metrics: 指标字典, 支持字段:
                - p50_latency_ms / p99_latency_ms: 响应延迟 (毫秒)
                - success_rate: 成功率 (0-1)
                - error_rate: 错误率 (0-1)
                - memory_usage: 内存使用率 (0-1)
                - tool_success_rate: 工具成功率 (0-1)

        Returns:
            HealthScore, 包含综合分/级别/原始 factors/建议
        """
        factors: dict = {}
        per_dimension: list[tuple[str, int, float]] = []

        for key, score_fn in self._DIMENSION_TABLE:
            if key in metrics:
                try:
                    raw = float(metrics[key])
                except (TypeError, ValueError):
                    continue
                s = score_fn(raw)
                per_dimension.append((key, s, raw))
                factors[key] = raw

        if not per_dimension:
            # 无可用指标, 默认 EXCELLENT (避免误报)
            score_val = 5
        else:
            # 综合分 = 各维度平均, 取整
            avg = sum(s for _, s, _ in per_dimension) / len(per_dimension)
            score_val = max(1, min(5, round(avg)))

        level = HealthLevel(score_val)
        recs = self._build_recommendations(level, per_dimension)

        # J-Space Hook: emit health signal (non-blocking)
        if _signal_stream is not None:
            try:
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    pass
                else:
                    _fire_and_forget(_signal_stream.emit(
                        "health", float(score_val) / 5.0, "behavioral_health"))
            except Exception as e:
                logger.debug("behavioral_health.signal_emit_failed", error=str(e))

        return HealthScore(
            score=score_val,
            level=level,
            factors=factors,
            recommendations=recs,
        )

    def get_recommendations(self, score: HealthScore) -> list[str]:
        """根据评分给出建议 (对外暴露接口)"""
        return list(score.recommendations)

    @staticmethod
    def _build_recommendations(level: HealthLevel,
                                per_dimension: list[tuple[str, int, float]]) -> list[str]:
        """根据综合级别 + 维度评分生成建议"""
        recs: list[str] = []

        if level == HealthLevel.EXCELLENT:
            recs.append("状态优秀, 保持当前运行参数")
        elif level == HealthLevel.GOOD:
            recs.append("整体良好, 关注次要指标的小幅波动")
        elif level == HealthLevel.FAIR:
            recs.append("出现明显退化, 建议检查近期错误日志和资源占用")
            recs.append("考虑启用降级模式 (degradation_mode)")
        elif level == HealthLevel.POOR:
            recs.append("严重退化, 建议立即重启受影响模块")
            recs.append("启用 circuit breaker / fallback 策略")
            recs.append("排查内存泄漏或长尾请求")
        elif level == HealthLevel.CRITICAL:
            recs.append("濒临崩溃, 建议立即停止接收新请求")
            recs.append("触发 RecoveryOrchestrator 进入 RESTART 级别")
            recs.append("通知人工介入 (escalate)")

        # 维度级建议 (评分为 1 或 2 的维度单独提示)
        for name, s, v in per_dimension:
            if s <= 2:
                recs.append(f"维度 [{name}] 评分较低 ({s}/5), 当前值={v}")
        return recs

    def calculate_from_runtime(self) -> HealthScore:
        """从运行时指标自动计算健康评分（公共接口，封装采集+计算两步）。"""
        return self.calculate(self._collect_runtime_metrics())

    # ── 周期性监控 ──

    def start_monitoring(self, interval: int = 60) -> asyncio.Task | None:
        """启动周期性评分 (后台 task)

        Args:
            interval: 评分间隔 (秒), 默认 60

        Returns:
            asyncio.Task, 调用方可 cancel()。若当前没有 event loop, 返回 None。
        """
        if self._monitor_task and not self._monitor_task.done():
            return self._monitor_task

        async def _loop() -> None:
            while True:
                try:
                    metrics = self._collect_runtime_metrics()
                    score = self.calculate(metrics)
                    logger.info(
                        f"BHS.monitor level={score.level.name} score={score.score} "
                        f"factors={score.factors}"
                    )
                    if score.level <= HealthLevel.POOR:
                        for rec in score.recommendations:
                            logger.warning(f"BHS.recommendation: {rec}")
                except Exception as e:
                    logger.warning(f"BHS.monitor_error: {e}")
                await asyncio.sleep(interval)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        self._monitor_task = loop.create_task(_loop())
        logger.info(f"BHS.monitoring_started interval={interval}s")
        return self._monitor_task

    def stop_monitoring(self) -> None:
        """停止健康监控循环。"""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._monitor_task = None

    def _collect_runtime_metrics(self) -> dict:
        """采集当前运行时指标 (从 SLO tracker / psutil)

        在事件循环或后台调用中执行, 失败时返回空字典 (calculate 会默认 EXCELLENT)。
        """
        metrics: dict = {}
        # SLO 指标
        try:
            from core.slo_tracker import get_slo_tracker
            slo = get_slo_tracker()
            p99 = slo.p99_latency()
            if p99:
                metrics["p99_latency_ms"] = float(p99)
            p50 = slo.p50_latency() if hasattr(slo, "p50_latency") else None
            if p50:
                metrics["p50_latency_ms"] = float(p50)
            err_rate = slo.error_rate()
            metrics["error_rate"] = float(err_rate)
            metrics["success_rate"] = max(0.0, 1.0 - float(err_rate))
        except Exception as e:
            logger.debug(f"BHS.collect_slo_failed: {e}")

        # 内存使用
        try:
            import psutil
            proc = psutil.Process(os.getpid())
            rss = proc.memory_info().rss
            vm = psutil.virtual_memory().total
            if vm:
                metrics["memory_usage"] = rss / vm
        except Exception as e:
            logger.debug(f"BHS.collect_memory_failed: {e}")
        return metrics


# 全局单例
_scorer: BehavioralHealthScorer | None = None


def get_behavioral_health_scorer() -> BehavioralHealthScorer:
    """获取全局 BehavioralHealthScorer 单例"""
    global _scorer
    if _scorer is None:
        _scorer = BehavioralHealthScorer()
    return _scorer