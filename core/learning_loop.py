"""学习反馈闭环 — 纠正记录 → 模式提取 → 约束注入 → 后续行为改变"""
from collections import deque
from loguru import logger


class LearningLoop:
    """学习反馈闭环"""

    def __init__(self) -> None:
        self._active_constraints: deque = deque(maxlen=20)
        self._correction_count: int = 0

    async def process_correction(self, user_msg: str, bot_reply: str) -> str | None:
        """处理用户纠正, 提取约束"""
        constraint = self._extract_constraint(user_msg, bot_reply)
        if constraint:
            self._active_constraints.append(constraint)
            self._correction_count += 1
            logger.info(f"学习闭环: 新约束 → {constraint}")
        return constraint

    def get_active_constraints(self) -> list[str]:
        """获取活跃约束 (最近10条)"""
        return list(self._active_constraints)[-10:]

    def _extract_constraint(self, user_msg: str, bot_reply: str) -> str | None:
        """从用户消息中提取行为约束"""
        msg = user_msg.lower()
        if any(kw in msg for kw in ["不要", "别", "不准", "不能", "禁止"]):
            return f"用户偏好: {user_msg.strip()[:80]}"
        if any(kw in msg for kw in ["应该是", "其实", "不对", "错了"]):
            return f"纠正: {user_msg.strip()[:80]}"
        if "记住" in msg or "记一下" in msg:
            return f"记忆: {user_msg.strip()[:80]}"
        return None

    def get_stats(self) -> dict:
        """返回学习闭环统计 (纠正总数与活跃约束数)."""
        return {
            "total_corrections": self._correction_count,
            "active_constraints": len(self._active_constraints),
        }


_learning_loop = LearningLoop()


def get_learning_loop() -> LearningLoop:
    """获取全局 LearningLoop 单例."""
    return _learning_loop
