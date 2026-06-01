from dataclasses import dataclass, field
from typing import Any
from enum import Enum


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


def register_tool(name: str, description: str, schema: dict,
                  permission: ToolPermission = ToolPermission.READ_ONLY,
                  category: str = "general",
                  max_frequency: int = 10,
                  requires_confirmation: bool = False):
    def decorator(func):
        _tools[name] = {
            "name": name,
            "description": description,
            "schema": schema,
            "permission": permission,
            "category": category,
            "max_frequency": max_frequency,
            "requires_confirmation": requires_confirmation,
            "func": func,
            "source": "builtin",
        }
        return func
    return decorator


def get_tool(name: str) -> dict | None:
    return _tools.get(name)


def list_tools() -> list[dict]:
    return list(_tools.values())


def to_openai_tools() -> list[dict]:
    result = []
    for t in _tools.values():
        result.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["schema"],
            }
        })
    return result


def clear_tools():
    _tools.clear()
