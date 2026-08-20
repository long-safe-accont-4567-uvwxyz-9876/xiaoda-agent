"""工具调用过程 → WebSocket 可视化事件（ToolCallCard 数据源）。

agent_core._execute_tool_with_hooks 中调用；无 WebUI 连接时为 no-op。
"""
from __future__ import annotations

import contextvars
import json
from loguru import logger

# 当前请求关联的 msg_id（由 ws_hub 在 process 前设置，使工具事件能对上消息气泡）
current_msg_id: contextvars.ContextVar[str] = contextvars.ContextVar("ws_msg_id", default="")


async def emit_tool_event(phase: str, tool_name: str, arguments: dict | None = None,
                          ok: bool | None = None, elapsed_ms: int | None = None) -> None:
    """工具事件可视化推送。

    修复：本函数绝不在工具执行路径上 await 广播。慢/挂起 WebSocket 连接会让
    broadcast 阻塞数秒，直接拖慢工具本身（实测 list_stickers 在 WebUI 连接存在时
    被拖到 10s，进而吃掉验证循环墙钟导致降级）。
    用 _spawn 后台推送：任务被跟踪引用（不丢失、不被 GC），事件仍会送达，仅异步。
    """
    try:
        from web.ws_hub import manager
        if manager.active_count == 0:
            return
        preview = ""
        if arguments:
            try:
                preview = json.dumps(arguments, ensure_ascii=False)[:200]
            except (TypeError, ValueError):
                preview = str(arguments)[:200]
        event = {
            "type": "tool_event",
            "msg_id": current_msg_id.get(),
            "phase": phase,
            "tool": tool_name,
            "args_preview": preview,
            "ok": ok,
            "elapsed_ms": elapsed_ms,
        }
        # fire-and-forget：可视化推送不阻塞工具执行；_spawn 保证任务被跟踪不丢
        from core.background_tasks import _spawn
        _spawn(manager.broadcast(event))
    except (RuntimeError, OSError, ConnectionError, ImportError):
        logger.debug("tool_events.emit_error", exc_info=True)