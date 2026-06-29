"""降级策略标准化 — 4级降级

FULL → DEGRADED → MINIMAL → EMERGENCY
"""
from enum import IntEnum
from loguru import logger


class DegradationLevel(IntEnum):
    FULL = 0
    DEGRADED = 1
    MINIMAL = 2
    EMERGENCY = 3


LEVEL_FEATURES = {
    DegradationLevel.FULL: {"tools": True, "memory": True, "tts": True, "image": True, "rag": True, "plugins": True},
    DegradationLevel.DEGRADED: {"tools": True, "memory": True, "tts": True, "image": False, "rag": True, "plugins": False},
    DegradationLevel.MINIMAL: {"tools": False, "memory": True, "tts": False, "image": False, "rag": False, "plugins": False},
    DegradationLevel.EMERGENCY: {"tools": False, "memory": False, "tts": False, "image": False, "rag": False, "plugins": False},
}


class DegradationManager:
    """降级策略管理器"""

    def __init__(self) -> None:
        """初始化降级管理器, 默认级别为 FULL."""
        self._level = DegradationLevel.FULL
        self._reason = ""

    @property
    def level(self) -> DegradationLevel:
        """返回当前降级级别."""
        return self._level

    @property
    def reason(self) -> str:
        """返回最近一次降级原因描述."""
        return self._reason

    def is_feature_available(self, feature: str) -> bool:
        """检查指定特性在当前降级级别下是否可用.

        Args:
            feature: 特性名 (如 tools/memory/tts/image/rag/plugins)

        Returns:
            True 表示可用, False 表示被禁用
        """
        return LEVEL_FEATURES.get(self._level, {}).get(feature, False)

    def escalate(self, reason: str) -> None:
        """将降级级别提升一级.

        Args:
            reason: 降级原因描述
        """
        if self._level < DegradationLevel.EMERGENCY:
            old = self._level
            self._level = DegradationLevel(self._level + 1)
            self._reason = reason
            logger.warning(f"降级升级: {old.name} -> {self._level.name}, 原因: {reason}")

    def recover(self) -> None:
        """将降级级别恢复一级 (向 FULL 靠拢)."""
        if self._level > DegradationLevel.FULL:
            old = self._level
            self._level = DegradationLevel(self._level - 1)
            self._reason = ""
            logger.info(f"降级恢复: {old.name} -> {self._level.name}")

    def set_level(self, level: DegradationLevel, reason: str = "") -> None:
        """直接设置降级级别.

        Args:
            level: 目标降级级别
            reason: 降级原因, 默认空字符串
        """
        if level != self._level:
            logger.info(f"降级设置: {self._level.name} -> {level.name}, 原因: {reason}")
            self._level = level
            self._reason = reason

    def get_status(self) -> dict:
        """返回当前降级状态摘要 (级别/特性映射/原因)."""
        return {
            "level": self._level.name,
            "features": LEVEL_FEATURES.get(self._level, {}),
            "reason": self._reason,
        }


_degradation_manager = DegradationManager()


def get_degradation_manager() -> DegradationManager:
    """获取全局降级管理器单例."""
    return _degradation_manager
