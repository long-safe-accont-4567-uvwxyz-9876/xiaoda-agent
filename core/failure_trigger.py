"""失败触发器 — 自动反思 + 重试 + 经验归档"""
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class FailureContext:
    """失败上下文"""
    task: str                          # 原始任务描述
    attempted_steps: list = field(default_factory=list)  # 已执行步骤
    error: str = ""                    # 错误信息
    error_type: str = ""               # 错误类型
    retry_count: int = 0               # 当前重试次数
    tool_name: str = ""                # 失败的工具名


class FailureTrigger:
    """失败触发器 — 失败→反思→重试→经验归档"""

    MAX_RETRIES = 3            # 最大重试次数
    PATTERN_THRESHOLD = 3      # 同类失败≥3次→写入规则

    def __init__(self, memory_db: Any | None=None, learning_manager: Any | None=None) -> None:
        """
        Args:
            memory_db: MemoryDB 实例（用于经验检索）
            learning_manager: LearningManager 实例（用于经验归档和提升）
        """
        self._memory_db = memory_db
        self._learning_manager = learning_manager

    async def on_failure(self, context: FailureContext) -> dict:
        """失败时触发反思流程

        Returns:
            dict: {action: retry/alternative/report, adjustment/method/reason}
        """
        logger.info("failure_trigger.activated",
                     extra={"tool": context.tool_name, "retry": context.retry_count})

        # 1. 检索相关经验
        past_experiences = await self._search_experiences(context.error)

        # 2. 生成修正策略
        strategy = await self._reflect(context, past_experiences)

        # 3. 判断下一步
        if context.retry_count < self.MAX_RETRIES:
            if strategy.get("action") == "retry":
                return {"action": "retry", "adjustment": strategy.get("adjustment", "")}
            if strategy.get("action") == "alternative":
                return {"action": "alternative", "method": strategy.get("method", "")}

        # 4. 超过重试上限→归档失败经验
        await self._archive_experience(context, strategy, outcome="failure")

        # 5. 检查是否高频模式
        pattern_count = await self._count_similar(context.error_type)
        if pattern_count >= self.PATTERN_THRESHOLD:
            await self._promote_to_rule(context, strategy)

        return {"action": "report", "reason": strategy.get("root_cause", "未知错误")}

    async def on_success_after_retry(self, context: FailureContext, strategy: dict) -> None:
        """重试成功后归档经验"""
        await self._archive_experience(context, strategy, outcome="success")

    async def _search_experiences(self, error: str) -> list:
        """检索相关经验（从 learnings 表）"""
        if not self._learning_manager:
            return []
        try:
            # 使用 learning_manager 检索相似错误
            # 注意：使用参数化查询，不拼接用户输入
            return await self._learning_manager.search_similar_errors(error, top_k=3)
        except Exception as e:
            logger.warning(f"failure_trigger.search_experiences_failed: {e}")
            return []

    async def _reflect(self, context: FailureContext, past_experiences: list) -> dict:
        """生成修正策略（根因分析 + 修正步骤）"""
        # 基于 past_experiences 生成策略
        if past_experiences:
            # 有历史经验，参考最佳实践
            best = past_experiences[0]
            return {
                "action": "retry",
                "adjustment": f"参考历史经验: {best.get('correction', '')}",
                "root_cause": best.get("error_pattern", context.error_type),
            }

        # 无历史经验，基于错误类型生成默认策略
        error_type = context.error_type.lower()
        if "timeout" in error_type or "timed out" in error_type:
            return {
                "action": "retry",
                "adjustment": "增加超时时间或简化请求",
                "root_cause": "请求超时",
            }
        if "auth" in error_type or "permission" in error_type:
            return {
                "action": "report",
                "root_cause": "认证/权限错误，需人工介入",
            }
        if "not found" in error_type or "404" in error_type:
            return {
                "action": "alternative",
                "method": "尝试替代路径或方法",
                "root_cause": "资源不存在",
            }
        return {
            "action": "retry",
            "adjustment": "检查参数和上下文后重试",
            "root_cause": context.error_type or "未知错误",
        }

    async def _archive_experience(self, context: FailureContext, strategy: dict, outcome: str) -> None:
        """归档经验到 learnings 表"""
        if not self._learning_manager:
            return
        try:
            await self._learning_manager.log_error(
                task=context.task,
                error=context.error[:200],  # 截断，不记录完整堆栈
                error_type=context.error_type,
                correction=strategy.get("adjustment", strategy.get("method", "")),
                outcome=outcome,
            )
            logger.info("failure_trigger.experience_archived",
                         extra={"outcome": outcome, "error_type": context.error_type})
        except Exception as e:
            logger.warning(f"failure_trigger.archive_failed: {e}")

    async def _count_similar(self, error_type: str) -> int:
        """统计同类错误出现次数"""
        if not self._learning_manager:
            return 0
        try:
            return await self._learning_manager.count_by_error_type(error_type)
        except Exception:
            return 0

    async def _promote_to_rule(self, context: FailureContext, strategy: dict) -> None:
        """高频失败提升为系统提示规则"""
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
