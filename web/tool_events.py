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
    try:
        from web.ws_hub import manager
        if manager.active_count == 0:
            return
        preview = ""
        if arguments:
            try:
                preview = json.dumps(arguments, ensure_ascii=False)[:200]
            except Exception:
                preview = str(arguments)[:200]
        await manager.broadcast({
            "type": "tool_event",
            "msg_id": current_msg_id.get(),
            "phase": phase,
            "tool": tool_name,
            "args_preview": preview,
            "ok": ok,
            "elapsed_ms": elapsed_ms,
        })
    except Exception:
        logger.debug("tool_events.emit_error", exc_info=True)
