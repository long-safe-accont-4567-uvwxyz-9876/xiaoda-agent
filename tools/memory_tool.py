import os
import json
from tool_registry import register_tool, ToolPermission, ToolResult
from loguru import logger


@register_tool(
    name="remember",
    description="保存一条记忆，用于记住重要信息",
    schema={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "要记住的内容"},
            "tags": {"type": "string", "description": "标签，用逗号分隔", "default": ""},
            "importance": {"type": "number", "description": "重要程度(0-1)", "default": 0.5},
        },
        "required": ["content"],
    },
    permission=ToolPermission.READ_WRITE,
    category="memory",
    max_frequency=5,
)
async def remember(content: str, tags: str = "", importance: float = 0.5) -> ToolResult:
    try:
        from config import load_config
        from memory_manager import MemoryManager
        config = load_config()
        memory = MemoryManager(config)
        await memory.init()
        row_id = await memory.remember(content, tags, importance)
        await memory.close()
        return ToolResult.ok(f"已记住（ID: {row_id}）：{content[:50]}")
    except Exception as e:
        return ToolResult.fail(f"保存记忆失败：{str(e)}")


@register_tool(
    name="recall",
    description="检索相关记忆",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索关键词"},
            "top_k": {"type": "integer", "description": "返回数量", "default": 5},
        },
        "required": ["query"],
    },
    permission=ToolPermission.READ_ONLY,
    category="memory",
    max_frequency=10,
)
async def recall(query: str, top_k: int = 5) -> ToolResult:
    try:
        from config import load_config
        from memory_manager import MemoryManager
        config = load_config()
        memory = MemoryManager(config)
        await memory.init()
        results = await memory.retrieve(query, top_k=top_k)
        await memory.close()
        if not results:
            return ToolResult.ok("没有找到相关记忆")
        return ToolResult.ok(results)
    except Exception as e:
        return ToolResult.fail(f"检索记忆失败：{str(e)}")


@register_tool(
    name="forget",
    description="删除一条记忆",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "要忘记的内容关键词"},
        },
        "required": ["query"],
    },
    permission=ToolPermission.READ_WRITE,
    category="memory",
    max_frequency=3,
)
async def forget(query: str) -> ToolResult:
    try:
        from config import load_config
        from memory_manager import MemoryManager
        config = load_config()
        memory = MemoryManager(config)
        await memory.init()
        result = await memory.forget(query)
        await memory.close()
        if result:
            return ToolResult.ok(f"已忘记相关内容")
        return ToolResult.ok("没有找到相关记忆")
    except Exception as e:
        return ToolResult.fail(f"删除记忆失败：{str(e)}")
