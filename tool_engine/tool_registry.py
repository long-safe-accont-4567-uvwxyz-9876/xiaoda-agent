from dataclasses import dataclass
from typing import Any
from enum import Enum
import threading

from utils.metrics import metrics


class ToolPermission(Enum):
    """工具权限级别枚举。"""
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
_schema_lock = threading.Lock()

# ── 工具数量上限管理 ──────────────────────────────────────
# 聊天 agent 优先保证对话流畅，工具不宜过多（DeepSeek function calling 舒适区间）
MAX_ENABLED_TOOLS = 60

# 来源优先级：builtin > plugin > mcp > dynamic
_SOURCE_PRIORITY: dict[str, int] = {
    "builtin": 100,
    "plugin": 50,
    "mcp": 30,
    "dynamic": 20,
    "sdk_mcp": 30,
}

# 分类优先级：对话/情感/记忆 > 文件/代码 > 系统/网络
_CATEGORY_PRIORITY: dict[str, int] = {
    "emotion": 95,
    "memory": 90,
    "conversation": 85,
    "knowledge": 80,
    "file": 60,
    "code": 55,
    "document": 50,
    "web": 40,
    "system": 30,
    "hardware": 25,
    "mcp": 20,
    "general": 10,
}


_V2_REQUIRED_SECTIONS = [
    ("功能概述", "overview"),
    ("使用场景", "usage"),
    ("参数约束", "parameters"),
    ("返回格式", "returns"),
    ("错误码", "errors"),
    ("注意事项", "notes"),
]


def _validate_schema_v2(name: str, schema: dict, description: str) -> None:
    """校验工具 Schema 是否符合 V2 规范，仅记录警告不阻断注册.

    每个段落接受中文或英文标记（如 [功能概述] 或 [overview]）。
    """
    try:
        from loguru import logger as _logger
    except ImportError:
        _logger = None
    for cn_section, en_section in _V2_REQUIRED_SECTIONS:
        if f"[{cn_section}]" not in (description or "") and f"[{en_section}]" not in (description or ""):
            if _logger:
                _logger.warning("tool_registry.schema_v2_missing_section tool={} section={}", name, cn_section)  # noqa: PLE1205
    props = schema.get("properties", {})
    for prop_name, prop_def in props.items():
        desc = prop_def.get("description", "")
        if not desc or len(desc) < 10:
            if _logger:
                _logger.warning("tool_registry.schema_v2_short_prop_desc tool={} prop={} desc={}", name, prop_name, repr(desc))  # noqa: PLE1205


def register_tool(name: str, description: str, schema: dict,
                  permission: ToolPermission = ToolPermission.READ_ONLY,
                  category: str = "general",
                  max_frequency: int = 10,
                  requires_confirmation: bool = False,
                  source: str = "builtin",
                  plugin_id: str = "",
                  version: str = "",
                  model_overrides: dict | None = None,
                  schema_v2: int = 1,
                  pre_call_hook=None,
                  post_call_hook=None) -> Any:
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
        model_overrides: 按模型家族覆盖 description/schema
        schema_v2: 2 表示符合 V2 规范（含完整 description 段落 + 错误码）
        pre_call_hook: 调用前钩子(参数校验/路径校验)
        post_call_hook: 调用后钩子(格式转换/错误恢复)

    Returns:
        装饰器函数
    """
    if schema_v2 >= 2:
        _validate_schema_v2(name, schema, description)

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
            "model_overrides": model_overrides,
            "schema_v2": schema_v2,
            "pre_call_hook": pre_call_hook,
            "post_call_hook": post_call_hook,
        }
        with _schema_lock:
            _schema_version += 1
            _schema_cache = None
        return func
    return decorator


def register_lazy_tool(name: str, description: str, schema: dict,
                       module_path: str, func_name: str,
                       permission: ToolPermission = ToolPermission.READ_ONLY,
                       category: str = "general",
                       max_frequency: int = 10,
                       requires_confirmation: bool = False,
                       source: str = "builtin",
                       plugin_id: str = "",
                       version: str = "") -> None:
    """程序化延迟注册工具：仅登记元数据，func 留空，首次调用时按需 import。

    与 ``register_tool`` 装饰器写入的字典结构一致（func 字段为 None 占位），
    额外附加 ``_lazy``/``module_path``/``func_name`` 标记，供 ``resolve_tool_func``
    在首次调用时解析真实实现。``to_openai_tools``/``list_tools``/``get_tool`` 只读
    元数据字段，不读 func，因此延迟工具在加载前即可对外暴露完整 schema。
    """
    global _schema_cache, _schema_version
    _tools[name] = {
        "name": name,
        "description": description,
        "schema": schema,
        "permission": permission,
        "category": category,
        "max_frequency": max_frequency,
        "requires_confirmation": requires_confirmation,
        "func": None,
        "source": source,
        "plugin_id": plugin_id,
        "version": version,
        "_lazy": True,
        "module_path": module_path,
        "func_name": func_name,
    }
    with _schema_lock:
        _schema_version += 1
        _schema_cache = None


def register_builtin_tools_lazy() -> None:
    """登记所有内置工具的元数据（懒加载），不 import 任何 tools.* 子模块。幂等。

    重复调用安全：若工具已存在（无论是懒注册还是已被真实解析），均跳过不覆盖，
    避免把已解析的 func 重新置回懒占位。
    """
    from tools._builtin_manifest import BUILTIN_TOOLS
    for entry in BUILTIN_TOOLS:
        name = entry["name"]
        existing = _tools.get(name)
        if existing is not None:
            # 已存在（懒注册或已解析）一律保留，不覆盖
            continue
        register_lazy_tool(
            name=name,
            description=entry["description"],
            schema=entry["schema"],
            module_path=entry["module_path"],
            func_name=entry["func_name"],
            permission=entry["permission"],
            category=entry.get("category", "general"),
            max_frequency=entry.get("max_frequency", 10),
            requires_confirmation=entry.get("requires_confirmation", False),
            source=entry.get("source", "builtin"),
            plugin_id=entry.get("plugin_id", ""),
            version=entry.get("version", ""),
        )


def resolve_tool_func(tool: dict) -> tuple[Any, str]:
    """解析并返回工具的可调用 func。

    对懒注册工具按需 ``importlib.import_module(module_path)`` 并
    ``getattr(module, func_name)`` 取真实实现，回填缓存（``_lazy`` 置 False），
    后续调用直接复用。优先用 getattr 直接拿 func，不依赖模块内 ``@register_tool``
    装饰器的覆写副作用，避免竞态。

    Returns:
        (func, error)：func 非 None 即成功；失败时 func 为 None，error 为原因。
    """
    if not tool.get("_lazy"):
        return tool.get("func"), ""
    import importlib
    try:
        module = importlib.import_module(tool["module_path"])
        func = getattr(module, tool["func_name"])
    except Exception as e:
        return None, f"加载工具实现失败 ({tool.get('name')}): {e}"
    tool["func"] = func
    tool["_lazy"] = False
    return func, ""


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
    with _schema_lock:
        _schema_version += 1
        _schema_cache = None


def get_tool(name: str) -> dict | None:
    """按名称获取工具定义, 不存在返回 None."""
    return _tools.get(name)


def invalidate_schema_cache() -> None:
    """强制使工具 schema 缓存失效，下次 to_openai_tools() 会重新构建。"""
    global _schema_cache
    with _schema_lock:
        _schema_cache = None


def list_tools() -> list[dict]:
    """返回所有已注册工具的列表."""
    return list(_tools.values())


def _tool_priority(tool: dict) -> int:
    """计算工具优先级分数（越高越优先保留）"""
    source_score = _SOURCE_PRIORITY.get(tool.get("source", ""), 10)
    cat_score = _CATEGORY_PRIORITY.get(tool.get("category", ""), 10)
    return source_score + cat_score


def to_openai_tools() -> list[dict]:
    """生成 OpenAI function-calling 格式的工具列表 (带缓存，受上限限制)."""
    global _schema_cache
    with _schema_lock:
        if _schema_cache is not None:
            metrics.inc("tool_registry.schema_cache.hit")
            return _schema_cache
    metrics.inc("tool_registry.schema_cache.miss")

    enabled = []
    for t in _tools.values():
        if t.get("max_frequency", 0) == 0:
            continue
        if t.get("enabled") is False:
            continue
        enabled.append(t)

    enabled.sort(key=_tool_priority, reverse=True)
    capped = enabled[:MAX_ENABLED_TOOLS]

    result = []
    for t in capped:
        result.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["schema"],
            }
        })

    if len(enabled) > MAX_ENABLED_TOOLS:
        metrics.inc("tool_registry.tools_capped")

    with _schema_lock:
        _schema_cache = result
    return result


def get_tool_stats() -> dict:
    """获取工具注册统计信息"""
    sources: dict[str, int] = {}
    categories: dict[str, int] = {}
    enabled_count = 0
    for t in _tools.values():
        src = t.get("source", "unknown")
        cat = t.get("category", "unknown")
        sources[src] = sources.get(src, 0) + 1
        categories[cat] = categories.get(cat, 0) + 1
        if t.get("max_frequency", 0) != 0 and t.get("enabled") is not False:
            enabled_count += 1
    return {
        "total": len(_tools),
        "enabled": enabled_count,
        "max_enabled": MAX_ENABLED_TOOLS,
        "remaining": max(0, MAX_ENABLED_TOOLS - enabled_count),
        "by_source": sources,
        "by_category": categories,
    }


def get_all_tool_dicts() -> dict[str, dict]:
    """公共访问函数：返回内部工具字典的浅拷贝"""
    return dict(_tools)


def clear_tools() -> None:
    """清空所有已注册工具并重置 schema 缓存."""
    global _schema_cache, _schema_version
    _tools.clear()
    with _schema_lock:
        _schema_version += 1
        _schema_cache = None


def unregister_tool(name: str) -> bool:
    """移除指定工具，返回是否成功移除"""
    global _schema_cache, _schema_version
    if name in _tools:
        del _tools[name]
        with _schema_lock:
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
        with _schema_lock:
            _schema_version += 1
            _schema_cache = None
    return removed
