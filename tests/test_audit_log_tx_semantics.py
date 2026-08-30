"""insert_audit_log 事务所有权回归测试（Task 8 Fix 1）。

根因：审计日志此前在共享主连接 _conn 上裸 execute+commit。任务 A 在
write_transaction 中已写行未提交时，任务 B 的 insert_audit_log 直接 commit
会把 A 的半事务提前提交；A 随后回滚时"已被提交的行"残留 → 半事务/脏数据。

修复契约（"共享事务"语义，与 MemoryDB 的 WriteTxGuard 同一模式）：
1. 外层 write_transaction 内（任务本地 ContextVar 感知）：只 execute 不
   commit，提交权归外层——嵌套审计写入随业务事务一起回滚（共享事务的
   必然结果，刻意选择：独立连接方案会因 WAL 写锁与外层事务互等而死锁）。
2. 独立审计写：transaction_lock_for(_conn) 同一把连接级锁内 execute+commit，
   与写事务串行化——不再旁路提交他人半事务，自身也不被业务回滚连带丢弃。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.database import DatabaseManager


async def _make_manager(tmp_path, name: str) -> DatabaseManager:
    manager = DatabaseManager(tmp_path / name)
    await manager.init()
    return manager


@pytest.mark.asyncio
async def test_nested_audit_log_rolls_back_with_business_transaction(tmp_path):
    """两个协作协程：B 的审计写入发生在 A 的未提交事务内 → 随 A 一起回滚。

    writer_a 在 write_transaction 体内经 create_task 派生 writer_b（子任务
    继承 A 的 contextvars 上下文，_write_tx_active=True），模拟"事务体内
    嵌套审计写点"（如 tool_executor 在事务中记录审计）。B 只 execute 不
    commit；A 抛错回滚后业务行与审计行都必须消失。
    """
    manager = await _make_manager(tmp_path, "audit_nested.db")
    audit_done = asyncio.Event()

    async def writer_b() -> None:
        # B 继承 A 的事务上下文：insert_audit_log 必须只 execute 不 commit
        await manager.insert_audit_log("tool_call", user_id="u", detail="nested-audit")
        audit_done.set()

    async def writer_a() -> None:
        async with manager.write_transaction():
            await manager._conn.execute(
                "INSERT INTO episodic_memories (timestamp, summary, session_id) "
                "VALUES (?, ?, ?)",
                (1.0, "half-txn-row", "user"),
            )
            b_task = asyncio.create_task(writer_b())
            await audit_done.wait()  # 挂起：等 B 的审计写入落入未提交事务
            await b_task
            raise RuntimeError("rollback-on-purpose")

    with pytest.raises(RuntimeError):
        await asyncio.gather(writer_a())

    biz = await manager.fetch_one(
        "SELECT id FROM episodic_memories WHERE summary='half-txn-row'")
    audit = await manager.fetch_one(
        "SELECT id FROM audit_logs WHERE detail='nested-audit'")
    assert biz is None, "业务半事务必须整体回滚"
    assert audit is None, "共享事务语义：嵌套审计写入应随业务事务一并回滚"
    await manager.close()


@pytest.mark.asyncio
async def test_standalone_audit_write_never_commits_foreign_half_transaction(tmp_path):
    """并发回归：A 持写事务写行 1 挂起期间，B（独立任务）insert_audit_log
    完成后 A 抛错回滚 → 行 1 不得存在（不得被 B 旁路提交）。

    修复前 B 裸 commit 会在 A 的挂起窗口把半事务提前提交，回滚后行 1 残留；
    修复后 B 的审计写经连接级锁串行化，等 A 的事务结束（回滚）才执行。
    """
    manager = await _make_manager(tmp_path, "audit_race.db")

    async def writer_a() -> None:
        async with manager.write_transaction():
            await manager._conn.execute(
                "INSERT INTO episodic_memories (timestamp, summary, session_id) "
                "VALUES (?, ?, ?)",
                (1.0, "half-txn-row", "user"),
            )
            await asyncio.sleep(0.05)  # 挂起窗口：裸 commit 会在此提前提交半事务
            raise RuntimeError("rollback-on-purpose")

    async def writer_b() -> None:
        await manager.insert_audit_log("tool_call", user_id="u", detail="standalone-audit")

    with pytest.raises(RuntimeError):
        await asyncio.gather(writer_a(), writer_b())

    biz = await manager.fetch_one(
        "SELECT id FROM episodic_memories WHERE summary='half-txn-row'")
    assert biz is None, "并发审计写不得把他人 write_transaction 半事务提前提交"

    # 独立审计写（B 不在事务上下文）经锁串行化后正常落库：业务回滚不连带丢弃审计
    audit = await manager.fetch_one(
        "SELECT id FROM audit_logs WHERE detail='standalone-audit'")
    assert audit is not None, "独立审计写应在 A 回滚后经连接级锁正常提交"
    await manager.close()


@pytest.mark.asyncio
async def test_standalone_audit_write_persists_immediately(tmp_path):
    """行为保护：绝大多数调用点（webui 路由 / tool_executor / bootstrap）
    在事务外调用 insert_audit_log，依赖"返回即已落库"——语义不得回归。
    """
    manager = await _make_manager(tmp_path, "audit_basic.db")

    await manager.insert_audit_log("webui.config.set", user_id="webui", detail="d1")

    row = await manager.fetch_one("SELECT event_type, user_id FROM audit_logs WHERE detail='d1'")
    assert row is not None, "事务外的审计写入应立即提交可见"
    assert row["event_type"] == "webui.config.set"
    assert row["user_id"] == "webui"
    await manager.close()
