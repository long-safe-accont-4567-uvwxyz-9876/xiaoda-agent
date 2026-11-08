"""审计 H1：XP 单例的构造与同步读盘必须离开事件循环线程。

背景（2026-11-08 只读审计）：web/server.py:_warm_xp 在协程里直接调用
`get_xp_system()`，其构造函数内部 `json.load` 生产库 xp_state.json（449KB），
读盘落在事件循环线程上——2026-11-08 18:01:36 实测
`event_loop.blocked lag=10.5s`，MainThread 栈终止于 core/xp_system.py:321
`json.load`，随后 18:01:51 放大成 `loop.lag_detected lag_ms=24393`。
web/ws_hub.py 的 `asyncio.to_thread(get_xp_system().add_chat_xp, ...)`
把构造写在实参里，同样在循环线程求值。

本测试用"调用时是否有 running loop"作为判据：只要 get_xp_system() 在事件
循环线程被求值，就会抛 AssertionError。
"""
from __future__ import annotations

import asyncio
import os
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core._shared import ProcessResult


def _in_loop_thread() -> bool:
    """当前线程是否正跑着事件循环。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.mark.asyncio
async def test_warm_xp_system_constructs_off_loop():
    """_warm_xp_system() 必须在工作线程里构造 XPSystem（同步读盘不得上循环）。"""
    from web import server as server_mod

    seen: dict[str, object] = {}
    loop_thread = threading.get_ident()

    class _StubXPSystem:
        def get_state(self, user_id: str):
            return None

    def _fake_get_xp_system():
        seen["thread"] = threading.get_ident()
        assert not _in_loop_thread(), "get_xp_system 在事件循环线程被求值"
        return _StubXPSystem()

    with patch("core.xp_system.get_xp_system", _fake_get_xp_system):
        xp = await server_mod._warm_xp_system()

    assert isinstance(xp, _StubXPSystem)
    assert seen["thread"] != loop_thread


@pytest.mark.asyncio
async def test_prewarm_local_singletons_moves_xp_off_loop(monkeypatch):
    """_prewarm_local_singletons 端到端：XP 预热阶段不得在循环线程读盘。"""
    from web import server as server_mod

    calls: list[bool] = []

    class _StubXPSystem:
        def get_state(self, user_id: str):
            return None

    def _fake_get_xp_system():
        calls.append(_in_loop_thread())
        return _StubXPSystem()

    # 其余三个预热与 XP 无关，替换成空实现，只验证 XP 这一路（同时避免
    # 测试去连生产库）。
    monkeypatch.setattr("core.mental_state.get_mental_state_manager",
                        lambda: MagicMock())
    monkeypatch.setattr("core.constraint_injector.search_constraint_lessons",
                        lambda *a, **kw: [])
    with patch("core.xp_system.get_xp_system", _fake_get_xp_system):
        await server_mod._prewarm_local_singletons(MagicMock())

    assert calls, "XP 预热没有被执行"
    assert calls == [False], f"XP 预热在循环线程执行: {calls}"


@pytest.mark.asyncio
async def test_ws_hub_subagent_xp_off_loop():
    """ws_hub 子 agent 路径的 get_xp_system() 也必须在工作线程求值。"""
    from web.ws_hub import process_and_serialize

    mock_core = MagicMock()
    mock_core.dispatch_web_sub_agent = AsyncMock(
        return_value=ProcessResult(
            reply="test reply", emotion=None, sticker_path=None,
            audio_path=None, image_paths=[], video_path=None,
            tts_pending=False, tts_text=""),
    )
    mock_registry = MagicMock()
    mock_registry.is_enabled.return_value = True
    mock_core.dispatcher.get_agent.return_value = MagicMock()
    mock_app = MagicMock()
    mock_app.state.agent_registry = mock_registry

    seen: list[bool] = []

    def _fake_get_xp_system():
        seen.append(_in_loop_thread())
        inst = MagicMock()
        inst.add_chat_xp = MagicMock()
        return inst

    os.environ.setdefault("MASTER_QQ_OPENID", "webui_test")
    with patch("core.xp_system.get_xp_system", _fake_get_xp_system):
        result = await process_and_serialize(
            core=mock_core, text="你好", session_id="test_session",
            agent="kimi", app=mock_app,
        )

    assert result is not None
    assert seen, "子 agent 路径没有触发 XP 增加"
    assert seen == [False], f"XP 构造在事件循环线程执行: {seen}"
