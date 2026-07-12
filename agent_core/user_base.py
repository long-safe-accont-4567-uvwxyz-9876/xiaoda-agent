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


# 子代理显示名映射（所有渠道共用）— 从 config 动态构建
_AGENT_DISPLAY_CACHE: dict[str, str] | None = None


class _LazyAgentDisplay(dict):
    """懒加载字典：首次访问时从 config 构建显示名映射。"""

    def __init__(self) -> None:
        super().__init__()
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        from config import get_agent_display_name, agent_names
        self.update({
            name: get_agent_display_name(name)
            for name in agent_names()
        })
        self._loaded = True

    def __getitem__(self, key: str) -> str:
        self._ensure_loaded()
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        self._ensure_loaded()
        return super().__contains__(key)

    def get(self, key: str, default: str | None = None) -> str | None:
        self._ensure_loaded()
        return super().get(key, default)

    def keys(self):
        self._ensure_loaded()
        return super().keys()

    def values(self):
        self._ensure_loaded()
        return super().values()

    def items(self):
        self._ensure_loaded()
        return super().items()

    def __len__(self) -> int:
        self._ensure_loaded()
        return super().__len__()

    def __iter__(self):
        self._ensure_loaded()
        return super().__iter__()


AGENT_DISPLAY = _LazyAgentDisplay()

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