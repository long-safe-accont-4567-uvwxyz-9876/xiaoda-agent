"""插件 SDK — Plugin ABC + 装饰器"""
from __future__ import annotations

from abc import ABC
from typing import Any, Callable


from plugins.context import PluginContext


# ── 装饰器元数据标记 ──

def register_tool(name: str, description: str = "", schema: dict | None = None) -> Any:
    """装饰器：标记方法为 LLM 可调用工具"""
    def decorator(func: Callable) -> Callable:
        func.__plugin_tool__ = {
            "name": name,
            "description": description,
            "schema": schema,
        }
        return func
    return decorator


def subscribe(event_type: str) -> Any:
    """装饰器：标记方法为事件处理器"""
    def decorator(func: Callable) -> Callable:
        func.__plugin_sub__ = {
            "event_type": event_type,
        }
        return func
    return decorator


class Plugin(ABC):
    """插件基类"""

    # 类属性，由 __init_subclass__ 填充
    __plugin_tool_declarations__: list[dict] = []
    __plugin_sub_declarations__: list[dict] = []

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # 收集装饰器声明的工具和事件订阅
        tools = []
        subs = []
        for attr_name in dir(cls):
            try:
                attr = getattr(cls, attr_name)
            except AttributeError:
                continue
            if callable(attr):
                tool_meta = getattr(attr, "__plugin_tool__", None)
                if tool_meta:
                    tools.append({**tool_meta, "method_name": attr_name, "handler": attr})
                sub_meta = getattr(attr, "__plugin_sub__", None)
                if sub_meta:
                    subs.append({**sub_meta, "method_name": attr_name, "handler": attr})
        cls.__plugin_tool_declarations__ = tools
        cls.__plugin_sub_declarations__ = subs

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None

    @property
    def ctx(self) -> PluginContext:
        if self._ctx is None:
            raise RuntimeError("Plugin context not set. Call bind() first.")
        return self._ctx

    def bind(self, context: PluginContext) -> None:
        """绑定 PluginContext"""
        self._ctx = context

    # ── 生命周期钩子（子类可选覆盖）──

    async def on_load(self) -> None:
        """首次启用前调用（一次性初始化）"""
        pass

    async def on_enable(self) -> None:
        """每次启用时调用"""
        pass

    async def on_disable(self) -> None:
        """每次禁用时调用"""
        pass

    async def on_unload(self) -> None:
        """卸载时调用"""
        pass

    # ── 装饰器注册绑定 ──

    def activate_registrations(self) -> None:
        """激活装饰器声明的工具和事件订阅"""
        if self._ctx is None:
            return
        for decl in self.__plugin_tool_declarations__:
            handler = decl["handler"]
            # 绑定 self 到方法
            bound_handler = handler.__get__(self, type(self))
            self._ctx.register_tool(
                name=decl["name"],
                handler=bound_handler,
                description=decl.get("description", ""),
                schema=decl.get("schema"),
            )
        for decl in self.__plugin_sub_declarations__:
            handler = decl["handler"]
            bound_handler = handler.__get__(self, type(self))
            self._ctx.subscribe(
                event_type=decl["event_type"],
                handler=bound_handler,
            )

    def deactivate_registrations(self) -> None:
        """停用装饰器声明的工具和事件订阅"""
        if self._ctx is None:
            return
        self._ctx.clear_registrations()
