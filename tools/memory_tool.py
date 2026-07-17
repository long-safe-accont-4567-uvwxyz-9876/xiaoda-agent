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
    description="【必须调用】保存记忆。当用户提到：记住、记一下、帮我记、别忘了、不要忘记、记得告诉我。必须调用此工具保存记忆。",
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
        from memory.scope import Scope
        mem_id = await mm.memory.insert_episodic_memory(
            summary=content,
            importance=importance,
            emotion_label=tags.split(",")[0].strip() if tags else "",
            scope=Scope(),
            is_raw=1,
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
    description="【必须调用】检索记忆。当用户提到：之前、以前、上次、记得、回忆、聊过、说过、发生过、我们之间、我们的故事、你还记得吗、你记得吗、回忆一下、帮我回忆。必须调用此工具，绝对不要凭印象回答。",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索关键词"},
            "top_k": {"type": "integer", "description": "返回数量", "default": 8},
        },
        "required": ["query"],
    },
    permission=ToolPermission.READ_ONLY,
    category="memory",
    max_frequency=10,
)
async def recall(query: str, top_k: int = 8) -> ToolResult:
    _start = time.time()
    try:
        mm = _get_memory_manager()
        results = await mm.retrieve_memories(query, k=top_k)

        if not results:
            metrics.inc("memory.recall.miss")
            metrics.observe("memory.recall.latency_ms", (time.time() - _start) * 1000)
            return ToolResult.ok("没有找到相关记忆")

        # 格式化输出：返回 summary + 补充元数据（entities/event_type/metadata）
        # 帮助 LLM 获得更完整的上下文，避免只看到压缩后的 summary 而丢失细节
        formatted = []
        for r in results:
            summary = r.get("summary", "")
            score = r.get("effective_score", r.get("score", 0))
            importance = r.get("importance", 0)
            mem_id = r.get("id", "?")
            ts = r.get("timestamp", 0)
            time_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "未知时间"
            is_raw = r.get("is_raw", 0)
            mem_type = "原始" if is_raw == 1 else "提炼"
            line = f"[{time_str}] (ID:{mem_id} 类型:{mem_type} 重要度:{importance:.1f} 相关度:{score:.2f}) {summary}"
            # 补充实体检索到的实体信息（帮助 LLM 关联上下文）
            entities_raw = r.get("entities", "")
            if entities_raw:
                try:
                    import json as _json
                    ents = _json.loads(entities_raw) if isinstance(entities_raw, str) else entities_raw
                    if isinstance(ents, list) and ents:
                        line += f" | 实体: {', '.join(str(e) for e in ents[:5])}"
                except (ValueError, TypeError):
                    pass
            # 补充事件类型和决策元数据
            event_type = r.get("event_type", "")
            if event_type:
                line += f" | 事件: {event_type}"
            metadata_raw = r.get("metadata_json", "")
            if metadata_raw:
                try:
                    import json as _json
                    meta = _json.loads(metadata_raw) if isinstance(metadata_raw, str) else metadata_raw
                    if isinstance(meta, dict):
                        decision = meta.get("decision", "")
                        topic = meta.get("topic", "")
                        if decision:
                            line += f" | 决策: {decision}"
                        if topic:
                            line += f" | 话题: {topic}"
                except (ValueError, TypeError):
                    pass
            # KG 上下文增强（如果检索阶段附加了相关知识）
            kg_context = r.get("kg_context", "")
            if kg_context:
                line += f" | 知识: {kg_context[:100]}"
            formatted.append(line)

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


@register_tool(
    name="confirm_memory",
    description="确认记忆正确，强化记忆权重。当用户确认某条记忆正确时使用（如用户说\"对/没错/就是这样\"）。"
                "每次确认：节点权重 +0.15，关联边权重 +0.25，access_count +1",
    schema={
        "type": "object",
        "properties": {
            "node_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要确认的概念节点 ID 列表",
            },
        },
        "required": ["node_ids"],
    },
    permission=ToolPermission.READ_WRITE,
    category="memory",
    max_frequency=10,
)
async def confirm_memory(node_ids: list[str]) -> ToolResult:
    try:
        mm = _get_memory_manager()
        if not mm.confirm_correct:
            return ToolResult.fail("confirm/correct 未初始化")
        result = await mm.confirm_correct.confirm(node_ids)
        metrics.inc("memory.confirm.success")
        return ToolResult.ok(result)
    except Exception as e:
        metrics.inc("memory.confirm.failure")
        logger.error("memory_tool.confirm_failed", error=str(e))
        return ToolResult.fail(f"确认记忆失败：{e!s}")


@register_tool(
    name="correct_memory",
    description="纠正错误记忆，创建新版本并保留溯源链。当用户纠正某条记忆时使用（如用户说\"不对/应该是/搞错了\"）。"
                "旧记忆被关闭但保留，新记忆继承权重，confidence×0.7",
    schema={
        "type": "object",
        "properties": {
            "old_hint": {"type": "string", "description": "用于找到旧记忆的查询提示"},
            "new_text": {"type": "string", "description": "纠正后的新内容"},
        },
        "required": ["old_hint", "new_text"],
    },
    permission=ToolPermission.READ_WRITE,
    category="memory",
    max_frequency=5,
)
async def correct_memory(old_hint: str, new_text: str) -> ToolResult:
    try:
        mm = _get_memory_manager()
        if not mm.confirm_correct:
            return ToolResult.fail("confirm/correct 未初始化")
        result = await mm.confirm_correct.correct(old_hint, new_text)
        if "error" in result:
            metrics.inc("memory.correct.no_match")
            return ToolResult.fail(result["error"])
        metrics.inc("memory.correct.success")
        return ToolResult.ok(result)
    except Exception as e:
        metrics.inc("memory.correct.failure")
        logger.error("memory_tool.correct_failed", error=str(e))
        return ToolResult.fail(f"纠正记忆失败：{e!s}")
