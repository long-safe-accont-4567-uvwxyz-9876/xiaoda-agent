"""L5 修复测试: model_used 必须传入 insert_conversation_log

根因: core/background_tasks.py 的 _run_persistence_tasks 调用
insert_conversation_log 时从未传入 model_used，导致生产数据库
（/media/orangepi/KIOXIA/nahida-data/db/agent.db）1793 条对话记录中
1792 条 model_used 字段为空，无法追溯每条回复使用的 LLM 模型。

修复: 在 run_background_tasks / _background_tasks / _run_persistence_tasks
签名中增加 model_used 参数，并透传到 insert_conversation_log。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.background_tasks import BackgroundTaskManager


def _make_mgr() -> tuple[BackgroundTaskManager, MagicMock]:
    """构造一个最小可用的 BackgroundTaskManager（memory=None 跳过编码任务）"""
    mock_db = MagicMock()
    mock_db.insert_conversation_log = AsyncMock()
    mock_db.update_session = AsyncMock()
    mock_db.commit = AsyncMock()

    mock_context = MagicMock()
    mock_context.history = []  # 长度 < 4，跳过 memory encode

    mgr = BackgroundTaskManager(
        db=mock_db,
        context=mock_context,
        memory=None,
    )
    return mgr, mock_db


@pytest.mark.asyncio
async def test_model_used_passed_to_conversation_log():
    """_run_persistence_tasks 必须把 model_used 传给 insert_conversation_log"""
    mgr, mock_db = _make_mgr()

    await mgr._run_persistence_tasks(
        user_input="你好",
        reply="你好呀～",
        user_id="test_user",
        source="qq_c2c",
        emotion={"primary": "happy"},
        session_id="",
        model_used="mimo-v2.5",
    )

    mock_db.insert_conversation_log.assert_awaited_once()
    call_kwargs = mock_db.insert_conversation_log.await_args.kwargs
    assert call_kwargs.get("model_used") == "mimo-v2.5", \
        f"model_used 未正确透传，实际: {call_kwargs.get('model_used')!r}"


@pytest.mark.asyncio
async def test_model_used_defaults_to_empty():
    """不传 model_used 时默认空字符串（向后兼容）"""
    mgr, mock_db = _make_mgr()

    await mgr._run_persistence_tasks(
        user_input="hi",
        reply="hello",
        user_id="u1",
        source="test",
        emotion={},
        session_id="",
    )

    call_kwargs = mock_db.insert_conversation_log.await_args.kwargs
    assert call_kwargs.get("model_used") == ""


@pytest.mark.asyncio
async def test_run_background_tasks_threads_model_used():
    """run_background_tasks 接受 model_used 参数并透传给 _background_tasks。

    patch _background_tasks 为 AsyncMock 捕获参数，避免触发 _run_scheduled_tasks
    的 DB 依赖。run_background_tasks 内部用 _spawn 创建协程，我们拦截 _spawn
    拿到协程对象但不 await（仅检查它能否无异常创建 + 参数链路）。
    """
    mgr, mock_db = _make_mgr()

    # 用 AsyncMock 替换 _background_tasks，捕获参数（run_background_tasks 用位置参数调用）
    captured_kwargs: dict = {}

    async def _fake_background_tasks(*args, **kwargs):
        # 将位置参数按 _background_tasks 签名映射到 kwargs 便于断言
        _names = ["user_input", "reply", "user_id", "source", "emotion", "tool_results"]
        for name, val in zip(_names, args):
            captured_kwargs[name] = val
        captured_kwargs.update(kwargs)

    mgr._background_tasks = _fake_background_tasks  # type: ignore[assignment]

    # 拦截 _spawn，直接 await 协程（_background_tasks 已被 mock，不会触发 DB）
    import core.background_tasks as bt

    def _fake_spawn(coro):
        asyncio.get_event_loop().create_task(coro)

    orig_spawn = bt._spawn
    bt._spawn = _fake_spawn
    try:
        mgr.run_background_tasks(
            "你好", "你好呀～", "u1", "qq_c2c", {"primary": "happy"}, [],
            session_id="", model_used="agnes-pro",
        )
        # 让事件循环跑一下，让 _spawn 创建的 task 完成
        await asyncio.sleep(0.05)
    finally:
        bt._spawn = orig_spawn

    assert captured_kwargs.get("model_used") == "agnes-pro", \
        f"run_background_tasks 未透传 model_used，实际: {captured_kwargs}"
    assert captured_kwargs.get("reply") == "你好呀～"
