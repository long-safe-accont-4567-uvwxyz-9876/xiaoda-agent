"""TNR 安全自愈规约 — STRATUS (arXiv:2506.02009)

Test → Negotiate → Recover 三步自愈
确保自愈后健康度不降, 可回滚。
"""
from loguru import logger
from dataclasses import dataclass


@dataclass
class TNRState:
    """TNR 状态"""
    tested: bool = False
    negotiated: bool = False
    recovered: bool = False
    health_before: float = 1.0
    health_after: float = 1.0
    rollback_available: bool = True


class TNRSelfHeal:
    """TNR 安全自愈规约"""

    def __init__(self):
        self._state = TNRState()
        self._pre_recovery_snapshot: dict | None = None

    def test(self, health_check_func) -> bool:
        """Step 1: Test — 检测是否需要自愈"""
        self._state.health_before = health_check_func()
        self._state.tested = True
        needs_healing = self._state.health_before < 0.7
        if needs_healing:
            logger.warning(f"TNR: 检测到健康度低 ({self._state.health_before:.2f}), 需要自愈")
        return needs_healing

    def negotiate(self, options: list[str]) -> str | None:
        """Step 2: Negotiate — 选择自愈策略"""
        if not options:
            return None
        # 优先选择风险最低的策略
        chosen = options[0]
        self._state.negotiated = True
        logger.info(f"TNR: 选择自愈策略 → {chosen}")
        return chosen

    def recover(self, heal_func, rollback_func=None) -> bool:
        """Step 3: Recover — 执行自愈"""
        try:
            heal_func()
            self._state.recovered = True
            logger.info("TNR: 自愈成功")
            return True
        except Exception as e:
            logger.error(f"TNR: 自愈失败: {e}")
            if rollback_func:
                try:
                    rollback_func()
                    logger.info("TNR: 已回滚")
                except Exception as re:
                    logger.error(f"TNR: 回滚也失败: {re}")
            return False

    def verify(self, health_check_func) -> bool:
        """验证: 自愈后健康度不降"""
        self._state.health_after = health_check_func()
        ok = self._state.health_after >= self._state.health_before
        if not ok:
            logger.warning(f"TNR: 自愈后健康度下降 {self._state.health_before:.2f} → {self._state.health_after:.2f}")
        return ok

    def get_state(self) -> dict:
        return {
            "tested": self._state.tested,
            "negotiated": self._state.negotiated,
            "recovered": self._state.recovered,
            "health_before": round(self._state.health_before, 3),
            "health_after": round(self._state.health_after, 3),
            "health_maintained": self._state.health_after >= self._state.health_before,
        }
