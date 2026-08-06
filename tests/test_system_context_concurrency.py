"""Bug 2 (P0-2): _system_context 是单例实例属性 → 并发覆写

根因：AgentCore 是单例，self._system_context 是实例属性。
  主动问候(nudge_engine)与用户消息并发时互相覆写 _system_context。

修复：用 contextvars.ContextVar 替代实例属性，实现请求级隔离。
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.message_processor import MessageProcessorMixin


def test_system_context_var_exists():
    """模块级必须定义 _system_context_var: ContextVar。"""
    from agent_core import message_processor as mp

    var = getattr(mp, "_system_context_var", None)
    assert var is not None, "message_processor 模块必须定义 _system_context_var"
    assert isinstance(var, ContextVar), f"_system_context_var 应为 ContextVar，实际为 {type(var)}"


@pytest.mark.asyncio
async def test_build_main_messages_reads_from_context_var_not_instance():
    """_build_main_messages 应从 ContextVar 读取 system_context，而非实例属性。

    场景：ContextVar 设为 "SCENE_FROM_VAR"，实例属性设为 "WRONG_FROM_INSTANCE"。
    修复后应注入 "SCENE_FROM_VAR"（ContextVar 值），而非 "WRONG_FROM_INSTANCE"。
    """
    from agent_core import message_processor as mp

    proc = MagicMock()
    proc.context = MagicMock()
    # _build_main_messages 内部 await context.build_messages(...)，需 AsyncMock
    proc.context.build_messages = AsyncMock(return_value=[
        {"role": "system", "content": "base prompt"},
        {"role": "user", "content": "hello"},
    ])
    proc._inject_image_description = AsyncMock(side_effect=lambda msgs, *a, **kw: msgs)
    proc._prepare_sticker_and_tools = MagicMock(return_value=(None, None))
    proc.sticker_manager = MagicMock()
    proc.sticker_manager.available = False
    proc.router = MagicMock()
    # 实例属性设为"错误"值——如果代码读实例属性，会注入这个
    proc._system_context = "WRONG_FROM_INSTANCE"

    # ContextVar 设为"正确"值——修复后代码应读这个
    token = mp._system_context_var.set("SCENE_FROM_VAR")
    try:
        messages, _, _ = await MessageProcessorMixin._build_main_messages(
            proc, "你好", True, None, "你好", {"primary": "neutral"},
            "qq_123", "qq",
        )
    finally:
        mp._system_context_var.reset(token)

    # 查找注入的 system_context 消息
    sys_msgs = [m for m in messages if m.get("role") == "system" and "SCENE_FROM_VAR" in m.get("content", "")]
    assert len(sys_msgs) == 1, (
        f"应从 ContextVar 读取并注入 'SCENE_FROM_VAR'，messages={messages}. "
        f"如果注入了 'WRONG_FROM_INSTANCE' 说明仍在读实例属性（Bug 未修复）"
    )

    # 确保没有注入实例属性的值
    wrong_msgs = [m for m in messages if "WRONG_FROM_INSTANCE" in m.get("content", "")]
    assert len(wrong_msgs) == 0, "不应从实例属性读取 system_context"


@pytest.mark.asyncio
async def test_concurrent_tasks_have_isolated_system_context():
    """两个并发 Task 各自 set ContextVar，互不覆写。

    模拟 AgentCore 单例并发处理两条消息：
    Task A → system_context="GREETING_SCENE"
    Task B → system_context=""
    两个 Task 应各自读到自己设置的值。
    """
    from agent_core import message_processor as mp

    results = {}

    async def task_a():
        mp._system_context_var.set("GREETING_SCENE")
        await asyncio.sleep(0.05)  # 让 Task B 也 set
        results["a"] = mp._system_context_var.get()

    async def task_b():
        mp._system_context_var.set("")
        await asyncio.sleep(0.05)
        results["b"] = mp._system_context_var.get()

    # 清空默认值
    token = mp._system_context_var.set("")
    try:
        await asyncio.gather(
            asyncio.create_task(task_a()),
            asyncio.create_task(task_b()),
        )
    finally:
        mp._system_context_var.reset(token)

    assert results["a"] == "GREETING_SCENE", (
        f"Task A 应读到自己设置的 'GREETING_SCENE'，实际为 '{results['a']}'——"
        f"说明 ContextVar 未实现 Task 级隔离（并发覆写 Bug）"
    )
    assert results["b"] == "", (
        f"Task B 应读到自己设置的 ''，实际为 '{results['b']}'"
    )
