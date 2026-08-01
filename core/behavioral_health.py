"""行为健康评分 (Behavioral Health Score, BHS) — 精简版

5 级健康度评分系统, 用于自检/降级/恢复决策。
合并自 behavioral_direction.py 和 behavioral_signal.py。

评分维度:
- 响应延迟 (p50/p99)
- 成功率
- 错误率
- 内存使用
- 工具成功率
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import IntEnum

from loguru import logger


class HealthLevel(IntEnum):
    """5 级健康度"""
    EXCELLENT = 5
    GOOD = 4
    FAIR = 3
    POOR = 2
    CRITICAL = 1


@dataclass
class HealthScore:
    """健康评分结果"""
    score: int
    level: HealthLevel
    factors: dict = field(default_factory=dict)
    recommendations: list = field(default_factory=list)


# ── 兼容: 行为信号流 (精简) ──────────────────────────────

@dataclass
class SignalEntry:
    """单条行为信号 (兼容旧接口)"""
    signal_type: str
    value: float
    source: str = ""
    timestamp: float = 0.0
    meta: dict = field(default_factory=dict)


class BehavioralSignalStream:
    """行为信号流 (精简兼容层) — 保留 emit/aggregate 接口"""
    def __init__(self, max_history: int = 1000) -> None:
        self._buffer: list[SignalEntry] = []

    async def emit(self, signal_type: str, value: float, source: str = "", **meta) -> None:
        self._buffer.append(SignalEntry(signal_type, value, source, meta=meta))
        if len(self._buffer) > 1000:
            self._buffer = self._buffer[-500:]

    def aggregate(self, signal_type: str, strategy: str = "mean_of_means") -> float:
        entries = [e for e in self._buffer if e.signal_type == signal_type]
        if not entries:
            return 0.0
        values = [e.value for e in entries]
        return sum(values) / len(values)


# ── 兼容: 行为方向向量 (精简) ────────────────────────────

@dataclass
class DirectionVector:
    """行为方向向量 (兼容旧接口)"""
    name: str
    dimensions: dict = field(default_factory=dict)
    source: str = ""
    magnitude: float = 1.0
    meta: dict = field(default_factory=dict)

    def apply_to_context(self, context: dict) -> dict:
        result = dict(context)
        # 维度映射: emotion→emotion_offset, prompt→prompt_modifier, tool→tool_bias, route→route_bias
        if "emotion" in self.dimensions:
            result["emotion_offset"] = self.dimensions["emotion"]
        if "prompt" in self.dimensions:
            result["prompt_modifier"] = self.dimensions["prompt"]
        if "tool" in self.dimensions:
            result["tool_bias"] = self.dimensions["tool"]
        if "route" in self.dimensions:
            result["route_bias"] = self.dimensions["route"]
        for k, v in self.dimensions.items():
            if k not in ("emotion", "prompt", "tool", "route"):
                result[k] = v
        return result

    def __mul__(self, scalar: float) -> "DirectionVector":
        return DirectionVector(
            name=self.name,
            dimensions={k: v * scalar for k, v in self.dimensions.items()},
            source=self.source,
            magnitude=self.magnitude * scalar,
            meta=dict(self.meta),
        )

    def __add__(self, other: "DirectionVector") -> "DirectionVector":
        merged_dims = dict(self.dimensions)
        for k, v in other.dimensions.items():
            merged_dims[k] = merged_dims.get(k, 0.0) + v
        return DirectionVector(
            name=f"{self.name}+{other.name}",
            dimensions=merged_dims,
            source="merged",
            magnitude=1.0,
            meta=dict(self.meta),
        )


class DirectionRegistry:
    """方向向量注册表 (兼容旧接口)"""
    def __init__(self, storage_path: str = "") -> None:
        self._directions: dict[str, DirectionVector] = {}

    def register(self, direction: DirectionVector) -> None:
        self._directions[direction.name] = direction

    def get(self, name: str) -> DirectionVector | None:
        return self._directions.get(name)

    def list_directions(self) -> list[str]:
        return list(self._directions.keys())


# ── 核心评分器 ──────────────────────────────────────────

class BehavioralHealthScorer:
    """行为健康评分器 (精简版)"""

    @staticmethod
    def _score_latency_ms(latency_ms: float) -> int:
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
        if usage < 0.50:
            return 5
        if usage < 0.70:
            return 4
        if usage < 0.85:
            return 3
        if usage < 0.95:
            return 2
        return 1

    _DIMENSION_TABLE = (
        ("p50_latency_ms",    _score_latency_ms.__func__),
        ("p99_latency_ms",    _score_latency_ms.__func__),
        ("success_rate",      _score_success_rate.__func__),
        ("error_rate",        _score_error_rate.__func__),
        ("memory_usage",      _score_memory_usage.__func__),
        ("tool_success_rate", _score_success_rate.__func__),
    )

    def calculate(self, metrics: dict) -> HealthScore:
        """计算综合健康评分"""
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
            score_val = 5
        else:
            avg = sum(s for _, s, _ in per_dimension) / len(per_dimension)
            score_val = max(1, min(5, round(avg)))

        level = HealthLevel(score_val)
        recs = self._build_recommendations(level, per_dimension)

        return HealthScore(score=score_val, level=level, factors=factors, recommendations=recs)

    def get_recommendations(self, score: HealthScore) -> list[str]:
        return list(score.recommendations)

    @staticmethod
    def _build_recommendations(level: HealthLevel,
                                per_dimension: list[tuple[str, int, float]]) -> list[str]:
        recs: list[str] = []
        if level == HealthLevel.EXCELLENT:
            recs.append("状态优秀, 保持当前运行参数")
        elif level == HealthLevel.GOOD:
            recs.append("整体良好, 关注次要指标的小幅波动")
        elif level == HealthLevel.FAIR:
            recs.append("出现明显退化, 建议检查近期错误日志和资源占用")
            recs.append("考虑启用降级模式")
        elif level == HealthLevel.POOR:
            recs.append("严重退化, 建议立即重启受影响模块")
            recs.append("启用 circuit breaker / fallback 策略")
        elif level == HealthLevel.CRITICAL:
            recs.append("濒临崩溃, 建议立即停止接收新请求")
            recs.append("通知人工介入")

        for name, s, v in per_dimension:
            if s <= 2:
                recs.append(f"维度 [{name}] 评分较低 ({s}/5), 当前值={v}")
        return recs

    def calculate_from_runtime(self) -> HealthScore:
        """从运行时指标自动计算健康评分"""
        return self.calculate(self._collect_runtime_metrics())

    def _collect_runtime_metrics(self) -> dict:
        """采集当前运行时指标"""
        metrics: dict = {}
        try:
            try:
                from core.slo_tracker import get_slo_tracker
            except ImportError:
                return metrics
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

        try:
            import psutil
            proc = psutil.Process(os.getpid())
            rss = proc.memory_info().rss
            vm = psutil.virtual_memory().total
            if vm:
                metrics["memory_usage"] = rss / vm
        except Exception:
            pass
        return metrics


# 全局单例
_scorer: BehavioralHealthScorer | None = None


def get_behavioral_health_scorer() -> BehavioralHealthScorer:
    """获取全局 BehavioralHealthScorer 单例"""
    global _scorer
    if _scorer is None:
        _scorer = BehavioralHealthScorer()
    return _scorer
