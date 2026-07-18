"""降级策略标准化 — 4级降级（兼容层，委托给 degradation_strategy.py）

⚠️ 本模块已弃用，仅保留向后兼容接口。所有逻辑委托给
core.degradation_strategy.DegradationStrategy，确保行为一致。

新代码请直接使用:
    from core.degradation_strategy import get_degradation_strategy, DegradationLevel
"""
from __future__ import annotations

from loguru import logger

# 延迟导入真正的实现
from core.degradation_strategy import (
    DegradationLevel as _NewLevel,
    DegradationStrategy,
    get_degradation_strategy,
)


# ── 兼容枚举：直接复用新版 DegradationLevel，添加旧名别名 ──
# Python 3.11 不允许继承已有成员的 Enum，改为模块级常量别名
DegradationLevel = _NewLevel
FULL = _NewLevel.L0_NORMAL       # 0
DEGRADED = _NewLevel.L1_DEGRADED  # 1
MINIMAL = _NewLevel.L2_MINIMAL    # 2
EMERGENCY = _NewLevel.L3_EMERGENCY  # 3


# 旧版特性映射 → 新版 feature_name 转换
_LEGACY_FEATURE_MAP = {
    "tools": "web_browse",
    "memory": "memory_search",
    "tts": "tts",
    "image": "emotion",
    "rag": "memory_search",
    "plugins": "web_browse",
}


class DegradationManager:
    """降级策略管理器（兼容层，委托给 DegradationStrategy）。"""

    def __init__(self) -> None:
        self._strategy = get_degradation_strategy()

    @property
    def level(self) -> DegradationLevel:
        """返回当前降级级别（兼容旧版枚举名称）。"""
        return DegradationLevel(int(self._strategy.current_level))

    @property
    def reason(self) -> str:
        """返回最近一次降级原因描述."""
        return self._strategy.reason

    def is_feature_available(self, feature: str) -> bool:
        """检查指定特性在当前降级级别下是否可用.

        Args:
            feature: 旧版特性名 (如 tools/memory/tts/image/rag/plugins)

        Returns:
            True 表示可用, False 表示被禁用
        """
        new_feature = _LEGACY_FEATURE_MAP.get(feature, feature)
        return self._strategy.is_feature_available(new_feature)

    def escalate(self, reason: str) -> None:
        """将降级级别提升一级."""
        current = int(self._strategy.current_level)
        if current < 3:
            self._strategy.trigger(
                _NewLevel(current + 1), reason=reason, source="legacy_escalate")

    def recover(self) -> None:
        """将降级级别恢复一级 (向 FULL 靠拢)."""
        self._strategy.recover(source="legacy_recover")

    def set_level(self, level: DegradationLevel, reason: str = "") -> None:
        """直接设置降级级别."""
        self._strategy.trigger(_NewLevel(int(level)), reason=reason, source="legacy_set")

    def get_status(self) -> dict:
        """返回当前降级状态摘要."""
        return self._strategy.get_status()


_degradation_manager: DegradationManager | None = None


def get_degradation_manager() -> DegradationManager:
    """获取全局降级管理器单例（兼容层）。"""
    global _degradation_manager
    if _degradation_manager is None:
        _degradation_manager = DegradationManager()
    return _degradation_manager
