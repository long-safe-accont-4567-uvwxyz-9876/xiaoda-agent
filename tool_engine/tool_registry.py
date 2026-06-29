from dataclasses import dataclass, field
from typing import Any
from enum import Enum

from utils.metrics import metrics


class ToolPermission(Enum):
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"
    EXECUTE = "execute"


@dataclass
class ToolResult:
    success: bool
    data: Any = None
    error: str = ""

    @classmethod
    def ok(cls, data: Any, **kwargs: Any) -> "ToolResult":
        """构造成功结果.

        Args:
            data: 返回数据
            **kwargs: 额外字段

        Returns:
            标记为成功的 ToolResult
        """
        return cls(success=True, data=data, **kwargs)

    @classmethod
    def fail(cls, error: str) -> "ToolResult":
        """构造失败结果.

        Args:
            error: 错误描述

        Returns:
            标记为失败的 ToolResult
        """
        return cls(success=False, error=error)


_tools: dict[str, dict] = {}
_schema_cache: list | None = None
_schema_version: int = 0


def register_tool(name: str, description: str, schema: dict,
                  permission: ToolPermission = ToolPermission.READ_ONLY,
                  category: str = "general",
                  max_frequency: int = 10,
                  requires_confirmation: bool = False,
                  source: str = "builtin",
                  plugin_id: str = "",
                  version: str = "") -> Any:
    """装饰器: 注册一个工具函数.

    Args:
        name: 工具名
        description: 工具描述
        schema: JSON schema 参数定义
        permission: 权限级别, 默认 READ_ONLY
        category: 分类, 默认 general
        max_frequency: 最大调用频率, 默认 10
        requires_confirmation: 是否需要确认, 默认 False
        source: 来源 (builtin/dynamic/plugin), 默认 builtin
        plugin_id: 插件标识, 默认空字符串
        version: 版本, 默认空字符串

    Returns:
        装饰器函数
    """
    def decorator(func: Any) -> Any:
        """实际注册函数的装饰器内层."""
        global _schema_cache, _schema_version
        _tools[name] = {
            "name": name,
            "description": description,
            "schema": schema,
            "permission": permission,
            "category": category,
            "max_frequency": max_frequency,
            "requires_confirmation": requires_confirmation,
            "func": func,
            "source": source,
            "plugin_id": plugin_id,
            "version": version,
        }
        _schema_version += 1
        _schema_cache = None
        return func
    return decorator


def register_tool_direct(name: str, description: str, func: callable,
                         parameters: dict, permission: ToolPermission = ToolPermission.READ_ONLY,
                         category: str = "general",
                         source: str = "dynamic",
                         plugin_id: str = "",
                         version: str = "") -> None:
    """直接注册工具（非装饰器模式），用于程序化注册"""
    global _schema_cache, _schema_version
    _tools[name] = {
        "name": name,
        "description": description,
        "schema": {
            "type": "object",
            **parameters,
        },
        "permission": permission,
        "category": category,
        "max_frequency": 10,
        "requires_confirmation": False,
        "func": func,
        "source": source,
        "plugin_id": plugin_id,
        "version": version,
    }
    _schema_version += 1
    _schema_cache = None


def get_tool(name: str) -> dict | None:
    """按名称获取工具定义, 不存在返回 None."""
    return _tools.get(name)


def list_tools() -> list[dict]:
    """返回所有已注册工具的列表."""
    return list(_tools.values())


def to_openai_tools() -> list[dict]:
    """生成 OpenAI function-calling 格式的工具列表 (带缓存)."""
    global _schema_cache, _schema_version
    # 内置工具模块的注册由 tool_engine/__init__.py 顶层导入 _builtin_tools 完成,
    # 此处不再需要 import tools.* (打破 tool_registry <-> tools.* 静态循环)
    if _schema_cache is not None:
        metrics.inc("tool_registry.schema_cache.hit")
        return _schema_cache
    metrics.inc("tool_registry.schema_cache.miss")
    result = []
    for t in _tools.values():
        if t.get("max_frequency", 0) == 0:
            continue
        result.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["schema"],
            }
        })
    _schema_cache = result
    return result


def get_all_tool_dicts() -> dict[str, dict]:
    """公共访问函数：返回内部工具字典的浅拷贝"""
    return dict(_tools)


def clear_tools() -> None:
    """清空所有已注册工具并重置 schema 缓存."""
    global _schema_cache, _schema_version
    _tools.clear()
    _schema_version += 1
    _schema_cache = None


def unregister_tool(name: str) -> bool:
    """移除指定工具，返回是否成功移除"""
    global _schema_cache, _schema_version
    if name in _tools:
        del _tools[name]
        _schema_version += 1
        _schema_cache = None
        return True
    return False


def unregister_by_plugin(plugin_id: str) -> list[str]:
    """移除指定插件注册的所有工具，返回被移除的工具名列表"""
    global _schema_cache, _schema_version
    removed = [name for name, t in _tools.items() if t.get("plugin_id") == plugin_id]
    for name in removed:
        del _tools[name]
    if removed:
        _schema_version += 1
        _schema_cache = None
    return removed
