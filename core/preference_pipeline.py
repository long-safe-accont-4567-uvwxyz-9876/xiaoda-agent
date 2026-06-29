"""偏好管线协调器 — 串联四层偏好沉淀

L1: LearningLoop (实时约束, 立即注入 prompt)
L2: LearningManager (SQLite 持久化, recurrence 计数, auto_promote 晋升)
L3: LearningFeedback (JSON 教训+策略, prompt 注入)
L4: PermanentMemory (最高级偏好, prompt 注入)

本模块负责层间联动 (写入→流转→晋升):
- L1→L3: process_correction 提取约束后同时记录为教训
- L2→L3: check_promotion 晋升学习时同时提取为教训

L2 的纠正记录由 evaluate_after_conversation 负责, L4 由 prompt_builder 直接注入,
此处不重复, 只补全缺失的层间联动。
"""
from __future__ import annotations

from loguru import logger

from core.learning_feedback import (
    EventType,
    LearningEvent,
    get_learning_feedback_loop,
)
from core.learning_loop import get_learning_loop


class PreferencePipeline:
    """四层偏好管线协调器"""

    def __init__(self) -> None:
        self._loop = get_learning_loop()
        self._feedback = get_learning_feedback_loop()

    async def process_correction(self, user_msg: str, bot_reply: str,
                                  learning_manager=None) -> str | None:
        """用户纠正 → L1(约束) + L3(教训) 联动

        L1 提取实时约束后, 同步记录为 L3 教训, 使约束不仅影响当前 prompt,
        还能通过教训表在后续相似任务中被召回。
        """
        # L1: 实时约束 (立即生效, 注入 prompt)
        constraint = await self._loop.process_correction(user_msg, bot_reply)
        # L3: 约束提取为教训 (供后续相似任务召回)
        if constraint:
            try:
                self._feedback.record(LearningEvent(
                    event_type=EventType.USER_FEEDBACK,
                    task_description="用户纠正",
                    approach_used=bot_reply[:80],
                    outcome=constraint,
                ))
            except Exception as e:
                logger.warning("pipeline.l3_record_failed", error=str(e))
        return constraint

    async def check_promotion(self, learning_manager) -> list[str]:
        """L2→L3 晋升联动: recurrence≥3 的学习晋升时, 同时提取为 L3 教训

        替代原 auto_promote, 增加了 L3 教训提取 — 晋升的经验不仅在 L2 的
        get_system_prompt_additions 中出现, 还能通过 L3 教训表按相关性被召回。
        """
        promoted_summaries: list[str] = []
        if learning_manager is None:
            return promoted_summaries
        try:
            promotable = await learning_manager.learning.get_promotable_learnings(min_recurrence=3)
            for learning in promotable:
                first_seen = learning.get("first_seen", 0)
                last_seen = learning.get("last_seen", 0)
                if last_seen - first_seen < 86400:
                    continue
                summary = learning.get("summary", "")
                # L3: 晋升时提取教训
                if summary:
                    try:
                        self._feedback.record(LearningEvent(
                            event_type=EventType.USER_FEEDBACK,
                            task_description="经验晋升",
                            outcome=summary,
                        ))
                    except Exception:
                        pass
                # L2: 晋升
                await learning_manager.learning.promote_learning(learning["learning_id"])
                logger.info("pipeline.promoted",
                            learning_id=learning["learning_id"],
                            summary=summary[:60])
                promoted_summaries.append(summary)
        except Exception as e:
            logger.warning("pipeline.promotion_failed", error=str(e))
        return promoted_summaries


_pipeline: PreferencePipeline | None = None


def get_preference_pipeline() -> PreferencePipeline:
    """获取全局 PreferencePipeline 单例."""
    global _pipeline
    if _pipeline is None:
        _pipeline = PreferencePipeline()
    return _pipeline
