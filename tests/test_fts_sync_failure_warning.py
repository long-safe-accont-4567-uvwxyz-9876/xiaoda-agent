"""FTS 索引同步失败必须可被观察：告警日志 + 失败计数，而非静默 debug。

覆盖 db_memory.MemoryDB 中三处 FTS 写入同步失败场景：
- insert_episodic_memory: 主表插入成功后 FTS 索引写入失败
- update_memory_summary: 主表 summary 更新成功后 FTS 同步失败
- update_fallback_raw: 主表 summary/emotion 更新成功后 FTS 同步失败

核心断言：主表写入成功语义不变（不抛异常、返回 mem_id / 数据落库），
同时 FTS 失败被提升为 WARNING 日志，且模块级计数器 _fts_sync_failures 递增。
"""
from __future__ import annotations

import sys
from pathlib import Path

import aiosqlite
import pytest
from loguru import logger

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import db.db_memory as dbm
from db.db_memory import MemoryDB


@pytest.fixture
async def memory_db():
    """构造最小内存 SQLite（episodic_memories + FTS 表）+ MemoryDB。"""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript("""
        CREATE TABLE episodic_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            summary TEXT NOT NULL,
            importance REAL DEFAULT 0.5,
            emotion_label TEXT DEFAULT '',
            session_id TEXT DEFAULT 'user',
            embedding_id INTEGER DEFAULT -1,
            source TEXT DEFAULT 'user',
            user_id TEXT DEFAULT 'default',
            agent_id TEXT DEFAULT 'xiaoda',
            is_raw INTEGER DEFAULT 0,
            distill_status TEXT DEFAULT ''
        );
        CREATE VIRTUAL TABLE episodic_memory_fts USING fts5(
            id UNINDEXED,
            summary_index
        );
    """)
    await conn.commit()
    db = MemoryDB(conn)
    yield db
    await conn.close()


@pytest.fixture
def warning_records():
    """捕获 WARNING 级别日志记录，测试结束后移除 sink。"""
    records = []
    sink_id = logger.add(lambda message: records.append(message.record), level="WARNING")
    yield records
    logger.remove(sink_id)


def _assert_warning(records, message):
    matches = [r for r in records if r["level"].name == "WARNING" and r["message"] == message]
    assert matches, (
        f"未捕获到 WARNING 日志 '{message}'，实际记录: "
        f"{[(r['level'].name, r['message']) for r in records]}"
    )


def _break_fts(monkeypatch):
    """让 _tokenize_for_fts 抛异常，模拟 FTS 同步失败。"""
    def boom(text: str) -> str:
        raise RuntimeError("fts broken")

    monkeypatch.setattr("db.fts_utils._tokenize_for_fts", boom)


@pytest.mark.asyncio
async def test_insert_episodic_memory_warns_when_fts_sync_fails(memory_db, warning_records, monkeypatch):
    monkeypatch.setattr(dbm, "_fts_sync_failures", 0)
    _break_fts(monkeypatch)

    mem_id = await memory_db.insert_episodic_memory(summary="测试摘要")

    # 主表写入成功语义不变
    assert isinstance(mem_id, int) and mem_id >= 1
    row = await memory_db.get_memory_by_id(mem_id)
    assert row is not None and row["summary"] == "测试摘要"
    # FTS 失败可被观察
    _assert_warning(warning_records, "db_memory.fts_insert_failed")
    assert dbm._fts_sync_failures == 1


@pytest.mark.asyncio
async def test_update_memory_summary_warns_when_fts_sync_fails(memory_db, warning_records, monkeypatch):
    mem_id = await memory_db.insert_episodic_memory(summary="原始摘要")
    monkeypatch.setattr(dbm, "_fts_sync_failures", 0)
    _break_fts(monkeypatch)

    await memory_db.update_memory_summary(mem_id, "更新后的摘要")

    # 主表写入成功语义不变
    row = await memory_db.get_memory_by_id(mem_id)
    assert row is not None and row["summary"] == "更新后的摘要"
    # FTS 失败可被观察
    _assert_warning(warning_records, "db_memory.fts_sync_on_summary_update_failed")
    assert dbm._fts_sync_failures == 1


@pytest.mark.asyncio
async def test_update_fallback_raw_warns_when_fts_sync_fails(memory_db, warning_records, monkeypatch):
    mem_id = await memory_db.insert_episodic_memory(summary="原始摘要")
    monkeypatch.setattr(dbm, "_fts_sync_failures", 0)
    _break_fts(monkeypatch)

    await memory_db.update_fallback_raw(
        mem_id,
        new_summary="fallback 更新后的摘要",
        label="neutral",
        distill_status="completed",
    )

    # 主表写入成功语义不变
    row = await memory_db.get_memory_by_id(mem_id)
    assert row is not None and row["summary"] == "fallback 更新后的摘要"
    assert row["emotion_label"] == "neutral"
    # FTS 失败可被观察
    _assert_warning(warning_records, "db_memory.fts_sync_on_fallback_failed")
    assert dbm._fts_sync_failures == 1


@pytest.mark.asyncio
async def test_fts_sync_failure_counter_getter(memory_db, warning_records, monkeypatch):
    """触发失败后累计计数应可经 getter 读取，且随失败次数递增。"""
    monkeypatch.setattr(dbm, "_fts_sync_failures", 0)
    _break_fts(monkeypatch)

    assert dbm.get_fts_sync_failures() == 0

    await memory_db.insert_episodic_memory(summary="第一次摘要")
    assert dbm.get_fts_sync_failures() == 1

    await memory_db.insert_episodic_memory(summary="第二次摘要")
    assert dbm.get_fts_sync_failures() == 2


@pytest.mark.asyncio
async def test_fts_sync_failure_warning_includes_total(memory_db, warning_records, monkeypatch):
    """warning 日志应携带累计计数（fts_sync_failures_total）。"""
    monkeypatch.setattr(dbm, "_fts_sync_failures", 0)
    _break_fts(monkeypatch)

    await memory_db.insert_episodic_memory(summary="带计数摘要")

    totals = [
        r["extra"].get("fts_sync_failures_total")
        for r in warning_records
        if r["level"].name == "WARNING" and r["message"] == "db_memory.fts_insert_failed"
    ]
    assert 1 in totals, f"warning 日志应包含累计计数，实际 extra: {totals}"
