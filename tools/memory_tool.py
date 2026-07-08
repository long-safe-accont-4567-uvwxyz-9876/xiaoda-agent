from typing import Any
import time
from tool_engine.tool_registry import register_tool, ToolPermission, ToolResult
from loguru import logger
from utils.metrics import metrics

# 模块级 MemoryManager 单例（由 agent_core.init() 注入）
_memory_manager = None


def bind(memory_manager: Any) -> None:
    """由 agent_core.init() 调用，注入已初始化的 MemoryManager 实例"""
    global _memory_manager
    _memory_manager = memory_manager
    logger.info("memory_tool.bound", has_vec=memory_manager.vec is not None)


def _get_memory_manager() -> Any:
    """获取已注入的 MemoryManager 实例，未注入则抛异常"""
    if _memory_manager is None:
        raise RuntimeError("MemoryManager 未初始化，请确认 agent_core.init() 已调用 bind()")
    return _memory_manager


@register_tool(
    name="remember",
    description="保存一条重要记忆。当用户明确要求你记住某件事、纠正你的错误认知、告知个人偏好或重要信息时使用",
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
    _start = time.time()
    try:
        mm = _get_memory_manager()
        # 使用 MemoryManager.memory.insert_episodic_memory 写入
        mem_id = await mm.memory.insert_episodic_memory(
            summary=content,
            importance=importance,
            emotion_label=tags.split(",")[0].strip() if tags else "",
        )

        # 同步写入向量索引
        if mm.vec and content:
            try:
                await mm.vec.upsert(mem_id, content)
            except Exception as ve:
                logger.warning("memory_tool.vec_upsert_failed", error=str(ve))

        metrics.inc("memory.remember.success")
        metrics.observe("memory.remember.latency_ms", (time.time() - _start) * 1000)
        return ToolResult.ok(f"已记住（ID: {mem_id}）：{content[:50]}")
    except Exception as e:
        metrics.inc("memory.remember.failure")
        logger.error("memory_tool.remember_failed", error=str(e))
        return ToolResult.fail(f"保存记忆失败：{e!s}")


@register_tool(
    name="recall",
    description="检索相关记忆。当用户问到之前聊过的内容、自身配置（如模型版本、系统设置）、用户偏好等不确定的信息时，必须先用此工具查询，不要凭印象编造",
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
    _start = time.time()
    try:
        mm = _get_memory_manager()
        results = await mm.retrieve_memories(query, k=top_k)

        if not results:
            metrics.inc("memory.recall.miss")
            metrics.observe("memory.recall.latency_ms", (time.time() - _start) * 1000)
            return ToolResult.ok("没有找到相关记忆")

        # 格式化输出
        formatted = []
        for r in results:
            summary = r.get("summary", "")
            score = r.get("effective_score", r.get("score", 0))
            importance = r.get("importance", 0)
            mem_id = r.get("id", "?")
            ts = r.get("timestamp", 0)
            time_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "未知时间"
            formatted.append(f"[{time_str}] (ID:{mem_id} 重要度:{importance:.1f} 相关度:{score:.2f}) {summary}")

        output = "\n".join(formatted)
        metrics.inc("memory.recall.hit")
        metrics.observe("memory.recall.latency_ms", (time.time() - _start) * 1000)
        return ToolResult.ok(output)
    except Exception as e:
        metrics.inc("memory.recall.failure")
        logger.error("memory_tool.recall_failed", error=str(e))
        return ToolResult.fail(f"检索记忆失败：{e!s}")


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
        mm = _get_memory_manager()
        # 先检索定位记忆
        results = await mm.retrieve_memories(query, k=1)
        if not results:
            return ToolResult.ok("没有找到相关记忆")

        target = results[0]
        mem_id = target.get("id")
        if mem_id is None:
            return ToolResult.fail("无法定位要删除的记忆 ID")

        # 统一删除：先删向量，再删记忆
        await mm.memory.delete_memory_with_vector(mem_id, vector_store=mm.vec)
        metrics.inc("memory.forget.success")
        logger.info("memory_tool.forgotten", mem_id=mem_id, summary=target.get("summary", "")[:50])
        return ToolResult.ok("已忘记相关内容")
    except Exception as e:
        metrics.inc("memory.forget.failure")
        logger.error("memory_tool.forget_failed", error=str(e))
        return ToolResult.fail(f"删除记忆失败：{e!s}")
