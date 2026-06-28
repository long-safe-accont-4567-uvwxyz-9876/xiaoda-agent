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

    def __init__(self):
        self._level = DegradationLevel.FULL
        self._reason = ""

    @property
    def level(self) -> DegradationLevel:
        return self._level

    @property
    def reason(self) -> str:
        return self._reason

    def is_feature_available(self, feature: str) -> bool:
        return LEVEL_FEATURES.get(self._level, {}).get(feature, False)

    def escalate(self, reason: str):
        if self._level < DegradationLevel.EMERGENCY:
            old = self._level
            self._level = DegradationLevel(self._level + 1)
            self._reason = reason
            logger.warning(f"降级升级: {old.name} -> {self._level.name}, 原因: {reason}")

    def recover(self):
        if self._level > DegradationLevel.FULL:
            old = self._level
            self._level = DegradationLevel(self._level - 1)
            self._reason = ""
            logger.info(f"降级恢复: {old.name} -> {self._level.name}")

    def set_level(self, level: DegradationLevel, reason: str = ""):
        if level != self._level:
            logger.info(f"降级设置: {self._level.name} -> {level.name}, 原因: {reason}")
            self._level = level
            self._reason = reason

    def get_status(self) -> dict:
        return {
            "level": self._level.name,
            "features": LEVEL_FEATURES.get(self._level, {}),
            "reason": self._reason,
        }


_degradation_manager = DegradationManager()


def get_degradation_manager() -> DegradationManager:
    return _degradation_manager
