from typing import Any


class _ToolProxy:
    """工具元数据的轻量代理。

    ``func`` 对懒注册工具为占位 None，访问时按需解析（import 实现模块并回填缓存）。
    元数据字段（name/description/schema/permission/category/max_frequency）在构造时
    即可用，无需触发懒加载。
    """

    def __init__(self, data: dict) -> None:
        self.name = data["name"]
        self.description = data["description"]
        self.schema = data["schema"]
        self.permission = data["permission"]
        self.category = data["category"]
        self.max_frequency = data["max_frequency"]
        self._data = data

    @property
    def func(self) -> Any:
        """按需解析并返回工具的可调用实现（懒加载）。"""
        from tool_engine.tool_registry import resolve_tool_func
        func, _err = resolve_tool_func(self._data)
        return func


def get_all_tools() -> Any:
    """返回所有已注册工具的代理列表。

    不再 import 任何 tools.* 子模块（避免触发重依赖冷启动），改为依赖
    ``register_builtin_tools_lazy`` 登记的元数据。幂等。
    """
    from tool_engine.tool_registry import get_all_tool_dicts, register_builtin_tools_lazy
    register_builtin_tools_lazy()
    return [_ToolProxy(t) for t in get_all_tool_dicts().values()]
