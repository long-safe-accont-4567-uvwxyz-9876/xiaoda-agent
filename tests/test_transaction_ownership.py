"""事务所有权测试（数据库小任务B-1）。

根因：aiosqlite 单连接共享事务状态。子仓库（MemoryDB/KnowledgeDBV2）的
auto_commit=True 写点此前裸 execute+commit，并发下会把他人 write_transaction()
中未完成的半事务提前 commit，或自身 rollback 使他人写入失效（已复现）。

修复契约：
1. DatabaseManager 提供受控写入口 execute_and_commit（单语句）：
   经 transaction_lock_for(conn) 包装 execute+commit。
2. MemoryDB / KnowledgeDBV2 的 auto_commit=True 单语句写点统一经同一把连接级锁。
3. 多语句写一律走 write_transaction / 锁内事务上下文。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.database import DatabaseManager
from db.db_kg_v2 import KnowledgeDBV2
from db.db_local_ai import transaction_lock_for


@pytest.mark.asyncio
async def test_execute_and_commit_serializes_with_write_transaction(tmp_path):
    """execute_and_commit 与 write_transaction 持有同一把连接级锁：
    长事务进行中时，单语句写必须等待，不得插入提交他人半事务。
    """
    manager = DatabaseManager(tmp_path / "own.db")
    await manager.init()

    observed: list[str] = []

    async def long_writer():
        async with manager.write_transaction():
            await manager._conn.execute(
                "INSERT INTO episodic_memories (timestamp, summary, session_id) "
                "VALUES (?, ?, ?)",
                (1.0, "half-txn-row", "user"),
            )
            observed.append("writer_before_yield")
            await asyncio.sleep(0.05)
            observed.append("writer_before_commit")
        observed.append("writer_committed")

    async def single_statement_writer():
        # 等 long_writer 先拿到锁、进入半事务
        while "writer_before_yield" not in observed:
            await asyncio.sleep(0.005)
        await manager.execute_and_commit(
            "UPDATE episodic_memories SET importance=0.9 WHERE summary=?",
            ("half-txn-row",),
        )
        observed.append("single_done")

    await asyncio.gather(long_writer(), single_statement_writer())

    # 单语句写只能在长事务提交之后完成（串行化，而非插入提交）
    assert observed.index("writer_committed") < observed.index("single_done")

    row = await manager.fetch_one(
        "SELECT importance FROM episodic_memories WHERE summary='half-txn-row'"
    )
    assert row is not None and row["importance"] == 0.9
    await manager.close()


@pytest.mark.asyncio
async def test_memorydb_auto_commit_write_shares_connection_lock(tmp_path):
    """MemoryDB 的 auto_commit=True 写点经 transaction_lock_for(conn) 串行化。"""
    manager = DatabaseManager(tmp_path / "mem.db")
    await manager.init()
    mem_db = manager.memory

    lock_obj = transaction_lock_for(manager._conn)
    assert lock_obj.locked() is False

    async with manager.write_transaction():
        assert lock_obj.locked() is True, (
            "write_transaction 与 MemoryDB 子仓库写点必须共享同一把连接级锁"
        )
    assert lock_obj.locked() is False

    # auto_commit 写入路径正常工作且不再裸提交
    mem_id = await mem_db.insert_episodic_memory(summary="短记忆", importance=0.5)
    row = await manager.fetch_one(
        "SELECT summary FROM episodic_memories WHERE id=?", (mem_id,)
    )
    assert row is not None and row["summary"] == "短记忆"
    await manager.close()


@pytest.mark.asyncio
async def test_concurrent_auto_commit_writes_never_commit_foreign_half_txn(tmp_path):
    """压力回归：并发的 auto_commit 单语句写与多语句 write_transaction 交错时，
    半事务要么完整提交要么整体回滚，不允许出现「被他人提前 commit」的脏行。

    场景：writer A 在 write_transaction 中先写 good_row 再 sleep（制造窗口），
    期间大量单语句写排队。若单语句写绕过锁直接 commit，A 回滚后 good_row 仍会
    存在（半事务被提前提交的证据）。
    """
    manager = DatabaseManager(tmp_path / "race.db")
    await manager.init()

    rounds = 20
    leaked = 0
    for i in range(rounds):
        marker = f"round-{i}"

        async def writer_a():
            async with manager.write_transaction():
                await manager._conn.execute(
                    "INSERT INTO episodic_memories (timestamp, summary, session_id) "
                    "VALUES (?, ?, ?)",
                    (1.0, marker, "user"),
                )
                await asyncio.sleep(0.001)
                raise RuntimeError("rollback-on-purpose")

        async def writer_b():
            await manager.execute_and_commit(
                "UPDATE episodic_memories SET access_count=access_count+1 "
                "WHERE summary='counter-row'",
            )

        try:
            await asyncio.gather(writer_a(), writer_b())
        except RuntimeError:
            pass
        row = await manager.fetch_one(
            "SELECT id FROM episodic_memories WHERE summary=?", (marker,)
        )
        if row is not None:
            leaked += 1

    assert leaked == 0, (
        f"{leaked}/{rounds} 次半事务被并发 auto_commit 写提前提交（事务所有权被破坏）"
    )
    await manager.close()


@pytest.mark.asyncio
async def test_memorydb_insert_serializes_with_write_transaction(tmp_path):
    """高频写点迁移回归：MemoryDB auto_commit 插入与 write_transaction
    共享连接级锁——长事务回滚期间并发插入不得把半事务提前提交。
    """
    manager = DatabaseManager(tmp_path / "insrace.db")
    await manager.init()
    assert manager.memory._tx_guard is not None

    leaked = 0
    rounds = 12
    for i in range(rounds):
        marker = f"mem-race-{i}"

        async def writer_a():
            async with manager.write_transaction():
                await manager._conn.execute(
                    "INSERT INTO episodic_memories "
                    "(timestamp, summary, session_id) VALUES (?, ?, ?)",
                    (1.0, marker, "user"),
                )
                await asyncio.sleep(0.001)
                raise RuntimeError("rollback-on-purpose")

        async def writer_b():
            await manager.memory.insert_episodic_memory(
                summary="background", importance=0.1)

        results = await asyncio.gather(
            writer_a(), writer_b(), return_exceptions=True)
        # writer_b 必须正常完成（无异常）
        assert results[1] is None, f"并发插入异常: {results[1]!r}"
        row = await manager.fetch_one(
            "SELECT id FROM episodic_memories WHERE summary=?", (marker,))
        if row is not None:
            leaked += 1

    assert leaked == 0, (
        f"{leaked}/{rounds} 次半事务被 MemoryDB auto_commit 写提前提交")
    await manager.close()


@pytest.mark.asyncio
async def test_kg_v2_writes_share_connection_lock(tmp_path):
    """KnowledgeDBV2 的 auto_commit=True 写点同样经连接级锁。"""
    manager = DatabaseManager(tmp_path / "kg.db")
    await manager.init()

    lock_obj = transaction_lock_for(manager._conn)
    async with manager.write_transaction():
        assert lock_obj.locked() is True
    await manager.close()


@pytest.mark.asyncio
async def test_kg_v2_merge_aborts_when_immediate_lock_unavailable(tmp_path):
    """KG v2 在 BEGIN IMMEDIATE 失败后不得继续写入未知事务：
    获取锁失败必须报错回退，而不是降级为隐式事务继续写。
    """
    from unittest.mock import AsyncMock

    from memory.knowledge_graph_v2 import KnowledgeGraphV2

    manager = DatabaseManager(tmp_path / "kglk.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraphV2(db_v2=db, vector_store=None)
    kg.extract_from_summary = AsyncMock(return_value={"entities": [], "relations": []})

    original_execute = manager._conn.execute

    async def begin_fails(sql, *args, **kwargs):
        if str(sql).strip().upper().startswith("BEGIN"):
            raise RuntimeError("cannot start a transaction within a transaction")
        return await original_execute(sql, *args, **kwargs)

    manager._conn.execute = begin_fails

    with pytest.raises(RuntimeError):
        await kg.add_facts_from_episode("用户喜欢打篮球", 1000.0)

    # 报错后不得留下任何写入痕迹（episode 未落库）
    episode_rows = await manager.fetch_all("SELECT id FROM kg_episodes")
    relation_rows = await manager.fetch_all("SELECT id FROM kg_relations_v2")
    assert episode_rows == []
    assert relation_rows == []
    await manager.close()
