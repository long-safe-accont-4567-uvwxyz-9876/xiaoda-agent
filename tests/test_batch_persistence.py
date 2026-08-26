"""测试 BackgroundTaskManager._run_persistence_tasks 的批量提交优化。

验证 insert_conversation_log 与 update_session 在同一 write_transaction 内执行
（单事务串行化提交），根治并发脏事务。try_idle_encode 不纳入批量提交。

架构变更（CodeRabbit 事务锁根因修复）：原版手动 insert+update+commit，现改为
db.write_transaction() 上下文管理器统一管理 commit/rollback。测试用 _TxnTracker
跟踪事务进入/提交次数，替代直接断言 db.commit（commit 已内聚到 write_transaction）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.background_tasks import BackgroundTaskManager


class _TxnTracker:
    """跟踪 write_transaction 的进入/提交/回滚次数。

    替代直接 mock db.commit：commit 已内聚到 write_transaction 的 __aexit__，
    测试通过 entered/committed/rolled_back 验证事务语义而非具体 commit 调用。
    """
    def __init__(self):
        self.entered = 0
        self.committed = 0
        self.rolled_back = 0

    @asynccontextmanager
    async def write_transaction(self):
        self.entered += 1
        try:
            yield MagicMock()  # 模拟 conn，本测试不直接操作连接
            self.committed += 1
        except Exception:
            self.rolled_back += 1
            raise


def _make_manager(*, memory_enabled: bool = False):
    """构造仅持有 mock 依赖的 BackgroundTaskManager。"""
    db = AsyncMock()
    db.insert_conversation_log = AsyncMock(return_value=None)
    db.update_session = AsyncMock(return_value=None)
    # AsyncMock 无法模拟 async context manager，用真实跟踪器替代 write_transaction
    db._txn = _TxnTracker()
    db.write_transaction = db._txn.write_transaction

    context = MagicMock()
    context.history = []  # 空历史，跳过记忆编码分支

    memory = AsyncMock() if memory_enabled else None
    manager = BackgroundTaskManager(db=db, context=context, memory=memory)
    return manager, db, memory


@pytest.mark.asyncio
async def test_both_writes_success_single_transaction():
    """场景一：两个写入均成功 → 单事务（entered=1, committed=1），两条 auto_commit=False。"""
    manager, db, _ = _make_manager()

    await manager._run_persistence_tasks(
        user_input="你好", reply="你好呀", user_id="u1", source="qq",
        emotion={"primary": "happy"}, session_id="s1",
    )

    # 单事务：进入一次、提交一次（批量合并，非两次独立事务）
    assert db._txn.entered == 1, "应进入单次 write_transaction"
    assert db._txn.committed == 1, "应提交一次"
    assert db._txn.rolled_back == 0

    db.insert_conversation_log.assert_awaited_once()
    _, kwargs = db.insert_conversation_log.call_args
    assert kwargs.get("auto_commit") is False, "insert 应传 auto_commit=False"

    db.update_session.assert_awaited_once()
    _, kwargs = db.update_session.call_args
    assert kwargs.get("auto_commit") is False, "update 应传 auto_commit=False"


@pytest.mark.asyncio
async def test_update_session_fails_transaction_still_commits_insert():
    """场景二：update_session 抛异常但 conversation_log 成功 → 事务仍提交（保留 insert）。

    根因：两条写入独立，update 失败不应回滚已成功的 insert。原版靠捕获异常继续，
    现由 write_transaction 正常退出 commit（inner except 吞掉 update 异常）。
    """
    manager, db, _ = _make_manager()
    db.update_session = AsyncMock(side_effect=RuntimeError("db locked"))

    await manager._run_persistence_tasks(
        user_input="在吗", reply="在的", user_id="u1", source="qq",
        emotion={"primary": "calm"}, session_id="s1",
    )

    assert db.insert_conversation_log.await_count == 1
    assert db.update_session.await_count == 1
    # insert 成功 → 事务正常提交（committed=1），不回滚
    assert db._txn.entered == 1
    assert db._txn.committed == 1, "insert 成功时事务应提交（保留 insert 数据）"
    assert db._txn.rolled_back == 0


@pytest.mark.asyncio
async def test_both_writes_fail_transaction_commits_empty():
    """场景三：两个写入都失败 → 事务进入一次、提交空事务（无数据写入）。

    架构变更说明：原版 both-fail 时 rollback（清脏事务）。现 write_transaction 的
    asyncio.Lock 从源头杜绝并发脏事务，both-fail 时 commit 空事务（等价 no-op，
    无数据写入）更简单且正确。关键不变量：无 partial data 残留。
    """
    manager, db, _ = _make_manager()
    db.insert_conversation_log = AsyncMock(side_effect=RuntimeError("disk full"))
    db.update_session = AsyncMock(side_effect=RuntimeError("disk full"))

    await manager._run_persistence_tasks(
        user_input="hi", reply="hello", user_id="u1", source="qq",
        emotion={"primary": "neutral"}, session_id="s1",
    )

    # 两条写入均被尝试
    assert db.insert_conversation_log.await_count == 1
    assert db.update_session.await_count == 1
    # 事务进入一次；异常被 inner except 吞掉，正常退出 → 空提交（无数据）
    assert db._txn.entered == 1
    assert db._txn.committed == 1, "both-fail 时空 commit（无数据，等价 rollback）"
    assert db._txn.rolled_back == 0, "inner except 吞掉异常，未传播到 write_transaction"


@pytest.mark.asyncio
async def test_empty_session_id_falls_back_to_user_id():
    """场景四：session_id 为空 → 兜底为 user_id，insert/update 均使用兜底值。

    2026-08-05 治本修复：微信 bot 不传 session_id（session_id=""），写入
    conversation_logs 后 WebUI 会话列表 WHERE session_id != '' 过滤掉 → 微信
    聊天记录不显示。修复后空 session_id 用 user_id 作为会话标识。
    原"空 session_id 跳过 update_session"语义已废弃。
    """
    manager, db, _ = _make_manager()

    await manager._run_persistence_tasks(
        user_input="hi", reply="hello", user_id="u1", source="cli",
        emotion={"primary": "neutral"}, session_id="",
    )

    db.insert_conversation_log.assert_awaited_once()
    _, kwargs = db.insert_conversation_log.call_args
    assert kwargs.get("session_id") == "u1", \
        f"空 session_id 应兜底为 user_id，实际为 {kwargs.get('session_id')!r}"

    # 兜底后 session_id 非空 → update_session 正常调用（单事务内）
    db.update_session.assert_awaited_once()
    _, kwargs = db.update_session.call_args
    assert kwargs.get("auto_commit") is False, "update 应传 auto_commit=False"
    assert db._txn.entered == 1
    assert db._txn.committed == 1, "insert 成功应触发一次事务提交"


@pytest.mark.asyncio
async def test_memory_encode_not_affected_by_batch_transaction():
    """场景五：history 足够长时 try_idle_encode 仍被调用，且独立于批量事务。

    验证 try_idle_encode 不纳入批量提交（fire-and-forget，不影响 write_transaction 次数）。
    CodeRabbit F3: 同步等待 spawn 的 fire-and-forget 任务，断言 memory 编码被实际调用，
    而非仅靠事务计数间接推断。
    """
    from core.background_tasks import _bg_tasks
    manager, db, memory = _make_manager(memory_enabled=True)
    manager.context.history = ["x"] * 5
    manager.context.get_last_n = MagicMock(return_value=[("q", "a")] * 3)
    # _encode_task 内部 await flush_pre_compressed_buffer()，需为 AsyncMock 否则 await 抛 TypeError
    manager.context.flush_pre_compressed_buffer = AsyncMock(return_value=[])

    _before = set(_bg_tasks)
    await manager._run_persistence_tasks(
        user_input="记住这些", reply="好的", user_id="u1", source="qq",
        emotion={"primary": "happy"}, session_id="s1",
    )

    # 同步等待 fire-and-forget 记忆编码任务完成，确保 try_idle_encode 已被调用
    _new_tasks = _bg_tasks - _before
    if _new_tasks:
        await asyncio.gather(*_new_tasks, return_exceptions=True)

    # 持久化事务仍只进入一次（记忆编码独立 fire-and-forget）
    assert db._txn.entered == 1, "持久化事务应仅一次，不受记忆编码影响"
    assert db._txn.committed == 1
    memory.try_idle_encode.assert_awaited_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
