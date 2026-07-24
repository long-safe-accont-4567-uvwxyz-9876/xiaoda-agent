"""CLI User — 每个事件实时打印，无消息条数限制。"""
from __future__ import annotations

from agent_core.user_base import AGENT_DISPLAY, UserBase
from core.event_bus import AgentEvent, AgentEventType


class CLIUser(UserBase):
    """CLI 端：每个事件直接打印。"""

    async def deliver(self, event: AgentEvent) -> None:
        display = event.data.get("display_name") or AGENT_DISPLAY.get(event.agent, event.agent)

        if event.type == AgentEventType.SUB_STARTED:
            print(f"  🔄 {display}正在思考...")
        elif event.type == AgentEventType.SUB_COMPLETED:
            print(f"  ✅ {display}回复完成")
        elif event.type == AgentEventType.SUB_FAILED:
            print(f"  ❌ {display}遇到了问题")
        elif event.type == AgentEventType.SUB_CANCELLED:
            print(f"  🚫 {display}被取消了")
        elif event.type == AgentEventType.TOOL_STARTED:
            tool = event.data.get("tool_name", "")
            print(f"  🔧 正在调用{tool}...")
        elif event.type == AgentEventType.TOOL_COMPLETED:
            tool = event.data.get("tool_name", "")
            print(f"  ✓ {tool}完成")
        elif event.type == AgentEventType.TOOL_FAILED:
            tool = event.data.get("tool_name", "")
            print(f"  ✗ {tool}失败")
