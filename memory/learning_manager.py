from typing import Any
from loguru import logger

from db.db_learning import LearningDB


CORRECTION_SIGNALS = ["不对", "错了", "不是这样的", "应该是", "你说错了",
                      "不是", "搞错了", "弄错了", "不对吧", "才不是",
                      "别瞎说", "胡说", "乱说"]
FEATURE_SIGNALS = ["能不能", "可不可以", "帮我", "我希望", "要是能",
                   "如果可以", "想要", "有没有办法", "能不能帮我"]


class LearningManager:
    """管理错误反馈、特性诉求等学习信号的记录与归纳。"""

    def __init__(self, db: Any, learning: LearningDB, router: Any) -> None:
        self._db = db
        self.learning = learning
        self._router = router

    async def log_error(self, error_text: str, context: str = "",
                         suggested_fix: str = "", priority: str = "high") -> str:
        summary = error_text[:100] if error_text else "未知错误"
        error_id = await self.learning.insert_error(
            summary=summary,
            error_text=error_text[:500],
            context=context[:300],
            suggested_fix=suggested_fix,
            priority=priority,
        )
        if error_id:
            logger.info("learning.error_logged", error_id=error_id, summary=summary[:60])
        return error_id

    async def log_correction(self, user_msg: str, bot_reply: str,
                              correction_hint: str = "") -> str | None:
        summary = f"用户纠正: {user_msg[:80]}"
        details = f"Bot说: {bot_reply[:150]}\n用户纠正: {user_msg[:150]}"
        if correction_hint:
            details += f"\n纠正提示: {correction_hint}"

        pattern_key = f"correction.{user_msg[:30]}"
        existing = await self.learning.find_learning_by_pattern(pattern_key)
        if existing:
            await self.learning.bump_learning_recurrence(existing["learning_id"])
            logger.info("learning.correction_recurrence",
                        learning_id=existing["learning_id"],
                        count=existing["recurrence_count"] + 1)
            return existing["learning_id"]

        learning_id = await self.learning.insert_learning(
            category="correction",
            priority="medium",
            summary=summary,
            details=details,
            source="user_feedback",
            pattern_key=pattern_key,
        )
        if learning_id:
            logger.info("learning.correction_logged", learning_id=learning_id)
        return learning_id

    async def log_feature_request(self, capability: str,
                                    user_context: str = "") -> str:
        request_id = await self.learning.insert_feature_request(
            capability=capability,
            user_context=user_context,
        )
        if request_id:
            logger.info("learning.feature_logged", request_id=request_id,
                        capability=capability[:60])
        return request_id

    async def evaluate_after_conversation(self, user_msg: str, reply: str,
                                           tool_results: list) -> None:
        try:
            for signal in CORRECTION_SIGNALS:
                if signal in user_msg:
                    await self.log_correction(user_msg, reply, signal)
                    break

            for signal in FEATURE_SIGNALS:
                if signal in user_msg and len(user_msg) > 5:
                    await self.log_feature_request(
                        capability=user_msg[:100],
                        user_context=user_msg[:200],
                    )
                    break

            for result in tool_results:
                from tool_engine.tool_registry import ToolResult
                if isinstance(result, ToolResult) and not result.success:
                    await self.log_error(
                        error_text=result.error,
                        context=f"工具调用失败",
                        priority="medium",
                    )
        except Exception as e:
            logger.warning("learning.evaluate_failed", error=str(e))

    async def auto_promote(self) -> None:
        try:
            promotable = await self.learning.get_promotable_learnings(min_recurrence=3)
            for learning in promotable:
                first_seen = learning.get("first_seen", 0)
                last_seen = learning.get("last_seen", 0)
                if last_seen - first_seen < 86400:
                    continue

                await self.learning.promote_learning(learning["learning_id"])
                logger.info("learning.promoted",
                            learning_id=learning["learning_id"],
                            summary=learning["summary"][:60],
                            recurrence=learning["recurrence_count"])
        except Exception as e:
            logger.warning("learning.auto_promote_failed", error=str(e))

    async def get_system_prompt_additions(self) -> str:
        try:
            promoted = await self.learning.get_promoted_learnings()
            if not promoted:
                return ""

            lines = ["[人家学到的重要经验]"]
            for p in promoted[:10]:
                summary = p.get("summary", "")
                if summary:
                    lines.append(f"· {summary[:100]}")

            return "\n".join(lines)
        except Exception:
            return ""
