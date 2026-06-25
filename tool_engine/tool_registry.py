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
    def ok(cls, data: Any, **kwargs) -> "ToolResult":
        return cls(success=True, data=data, **kwargs)

    @classmethod
    def fail(cls, error: str) -> "ToolResult":
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
                  version: str = ""):
    def decorator(func):
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
                         version: str = ""):
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
    return _tools.get(name)


def list_tools() -> list[dict]:
    return list(_tools.values())


def to_openai_tools() -> list[dict]:
    global _schema_cache, _schema_version
    # 确保所有工具模块已导入注册
    import tools.file_tools_v2
    import tools.code_tools_v2
    import tools.web_tools_v2
    import tools.document_tools
    import tools.web_browse_tools
    import tools.web_browse_enhanced
    import tools.multi_search_tools
    import tools.agnes_tools
    import tools.hardware_tools
    import tools.system_tools
    import tools.vision_tools
    import tools.memory_tool
    import tools.nudge_tool
    import tools.domestic_search_tools
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


def clear_tools():
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
