"""User 基类 — EventBus 事件投递的统一入口。
各渠道 User 继承此基类，实现 deliver(event) 决定如何投递事件：
- CLIUser：每个事件直接 rich print（无消息条数限制）
- WebUser：每个事件 ws.send_json 推送（无消息条数限制）
- QQUser：仅 SUB_STARTED 通知，其余静默（节省5条限制）
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.event_bus import AgentEvent


class UserBase(ABC):
    """User 基类 — 所有渠道 User 的父类。"""

    @abstractmethod
    async def deliver(self, event: "AgentEvent") -> None:
        """投递事件 — 由 EventBus.emit() 调用。

        各渠道 User 按自身特性实现：
        - CLIUser：实时打印
        - WebUser：WebSocket 推送
        - QQUser：仅开始时通知
        """
        ...


# 子代理显示名映射（所有渠道共用）
AGENT_DISPLAY: dict[str, str] = {
    "xiaoli": "小莉",
    "xiaolang": "小狼",
    "xiaolian": "小涟",
    "xiaoke": "小可",
    "xiaoda": "小妲",
}

# 紧凑图标映射（QQ 聚合模式用）
STATUS_ICON: dict[str, str] = {
    "sub_started": "🔄",
    "sub_completed": "✅",
    "sub_failed": "❌",
    "sub_cancelled": "🚫",
    "tool_started": "🔧",
    "tool_completed": "✓",
    "tool_failed": "✗",
}
