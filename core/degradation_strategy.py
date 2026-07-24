"""Q4 降级策略标准化 — 4 级降级 (P1 服务质量)

参考:
- core/recovery_orchestrator.py (6 级恢复编排)
- core/degradation_detector.py (三轴退化检测)
- core/slo_tracker.py (SLO 燃烧率)
- Google SRE: 优雅降级 (Graceful Degradation)

4 级降级 (IntEnum):
0. L0_NORMAL     (0): 正常运行, 全功能
1. L1_DEGRADED   (1): 轻度降级, 非核心功能关闭 (TTS, 表情包), 核心对话保留
2. L2_MINIMAL    (2): 最小化运行, 只保留文本对话, 所有增强功能关闭
3. L3_EMERGENCY  (3): 紧急模式, 只保留最基础响应 (固定回复模板), 用于系统濒临崩溃

特性:
- 功能级别映射 (feature_map): {功能名: 最高可用级别}
  - 功能在 current_level <= 最高可用级别 时可用
- 级别变化回调 (on_level_change)
- 自动触发: 集成 DegradationDetector / SLOTracker (只读取状态)
- 恢复机制: recover() 逐级回升 L3→L2→L1→L0

用法:
    strat = DegradationStrategy()
    strat.trigger(DegradationLevel.L1_DEGRADED, reason="延迟过高")
    if strat.is_feature_available("tts"):
        ...  # 执行 TTS
    strat.recover()  # 尝试恢复到上一级
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from loguru import logger

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


# ============================================================
# 4 级降级枚举
# ============================================================

class DegradationLevel(IntEnum):
    """4 级降级级别 (值越小越接近正常)"""
    L0_NORMAL = 0       # 正常运行, 全功能
    L1_DEGRADED = 1     # 轻度降级, 非核心功能关闭
    L2_MINIMAL = 2       # 最小化运行, 只保留文本对话
    L3_EMERGENCY = 3    # 紧急模式, 只保留基础响应


# ============================================================
# 数据结构
# ============================================================

@dataclass
class LevelChangeEvent:
    """级别变化事件 (传给回调)"""
    old_level: DegradationLevel
    new_level: DegradationLevel
    reason: str
    timestamp: float = field(default_factory=time.time)
    # 触发来源: manual / detector / slo_tracker
    source: str = "manual"

    def to_dict(self) -> dict:
        """将级别变化事件序列化为字典."""
        return {
            "old_level": self.old_level.name,
            "new_level": self.new_level.name,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "source": self.source,
        }


# ============================================================
# 降级策略
# ============================================================

# 功能 → 最高可用级别 (功能在 current_level <= 该级别时可用)
# 例: "tts": L0 表示 TTS 仅在 L0 正常模式可用;
#      "web_browse": L1 表示 L0/L1 可用, L2+ 关闭;
#      "basic_response": L3 表示任意级别可用 (含紧急模式)
DEFAULT_FEATURE_MAP: dict[str, DegradationLevel] = {
    "tts": DegradationLevel.L0_NORMAL,
    "emotion": DegradationLevel.L0_NORMAL,           # 表情包
    "web_browse": DegradationLevel.L1_DEGRADED,
    "memory_search": DegradationLevel.L1_DEGRADED,
    "text_chat": DegradationLevel.L2_MINIMAL,
    "basic_response": DegradationLevel.L3_EMERGENCY,
}

# 紧急模式固定回复模板 (L3_EMERGENCY 时使用)
EMERGENCY_FALLBACK_REPLY = "人家现在有点不舒服，稍等一下哦～"


class DegradationStrategy:
    """4 级降级策略管理器

    用法:
        strat = DegradationStrategy()
        strat.on_level_change(lambda ev: notify(ev))
        strat.trigger(DegradationLevel.L1_DEGRADED, reason="延迟过高")
        if strat.is_feature_available("tts"):
            synthesize_voice()
        strat.recover()
    """

    def __init__(
        self,
        feature_map: dict[str, DegradationLevel] | None = None,
        initial_level: DegradationLevel = DegradationLevel.L0_NORMAL,
    ) -> None:
        # 拷贝默认功能映射, 避免修改全局常量
        self.feature_map: dict[str, DegradationLevel] = dict(
            feature_map if feature_map is not None else DEFAULT_FEATURE_MAP
        )
        self._level: DegradationLevel = initial_level
        self._reason: str = ""
        self._since: float = time.time()
        self._callbacks: list[Callable[[LevelChangeEvent], None]] = []
        # 触发历史 (审计)
        self._history: list[LevelChangeEvent] = []

    # ─── 属性 ───

    @property
    def current_level(self) -> DegradationLevel:
        """当前降级级别"""
        return self._level

    @property
    def reason(self) -> str:
        """当前降级原因 (L0 时为空串)"""
        return self._reason

    @property
    def since(self) -> float:
        """进入当前级别的时间戳"""
        return self._since

    # ─── 核心操作 ───

    def trigger(
        self,
        level: DegradationLevel,
        reason: str,
        source: str = "manual",
    ) -> None:
        """触发降级到指定级别

        - 若 level 高于当前级别 (更严重), 升级降级
        - 若 level 低于当前级别, 视为恢复, 也会更新状态
        - 若 level == 当前级别, 仅更新原因
        """
        old = self._level
        if level == old:
            # 同级别, 仅更新原因
            self._reason = reason or self._reason
            logger.debug(
                f"Degradation.same_level level={level.name} reason={reason}"
            )
            return
        self._level = level
        self._reason = reason
        self._since = time.time()
        event = LevelChangeEvent(
            old_level=old, new_level=level,
            reason=reason, source=source,
        )
        self._history.append(event)
        # 日志: 升级 warn, 降级 info
        if level > old:
            logger.warning(
                f"Degradation.escalate {old.name} -> {level.name} "
                f"reason={reason} source={source}"
            )
        else:
            logger.info(
                f"Degradation.recover {old.name} -> {level.name} "
                f"reason={reason} source={source}"
            )
        self._fire_callbacks(event)

    def recover(self, source: str = "manual") -> bool:
        """尝试恢复到上一级 (L3→L2→L1→L0)

        返回 True 表示已恢复 (未到 L0), False 表示已在 L0 无法继续恢复。
        """
        if self._level <= DegradationLevel.L0_NORMAL:
            logger.debug("Degradation.recover.already_normal 已在 L0, 无需恢复")
            return False
        prev = DegradationLevel(self._level - 1)
        self.trigger(prev, reason="recover", source=source)
        return True

    # ─── 功能检查 ───

    def is_feature_available(self, feature: str) -> bool:
        """检查功能在当前级别是否可用

        规则: current_level <= feature_map[feature] 时可用。
        未知功能默认可用 (True, fail-open, 避免误关闭现有功能)。
        """
        threshold = self.feature_map.get(feature)
        if threshold is None:
            # 未知功能: 默认可用, 避免破坏未声明的现有功能
            return True
        return self._level <= threshold

    def disabled_features(self) -> list[str]:
        """返回当前级别下被关闭的功能列表"""
        return [
            name for name, threshold in self.feature_map.items()
            if self._level > threshold
        ]

    # ─── 回调 ───

    def on_level_change(
        self, callback: Callable[[LevelChangeEvent], None]
    ) -> None:
        """注册级别变化回调 (每次 trigger/recover 触发)"""
        self._callbacks.append(callback)

    def clear_callbacks(self) -> None:
        """清空所有已注册的级别变化回调."""
        self._callbacks.clear()

    def _fire_callbacks(self, event: LevelChangeEvent) -> None:
        for cb in list(self._callbacks):
            try:
                cb(event)
            except Exception as e:
                logger.error(f"Degradation 回调异常: {e!r}")

    # ─── 状态查询 ───

    def get_status(self) -> dict:
        """返回当前状态 {level, reason, since, disabled_features}"""
        return {
            "level": self._level.name,
            "level_value": int(self._level),
            "reason": self._reason,
            "since": self._since,
            "disabled_features": self.disabled_features(),
            "available_features": [
                name for name in self.feature_map
                if self._level <= self.feature_map[name]
            ],
        }

    def get_history(self, limit: int = 20) -> list[dict]:
        """返回最近 N 次级别变化事件"""
        return [ev.to_dict() for ev in self._history[-limit:]]

    def reset(self) -> None:
        """重置到 L0_NORMAL (不影响已注册回调)"""
        self._level = DegradationLevel.L0_NORMAL
        self._reason = ""
        self._since = time.time()
        self._history.clear()

    # ─── 自动触发: 集成 DegradationDetector / SLOTracker (只读) ───

    def evaluate_from_detector(
        self,
        detector: object,
        slo_tracker: object | None = None,
    ) -> LevelChangeEvent | None:
        """根据 DegradationDetector 的最近报告自动触发降级

        映射规则:
        - Severity.EMERGENCY → L2_MINIMAL  (三轴退化)
        - Severity.CRITICAL  → L1_DEGRADED (双轴退化)
        - Severity.WARNING   → 不主动降级 (仅告警, 避免抖动)
        - Severity.NONE      → 尝试 recover() 一级

        注意: 只读取 detector 状态, 不修改 detector。
        返回触发的 LevelChangeEvent, 未触发则返回 None。
        """
        # J-Space Hook: 信号驱动降级
        try:
            from config import ENABLE_J_SPACE_HOOKS
            if ENABLE_J_SPACE_HOOKS and _signal_stream is not None:
                health_score = _signal_stream.aggregate("health", "mean_of_means")
                if health_score == 0.0:
                    pass  # 空 buffer，跳过
                elif health_score < 0.3:
                    if self._level < DegradationLevel.L1_DEGRADED:
                        self.trigger(
                            DegradationLevel.L1_DEGRADED,
                            reason=f"health_score={health_score:.2f} < 0.3 (CRITICAL)",
                            source="signal_stream",
                        )
        except Exception as e:
            logger.debug("degradation_strategy.health_signal_failed", error=str(e))

        # 延迟导入, 避免循环依赖
        try:
            from core.degradation_detector import Severity
        except Exception as e:
            logger.debug(f"DegradationStrategy 无法导入 Severity: {e!r}")
            return None

        report = getattr(detector, "_last_report", None)
        if report is None:
            return None
        severity = getattr(report, "severity", Severity.NONE)

        if severity == Severity.EMERGENCY:
            target = DegradationLevel.L2_MINIMAL
            reason = (
                f"detector EMERGENCY (三轴退化): "
                f"{getattr(report, 'axis', 'unknown')}"
            )
            if self._level < target:
                self.trigger(target, reason=reason, source="detector")
                return self._history[-1] if self._history else None
        elif severity == Severity.CRITICAL:
            target = DegradationLevel.L1_DEGRADED
            reason = (
                f"detector CRITICAL (双轴退化): "
                f"{getattr(report, 'axis', 'unknown')}"
            )
            # 仅在当前级别低于目标时升级 (不降级已有更严重的级别)
            if self._level < target:
                self.trigger(target, reason=reason, source="detector")
                return self._history[-1] if self._history else None
        elif severity == Severity.NONE:
            # 无退化: 尝试恢复一级
            if self._level > DegradationLevel.L0_NORMAL:
                self.recover(source="detector")
                return self._history[-1] if self._history else None
        # WARNING 不主动降级
        return None

    def evaluate_from_slo(
        self, slo_tracker: object, burn_threshold: float = 2.0
    ) -> LevelChangeEvent | None:
        """根据 SLOTracker 燃烧率自动触发降级

        映射规则:
        - burn_rate > 2.0 → L1_DEGRADED (SLO 燃烧过快)
        - (更高阈值不在此处处理, 由 detector EMERGENCY 接管)

        注意: 只读取 slo_tracker 状态, 不修改。
        返回触发的 LevelChangeEvent, 未触发则返回 None。
        """
        burn_fn = getattr(slo_tracker, "burn_rate", None)
        if not callable(burn_fn):
            return None
        try:
            burn = float(burn_fn())
        except Exception as e:
            logger.debug(f"DegradationStrategy 读取 burn_rate 失败: {e!r}")
            return None
        if burn > burn_threshold:
            target = DegradationLevel.L1_DEGRADED
            reason = f"slo burn_rate={burn:.2f} > {burn_threshold}"
            if self._level < target:
                self.trigger(target, reason=reason, source="slo_tracker")
                return self._history[-1] if self._history else None
        return None

    def emergency_reply(self, original_reply: str = "") -> str:
        """紧急模式下返回固定回复模板

        L3_EMERGENCY 时 basic_response 仍可用, 但建议使用固定模板
        避免触发可能失败的 LLM 调用。
        """
        if self._level >= DegradationLevel.L3_EMERGENCY:
            return EMERGENCY_FALLBACK_REPLY
        return original_reply


# ============================================================
# 全局单例
# ============================================================

_strategy: DegradationStrategy | None = None


def get_degradation_strategy() -> DegradationStrategy:
    """获取全局 DegradationStrategy 单例"""
    global _strategy
    if _strategy is None:
        _strategy = DegradationStrategy()
    return _strategy


def reset_degradation_strategy() -> DegradationStrategy:
    """重置全局单例 (主要用于测试)"""
    global _strategy
    _strategy = DegradationStrategy()
    return _strategy


# ============================================================
# 自动触发接线: 集成 DegradationDetector / SLOTracker (只读)
# ============================================================

def wire_auto_trigger(
    detector: object | None = None,
    slo_tracker: object | None = None,
    burn_threshold: float = 2.0,
) -> bool:
    """将降级策略接入 DegradationDetector 回调与 SLOTracker (只读取状态)

    - 通过 detector.on_degradation() 注册回调 (不修改 detector 文件)
    - 回调触发时调用 evaluate_from_detector / evaluate_from_slo 自动降级
    - detector / slo_tracker 为 None 时使用全局单例

    返回 True 表示接线成功, False 表示无可用 detector。
    """
    if detector is None:
        try:
            from core.degradation_detector import get_degradation_detector
            detector = get_degradation_detector()
        except Exception as e:
            logger.debug(f"wire_auto_trigger: 无可用 detector: {e!r}")
            return False
    if slo_tracker is None:
        try:
            from core.slo_tracker import get_slo_tracker
            slo_tracker = get_slo_tracker()
        except Exception:
            slo_tracker = None

    strat = get_degradation_strategy()
    _slo = slo_tracker
    _burn_thr = burn_threshold

    def _on_degradation(_report: Any) -> None:
        """detector 回调: 退化时自动评估降级"""
        try:
            strat.evaluate_from_detector(detector)
            if _slo is not None:
                strat.evaluate_from_slo(_slo, burn_threshold=_burn_thr)
        except Exception as e:
            logger.error(f"wire_auto_trigger 回调异常: {e!r}")

    on_degradation = getattr(detector, "on_degradation", None)
    if callable(on_degradation):
        on_degradation(_on_degradation)
        logger.info("DegradationStrategy.wire_auto_trigger 已接入 detector 回调")
        return True
    return False
