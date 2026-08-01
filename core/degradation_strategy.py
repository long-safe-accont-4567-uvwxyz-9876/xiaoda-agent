"""Q4 降级策略标准化 — 4 级降级 (精简版)

4 级降级 (IntEnum):
0. L0_NORMAL     (0): 正常运行, 全功能
1. L1_DEGRADED   (1): 轻度降级, 非核心功能关闭
2. L2_MINIMAL    (2): 最小化运行, 只保留文本对话
3. L3_EMERGENCY  (3): 紧急模式, 只保留最基础响应

特性:
- 功能级别映射 (feature_map)
- 级别变化回调 (on_level_change)
- 恢复机制: recover() 逐级回升

合并自 core/degradation.py (兼容层 DegradationManager)。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum
from collections.abc import Callable

from loguru import logger


# ============================================================
# 4 级降级枚举
# ============================================================

class DegradationLevel(IntEnum):
    """4 级降级级别 (值越小越接近正常)"""
    L0_NORMAL = 0
    L1_DEGRADED = 1
    L2_MINIMAL = 2
    L3_EMERGENCY = 3


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
    source: str = "manual"

    def to_dict(self) -> dict:
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

DEFAULT_FEATURE_MAP: dict[str, DegradationLevel] = {
    "tts": DegradationLevel.L0_NORMAL,
    "emotion": DegradationLevel.L0_NORMAL,
    "web_browse": DegradationLevel.L1_DEGRADED,
    "memory_search": DegradationLevel.L1_DEGRADED,
    "text_chat": DegradationLevel.L2_MINIMAL,
    "basic_response": DegradationLevel.L3_EMERGENCY,
}

EMERGENCY_FALLBACK_REPLY = "人家现在有点不舒服，稍等一下哦～"


class DegradationStrategy:
    """4 级降级策略管理器"""

    def __init__(
        self,
        feature_map: dict[str, DegradationLevel] | None = None,
        initial_level: DegradationLevel = DegradationLevel.L0_NORMAL,
    ) -> None:
        self.feature_map: dict[str, DegradationLevel] = dict(
            feature_map if feature_map is not None else DEFAULT_FEATURE_MAP
        )
        self._level: DegradationLevel = initial_level
        self._reason: str = ""
        self._since: float = time.time()
        self._callbacks: list[Callable[[LevelChangeEvent], None]] = []
        self._history: list[LevelChangeEvent] = []

    @property
    def current_level(self) -> DegradationLevel:
        return self._level

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def since(self) -> float:
        return self._since

    def trigger(
        self,
        level: DegradationLevel,
        reason: str,
        source: str = "manual",
    ) -> None:
        """触发降级到指定级别"""
        old = self._level
        if level == old:
            self._reason = reason or self._reason
            logger.debug(f"Degradation.same_level level={level.name} reason={reason}")
            return
        self._level = level
        self._reason = reason
        self._since = time.time()
        event = LevelChangeEvent(old_level=old, new_level=level, reason=reason, source=source)
        self._history.append(event)
        if level > old:
            logger.warning(f"Degradation.escalate {old.name} -> {level.name} reason={reason} source={source}")
        else:
            logger.info(f"Degradation.recover {old.name} -> {level.name} reason={reason} source={source}")
        self._fire_callbacks(event)

    def recover(self, source: str = "manual") -> bool:
        """尝试恢复到上一级 (L3→L2→L1→L0)"""
        if self._level <= DegradationLevel.L0_NORMAL:
            return False
        prev = DegradationLevel(self._level - 1)
        self.trigger(prev, reason="recover", source=source)
        return True

    def is_feature_available(self, feature: str) -> bool:
        """检查功能在当前级别是否可用"""
        threshold = self.feature_map.get(feature)
        if threshold is None:
            return True
        return self._level <= threshold

    def disabled_features(self) -> list[str]:
        """返回当前级别下被关闭的功能列表"""
        return [name for name, threshold in self.feature_map.items() if self._level > threshold]

    def on_level_change(self, callback: Callable[[LevelChangeEvent], None]) -> None:
        self._callbacks.append(callback)

    def clear_callbacks(self) -> None:
        self._callbacks.clear()

    def _fire_callbacks(self, event: LevelChangeEvent) -> None:
        for cb in list(self._callbacks):
            try:
                cb(event)
            except Exception as e:
                logger.error(f"Degradation 回调异常: {e!r}")

    def get_status(self) -> dict:
        return {
            "level": self._level.name,
            "level_value": int(self._level),
            "reason": self._reason,
            "since": self._since,
            "disabled_features": self.disabled_features(),
            "available_features": [
                name for name in self.feature_map if self._level <= self.feature_map[name]
            ],
        }

    def get_history(self, limit: int = 20) -> list[dict]:
        return [ev.to_dict() for ev in self._history[-limit:]]

    def reset(self) -> None:
        self._level = DegradationLevel.L0_NORMAL
        self._reason = ""
        self._since = time.time()
        self._history.clear()

    def emergency_reply(self, original_reply: str = "") -> str:
        """紧急模式下返回固定回复模板"""
        if self._level >= DegradationLevel.L3_EMERGENCY:
            return EMERGENCY_FALLBACK_REPLY
        return original_reply


# ============================================================
# 全局单例
# ============================================================

_strategy: DegradationStrategy | None = None


def get_degradation_strategy() -> DegradationStrategy:
    global _strategy
    if _strategy is None:
        _strategy = DegradationStrategy()
    return _strategy


def reset_degradation_strategy() -> DegradationStrategy:
    global _strategy
    _strategy = DegradationStrategy()
    return _strategy


# ============================================================
# 兼容层: DegradationManager (合并自 core/degradation.py)
# ============================================================

# 旧版特性名 → 新版特性名映射
_LEGACY_FEATURE_MAP = {
    "tools": "web_browse",
    "memory": "memory_search",
    "tts": "tts",
    "image": "emotion",
    "rag": "memory_search",
    "plugins": "web_browse",
}

# 旧版枚举别名
FULL = DegradationLevel.L0_NORMAL
DEGRADED = DegradationLevel.L1_DEGRADED
MINIMAL = DegradationLevel.L2_MINIMAL
EMERGENCY = DegradationLevel.L3_EMERGENCY


class DegradationManager:
    """降级策略管理器（兼容层，委托给 DegradationStrategy）。"""

    def __init__(self) -> None:
        self._strategy = get_degradation_strategy()

    @property
    def level(self) -> DegradationLevel:
        return DegradationLevel(int(self._strategy.current_level))

    @property
    def reason(self) -> str:
        return self._strategy.reason

    def is_feature_available(self, feature: str) -> bool:
        new_feature = _LEGACY_FEATURE_MAP.get(feature, feature)
        return self._strategy.is_feature_available(new_feature)

    def escalate(self, reason: str) -> None:
        current = int(self._strategy.current_level)
        if current < 3:
            self._strategy.trigger(DegradationLevel(current + 1), reason=reason, source="legacy_escalate")

    def recover(self) -> None:
        self._strategy.recover(source="legacy_recover")

    def set_level(self, level: DegradationLevel, reason: str = "") -> None:
        self._strategy.trigger(DegradationLevel(int(level)), reason=reason, source="legacy_set")

    def get_status(self) -> dict:
        return self._strategy.get_status()


_degradation_manager: DegradationManager | None = None


def get_degradation_manager() -> DegradationManager:
    """获取全局降级管理器单例（兼容层）。"""
    global _degradation_manager
    if _degradation_manager is None:
        _degradation_manager = DegradationManager()
    return _degradation_manager
