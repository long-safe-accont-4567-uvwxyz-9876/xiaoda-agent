"""插件 SDK 兼容层 — 精简版"""
from __future__ import annotations

from typing import Any, Callable


class Plugin:
    """插件基类 (兼容层)"""

    _context: Any = None

    def bind(self, context: Any) -> None:
        self._context = context

    def activate_registrations(self) -> None:
        pass

    async def on_load(self) -> None:
        pass

    async def on_enable(self) -> None:
        pass

    async def on_disable(self) -> None:
        pass

    async def on_unload(self) -> None:
        pass


def register_tool(name: str, description: str = "") -> Callable:
    """工具注册装饰器 (兼容层)"""
    def decorator(func: Callable) -> Callable:
        func._tool_name = name
        func._tool_description = description
        return func
    return decorator
