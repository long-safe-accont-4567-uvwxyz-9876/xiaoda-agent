"""QQ User — 仅 SUB_STARTED 通知，其余静默。

QQ Bot 消息频率封顶 5 条/轮，子代理完成/失败事件不额外消耗消息条数，
主回复本身已包含最终结果或降级文案。
"""
from __future__ import annotations

from typing import Awaitable, Callable

from loguru import logger

from core.event_bus import AgentEvent, AgentEventType
from agent_core.user_base import UserBase, AGENT_DISPLAY


class QQUser(UserBase):
    """QQ 端：仅子代理开始时发送1条通知消息。

    Args:
        reply_fn: QQ 消息回复函数，签名为 async (content: str, msg_seq: int) -> None
        msg_seq_fn: 获取下一个 msg_seq 的函数
    """

    def __init__(
        self,
        reply_fn: Callable[[str, int], Awaitable[None]],
        msg_seq_fn: Callable[[], int],
    ) -> None:
        self._reply_fn = reply_fn
        self._msg_seq_fn = msg_seq_fn

    async def deliver(self, event: AgentEvent) -> None:
        # 仅 SUB_STARTED 发送消息，其余事件静默
        if event.type != AgentEventType.SUB_STARTED:
            return
        display = event.data.get("display_name") or AGENT_DISPLAY.get(event.agent, event.agent)
        content = f"🔄 {display}正在思考..."
        try:
            await self._reply_fn(content, self._msg_seq_fn())
        except Exception as e:
            logger.debug("qq_user.reply_failed agent={} error={}", event.agent, str(e)[:100])