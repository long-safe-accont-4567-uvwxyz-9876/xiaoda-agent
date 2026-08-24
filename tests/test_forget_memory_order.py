"""忘记记忆删除顺序测试（数据库小任务B-2）。

根因：db_memory_lifecycle 删除路径先删外部向量再删主表，且主表 DELETE 时
未先处理 memory_versions（其 FK 无 ON DELETE 策略）。当 memory_versions 存在
引用时主表 DELETE 抛 IntegrityError，但向量已被删除且不可恢复。

修复契约：
1. 主库事务内先删引用（memory_versions / context_audit_log / entity_memory_links /
   memory_child_chunks / episodic_memory_fts）再删 episodic_memories 主行。
2. commit 成功之后才经 vector_store 删除向量；删除失败不抛出、可由对账重试
   （幂等 delete），绝不出现「向量已删而主记录仍在」的不可恢复状态。
3. tools/memory_tool forget 走新顺序。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.database import DatabaseManager


class FakeVectorStore:
    """记录调用顺序的假向量库；delete 可配置失败。"""

    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[tuple[str, int]] = []
        self._fail_times = fail_times

    async def delete(self, memory_id: int) -> None:
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("vector delete failed")
        self.calls.append(("delete", int(memory_id)))

    async def upsert(self, memory_id: int, text: str) -> None:
        self.calls.append(("upsert", int(memory_id)))


@pytest.mark.asyncio
async def test_batch_delete_db_first_vector_after_commit(tmp_path):
    """批量删除顺序契约：主库 commit 之后才删向量；向量失败不抛出且可重试。"""
    manager = DatabaseManager(tmp_path / "batchorder.db")
    await manager.init()

    observations: list[bool] = []

    class OrderVec(FakeVectorStore):
        async def delete(self, memory_id: int) -> None:
            if self._fail_times > 0:
                self._fail_times -= 1
                raise RuntimeError("vector delete failed")
            row = await manager.fetch_one(
                "SELECT id FROM episodic_memories WHERE id=?", (int(memory_id),)
            )
            observations.append(row is None)
            self.calls.append(("delete", int(memory_id)))

    ids = []
    for i in range(2):
        mem_id = await manager.memory.insert_episodic_memory(
            summary=f"batch-{i}", importance=0.5)
        ids.append(mem_id)
        await manager._conn.execute(
            "INSERT INTO memory_versions "
            "(memory_id, version, content_hash, prev_hash, summary_snapshot, created_at) "
            "VALUES (?, 1, 'h', '', 's', 1.0)",
            (mem_id,),
        )
    await manager._conn.commit()

    vec = OrderVec(fail_times=1)  # 第一个向量删除失败
    # 不抛出：主库删除已完成
    await manager.memory.delete_memories_batch(ids, vector_store=vec)

    for mem_id in ids:
        row = await manager.fetch_one(
            "SELECT id FROM episodic_memories WHERE id=?", (mem_id,))
        versions = await manager.fetch_all(
            "SELECT id FROM memory_versions WHERE memory_id=?", (mem_id,))
        assert row is None and versions == []
    # 向量删除全部发生在 commit 之后（此时主表行已不可见）；
    # 失败的第一次调用不产生 observation、不记入成功
    assert observations == [True], (
        f"向量删除必须在主库 commit 之后执行且失败不落账: {observations}")
    successful_ids = {m for _, m in vec.calls}
    assert len(successful_ids) == 1

    # 对账重试：补删失败的那个
    await manager.memory.delete_memories_batch([ids[0]], vector_store=OrderVec())
    await manager.close()


async def _insert_memory_with_versions(manager: DatabaseManager) -> int:
    mem_id = await manager.memory.insert_episodic_memory(
        summary="用户喜欢喝美式咖啡", importance=0.8, is_raw=0,
    )
    await manager._conn.execute(
        "INSERT INTO memory_versions "
        "(memory_id, version, content_hash, prev_hash, summary_snapshot, created_at) "
        "VALUES (?, 1, 'hash-a', '', 'snapshot', 1.0)",
        (mem_id,),
    )
    await manager._conn.commit()
    return mem_id


@pytest.mark.asyncio
async def test_delete_memory_with_vector_succeeds_with_memory_versions(tmp_path):
    """memory_versions 存在外键引用时删除成功：先删引用再删主表。"""
    manager = DatabaseManager(tmp_path / "forget.db")
    await manager.init()
    vec = FakeVectorStore()
    mem_id = await _insert_memory_with_versions(manager)

    versions_before = await manager.fetch_all(
        "SELECT id FROM memory_versions WHERE memory_id=?", (mem_id,)
    )
    assert versions_before, "前置条件：memory_versions 必须存在"

    await manager.memory.delete_memory_with_vector(mem_id, vector_store=vec)

    main_row = await manager.fetch_one(
        "SELECT id FROM episodic_memories WHERE id=?", (mem_id,)
    )
    versions_after = await manager.fetch_all(
        "SELECT id FROM memory_versions WHERE memory_id=?", (mem_id,)
    )
    assert main_row is None, "主表行应被删除"
    assert versions_after == [], "memory_versions 引用必须先于主表删除"
    assert ("delete", mem_id) in vec.calls, "commit 后才删向量"
    await manager.close()


@pytest.mark.asyncio
async def test_delete_order_db_first_vector_after_commit(tmp_path):
    """顺序契约：向量删除发生在主库 commit 之后。

    让向量 delete 记录时检查主表行是否已经消失——若向量删除发生在 commit 前，
    主表行仍可见（同一连接内未提交事务对自己可见），测试即失败。
    """
    manager = DatabaseManager(tmp_path / "order.db")
    await manager.init()

    observations: list[bool] = []

    class OrderCheckVec(FakeVectorStore):
        async def delete(self, memory_id: int) -> None:
            row = await manager.fetch_one(
                "SELECT id FROM episodic_memories WHERE id=?", (int(memory_id),)
            )
            observations.append(row is None)
            self.calls.append(("delete", int(memory_id)))

    mem_id = await _insert_memory_with_versions(manager)
    await manager.memory.delete_memory_with_vector(mem_id, vector_store=OrderCheckVec())

    assert observations == [True], (
        "向量删除必须在主库 commit 成功之后执行（此时主表行应已不可见）"
    )
    await manager.close()


@pytest.mark.asyncio
async def test_delete_survives_vector_failure_and_is_retryable(tmp_path):
    """向量删除失败不影响主库删除结果，且可重试补删（幂等）。"""
    manager = DatabaseManager(tmp_path / "vecfail.db")
    await manager.init()
    vec = FakeVectorStore(fail_times=1)
    mem_id = await _insert_memory_with_versions(manager)

    # 第一次：向量删除失败，但不抛出，主表仍完成删除
    await manager.memory.delete_memory_with_vector(mem_id, vector_store=vec)

    main_row = await manager.fetch_one(
        "SELECT id FROM episodic_memories WHERE id=?", (mem_id,)
    )
    assert main_row is None, "向量删除失败不得阻断主库删除"
    assert ("delete", mem_id) not in vec.calls, "失败的删除不应记为成功"

    # 第二次（对账重试）：向量恢复后补删成功
    await manager.memory.delete_memory_with_vector(mem_id, vector_store=vec)
    assert ("delete", mem_id) in vec.calls
    await manager.close()


@pytest.mark.asyncio
async def test_hard_delete_raw_for_user_request_deletes_references_first(tmp_path):
    """硬删原始记录同样先清引用再删主表，commit 后删向量。"""
    manager = DatabaseManager(tmp_path / "rawdel.db")
    await manager.init()
    vec = FakeVectorStore()
    mem_id = await manager.memory.insert_episodic_memory(
        summary="原始记录内容", importance=0.5, is_raw=1,
    )
    await manager._conn.execute(
        "INSERT INTO memory_versions "
        "(memory_id, version, content_hash, prev_hash, summary_snapshot, created_at) "
        "VALUES (?, 1, 'hash-r', '', 'snap', 1.0)",
        (mem_id,),
    )
    await manager._conn.commit()

    deleted = await manager.memory.hard_delete_raw_for_user_request(
        mem_id, vector_store=vec,
    )
    assert deleted is True
    row = await manager.fetch_one(
        "SELECT id FROM episodic_memories WHERE id=?", (mem_id,)
    )
    versions = await manager.fetch_all(
        "SELECT id FROM memory_versions WHERE memory_id=?", (mem_id,)
    )
    assert row is None and versions == []
    assert ("delete", mem_id) in vec.calls
    await manager.close()
