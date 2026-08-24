"""Web User — 每个事件通过 WebSocket 推送，无消息条数限制。"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from loguru import logger

from agent_core.user_base import AGENT_DISPLAY, UserBase
from core.event_bus import AgentEvent


class WebUser(UserBase):
    """Web 端：每个事件通过 ws.send_json 推送。

    Args:
        send_fn: WebSocket 发送函数，签名为 async (event: dict) -> None
    """

    def __init__(self, send_fn: Callable[[dict], Awaitable[None]]) -> None:
        self._send_fn = send_fn

    async def deliver(self, event: AgentEvent) -> None:
        display = event.data.get("display_name") or AGENT_DISPLAY.get(event.agent, event.agent)
        payload: dict[str, Any] = {
            "type": event.type.value,
            "agent": event.agent,
            "task_id": event.task_id,
            "display_name": display,
            "timestamp": event.timestamp,
        }
        for k, v in event.data.items():
            if k not in payload:
                payload[k] = v
        try:
            await self._send_fn(payload)
        except Exception as e:
            logger.debug("web_user.send_failed type={} error={}", event.type, str(e)[:100])
