"""测试 BackgroundTaskManager._run_persistence_tasks 的批量提交优化。

验证 v3 spec P0-2：insert_conversation_log 与 update_session 合并为单次 commit，
减少 aiosqlite 线程切换次数。try_idle_encode 不纳入批量提交。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.background_tasks import BackgroundTaskManager


def _make_manager(*, memory_enabled: bool = False) -> tuple[BackgroundTaskManager, AsyncMock, AsyncMock]:
    """构造一个仅持有 mock 依赖的 BackgroundTaskManager。

    - db: AsyncMock，提供 insert_conversation_log / update_session / commit
    - context: MagicMock，history 设为空列表以跳过记忆编码分支（聚焦 db 批量提交）
    - memory: AsyncMock，提供 try_idle_encode（仅当 memory_enabled=True 时传入）
    """
    db = AsyncMock()
    db.insert_conversation_log = AsyncMock(return_value=None)
    db.update_session = AsyncMock(return_value=None)
    db.commit = AsyncMock(return_value=None)

    context = MagicMock()
    context.history = []  # 空历史，跳过记忆编码分支

    memory = AsyncMock() if memory_enabled else None

    manager = BackgroundTaskManager(db=db, context=context, memory=memory)
    return manager, db, memory


@pytest.mark.asyncio
async def test_both_writes_success_commit_once():
    """场景一：两个写入均成功 → commit 恰好调用一次（而非两次）。"""
    manager, db, _ = _make_manager()

    await manager._run_persistence_tasks(
        user_input="你好",
        reply="你好呀",
        user_id="u1",
        source="qq",
        emotion={"primary": "happy"},
        session_id="s1",
    )

    # commit 应只被调用一次
    assert db.commit.await_count == 1, "commit 应被调用恰好一次"
    assert db.commit.await_count != 2, "不应每次写入都 commit"

    # insert_conversation_log 被调用且 auto_commit=False
    db.insert_conversation_log.assert_awaited_once()
    _, kwargs = db.insert_conversation_log.call_args
    assert kwargs.get("auto_commit") is False, "insert_conversation_log 应传 auto_commit=False"

    # update_session 被调用且 auto_commit=False
    db.update_session.assert_awaited_once()
    _, kwargs = db.update_session.call_args
    assert kwargs.get("auto_commit") is False, "update_session 应传 auto_commit=False"


@pytest.mark.asyncio
async def test_update_session_fails_commit_still_called():
    """场景二：update_session 抛异常但 conversation_log 成功 → commit 仍被调用一次。"""
    manager, db, _ = _make_manager()
    db.update_session = AsyncMock(side_effect=RuntimeError("db locked"))

    await manager._run_persistence_tasks(
        user_input="在吗",
        reply="在的",
        user_id="u1",
        source="qq",
        emotion={"primary": "calm"},
        session_id="s1",
    )

    # conversation_log 成功 → commit 仍应被调用一次
    assert db.insert_conversation_log.await_count == 1
    assert db.update_session.await_count == 1
    assert db.commit.await_count == 1, "conversation_log 成功时 commit 仍应被调用一次"


@pytest.mark.asyncio
async def test_both_writes_fail_commit_not_called():
    """场景三：两个写入都失败 → commit 不被调用（避免空 commit）。"""
    manager, db, _ = _make_manager()
    db.insert_conversation_log = AsyncMock(side_effect=RuntimeError("disk full"))
    db.update_session = AsyncMock(side_effect=RuntimeError("disk full"))

    await manager._run_persistence_tasks(
        user_input="hi",
        reply="hello",
        user_id="u1",
        source="qq",
        emotion={"primary": "neutral"},
        session_id="s1",
    )

    # 两个写入均失败 → commit 不应被调用
    assert db.insert_conversation_log.await_count == 1
    assert db.update_session.await_count == 1
    assert db.commit.await_count == 0, "两个写入都失败时不应调用 commit"


@pytest.mark.asyncio
async def test_no_session_id_skips_update_session():
    """场景四：session_id 为空 → 不调用 update_session，但 conversation_log 成功仍触发一次 commit。"""
    manager, db, _ = _make_manager()

    await manager._run_persistence_tasks(
        user_input="hi",
        reply="hello",
        user_id="u1",
        source="cli",
        emotion={"primary": "neutral"},
        session_id="",
    )

    db.insert_conversation_log.assert_awaited_once()
    db.update_session.assert_not_called()
    assert db.commit.await_count == 1, "conversation_log 成功应触发一次 commit"


@pytest.mark.asyncio
async def test_memory_encode_not_affected_by_batch_commit():
    """场景五：history 足够长时 try_idle_encode 仍被调用，且独立于批量 commit。

    验证 try_idle_encode 不纳入批量提交（不传 auto_commit，commit 次数仍为 1）。
    
    注意：由于 try_idle_encode 使用 _spawn (fire-and-forget)，测试无法直接检测其调用。
    此测试主要验证 commit 次数不受记忆编码影响。
    """
    manager, db, memory = _make_manager(memory_enabled=True)
    # 设置足够长的 history 以触发记忆编码分支
    manager.context.history = ["x"] * 5
    manager.context.get_last_n = MagicMock(return_value=[("q", "a")] * 3)

    await manager._run_persistence_tasks(
        user_input="记住这些",
        reply="好的",
        user_id="u1",
        source="qq",
        emotion={"primary": "happy"},
        session_id="s1",
    )

    # 记忆编码使用 _spawn (fire-and-forget)，无法在测试中直接检测
    # 主要验证 commit 不受记忆编码影响
    # commit 仍只被调用一次（记忆编码不增加 commit）
    assert db.commit.await_count == 1, "commit 应仅被调用一次，不受记忆编码影响"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
