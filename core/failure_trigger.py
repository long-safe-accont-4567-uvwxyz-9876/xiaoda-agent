"""失败触发器 — 自动反思 + 重试 + 经验归档"""
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class FailureContext:
    """失败上下文"""
    task: str
    attempted_steps: list = field(default_factory=list)
    error: str = ""
    error_type: str = ""
    retry_count: int = 0
    tool_name: str = ""


class FailureTrigger:
    """失败触发器 — 失败→反思→重试→经验归档"""

    MAX_RETRIES = 3
    PATTERN_THRESHOLD = 3

    def __init__(self, memory_db=None, learning_manager=None):
        self._memory_db = memory_db
        self._learning_manager = learning_manager

    async def on_failure(self, context: FailureContext) -> dict:
        logger.info("failure_trigger.activated",
                     extra={"tool": context.tool_name, "retry": context.retry_count})

        past_experiences = await self._search_experiences(context.error)

        strategy = await self._reflect(context, past_experiences)

        if context.retry_count < self.MAX_RETRIES:
            if strategy.get("action") == "retry":
                return {"action": "retry", "adjustment": strategy.get("adjustment", "")}
            elif strategy.get("action") == "alternative":
                return {"action": "alternative", "method": strategy.get("method", "")}

        await self._archive_experience(context, strategy, outcome="failure")

        pattern_count = await self._count_similar(context.error_type)
        if pattern_count >= self.PATTERN_THRESHOLD:
            await self._promote_to_rule(context, strategy)

        return {"action": "report", "reason": strategy.get("root_cause", "未知错误")}

    async def on_success_after_retry(self, context: FailureContext, strategy: dict):
        await self._archive_experience(context, strategy, outcome="success")

    async def _search_experiences(self, error: str) -> list:
        if not self._learning_manager:
            return []
        try:
            return await self._learning_manager.search_similar_errors(error, top_k=3)
        except Exception as e:
            logger.warning(f"failure_trigger.search_experiences_failed: {e}")
            return []

    async def _reflect(self, context: FailureContext, past_experiences: list) -> dict:
        if past_experiences:
            best = past_experiences[0]
            return {
                "action": "retry",
                "adjustment": f"参考历史经验: {best.get('correction', '')}",
                "root_cause": best.get("error_pattern", context.error_type),
            }

        error_type = context.error_type.lower()
        if "timeout" in error_type or "timed out" in error_type:
            return {
                "action": "retry",
                "adjustment": "增加超时时间或简化请求",
                "root_cause": "请求超时",
            }
        elif "auth" in error_type or "permission" in error_type:
            return {
                "action": "report",
                "root_cause": "认证/权限错误，需人工介入",
            }
        elif "not found" in error_type or "404" in error_type:
            return {
                "action": "alternative",
                "method": "尝试替代路径或方法",
                "root_cause": "资源不存在",
            }
        else:
            return {
                "action": "retry",
                "adjustment": "检查参数和上下文后重试",
                "root_cause": context.error_type or "未知错误",
            }

    async def _archive_experience(self, context: FailureContext, strategy: dict, outcome: str):
        if not self._learning_manager:
            return
        try:
            await self._learning_manager.log_error(
                task=context.task,
                error=context.error[:200],
                error_type=context.error_type,
                correction=strategy.get("adjustment", strategy.get("method", "")),
                outcome=outcome,
            )
            logger.info("failure_trigger.experience_archived",
                         extra={"outcome": outcome, "error_type": context.error_type})
        except Exception as e:
            logger.warning(f"failure_trigger.archive_failed: {e}")

    async def _count_similar(self, error_type: str) -> int:
        if not self._learning_manager:
            return 0
        try:
            return await self._learning_manager.count_by_error_type(error_type)
        except Exception:
            return 0

    async def _promote_to_rule(self, context: FailureContext, strategy: dict):
        if not self._learning_manager:
            return
        try:
            await self._learning_manager.promote_error_pattern(
                error_type=context.error_type,
                correction=strategy.get("adjustment", ""),
            )
            logger.info("failure_trigger.promoted_to_rule",
                         extra={"error_type": context.error_type})
        except Exception as e:
            logger.warning(f"failure_trigger.promote_failed: {e}")
