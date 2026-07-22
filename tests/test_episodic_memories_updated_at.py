"""测试 episodic_memories.updated_at 字段与触发器（剩余事项修复）。

验证：
1. v19 迁移正确添加 updated_at 列
2. 触发器在 summary 被 UPDATE 时自动维护 updated_at
3. 其他列变更（emotion_label、access_count）不触发 updated_at 更新
4. created_at 不受触发器影响
5. 回填：已有记录的 updated_at 初始化为 timestamp
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import aiosqlite
import pytest

# 确保项目根目录在 sys.path
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.database import DatabaseManager
from db.db_memory import MemoryDB


@pytest.fixture
async def memory_db(tmp_path):
    """构造一个临时内存数据库 + MemoryDB 实例，自动跑迁移到 v19。

    使用临时文件路径的 SQLite 数据库（不用 :memory: 因为迁移可能依赖文件持久化），
    完成后自动清理。
    """
    db_path = tmp_path / "test_memory.db"
    # 直接用 DatabaseManager 跑完整建表 + 迁移流程
    db_manager = DatabaseManager(db_path)
    await db_manager.init()

    # 验证 v19 迁移已应用
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT MAX(version) FROM schema_version")
        row = await cursor.fetchone()
        assert row[0] >= 19, f"v19 迁移未应用，当前版本: {row[0] if row else None}"

    # 用同一连接返回 MemoryDB 实例
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    memory_db = MemoryDB(conn)

    yield memory_db, db_path

    await conn.close()


@pytest.mark.asyncio
async def test_v19_migration_adds_updated_at_column(memory_db):
    """v19 迁移后 episodic_memories 表应包含 updated_at 列。"""
    mem_db, db_path = memory_db
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("PRAGMA table_info(episodic_memories)")
        cols = {row["name"] for row in await cursor.fetchall()}
        assert "updated_at" in cols, f"updated_at 列不存在: {cols}"


@pytest.mark.asyncio
async def test_v19_migration_creates_trigger(memory_db):
    """v19 迁移后应创建 trg_episodic_memories_touch_updated_at 触发器。"""
    mem_db, db_path = memory_db
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_episodic_memories_touch_updated_at'"
        )
        row = await cursor.fetchone()
        assert row is not None, "触发器 trg_episodic_memories_touch_updated_at 不存在"


@pytest.mark.asyncio
async def test_insert_initial_updated_at_zero(memory_db):
    """插入新记忆时 updated_at 默认 0（未变更过 summary）。"""
    mem_db, _ = memory_db
    mem_id = await mem_db.insert_episodic_memory(
        summary="初始摘要内容",
        importance=0.5,
    )
    mem = await mem_db.get_memory_by_id(mem_id)
    assert mem is not None
    assert mem["updated_at"] == 0, f"新插入记忆 updated_at 应为 0，实际: {mem['updated_at']}"


@pytest.mark.asyncio
async def test_update_summary_triggers_updated_at_change(memory_db):
    """UPDATE summary 时触发器应自动更新 updated_at 为当前时间戳。"""
    mem_db, _ = memory_db
    mem_id = await mem_db.insert_episodic_memory(
        summary="原始摘要",
        importance=0.5,
    )

    # 记录原始 updated_at
    before = await mem_db.get_memory_by_id(mem_id)
    assert before["updated_at"] == 0

    # 等待 1.1 秒确保 strftime('%s','now') 有变化（秒级精度）
    await asyncio.sleep(1.1)

    # 更新 summary
    await mem_db.update_memory_summary(mem_id, "这是更新后的摘要内容")

    after = await mem_db.get_memory_by_id(mem_id)
    assert after["updated_at"] > 0, f"updated_at 未被触发器更新: {after['updated_at']}"
    # updated_at 应接近当前时间戳
    now = time.time()
    assert abs(now - after["updated_at"]) < 5, (
        f"updated_at 与当前时间偏差过大: updated_at={after['updated_at']}, now={now}"
    )


@pytest.mark.asyncio
async def test_update_emotion_label_does_not_trigger_updated_at(memory_db):
    """UPDATE 非 summary 列（如 emotion_label）不应触发 updated_at 更新。"""
    mem_db, _ = memory_db
    mem_id = await mem_db.insert_episodic_memory(
        summary="测试摘要",
        importance=0.5,
    )

    # 原始 updated_at 应为 0
    before = await mem_db.get_memory_by_id(mem_id)
    assert before["updated_at"] == 0

    # 等待确保时间戳变化
    await asyncio.sleep(1.1)

    # 仅更新 emotion_label（不是 summary）
    await mem_db.update_emotion_label(mem_id, "happy")

    after = await mem_db.get_memory_by_id(mem_id)
    assert after["updated_at"] == 0, (
        f"更新 emotion_label 不应触发 updated_at 变更，但实际: {after['updated_at']}"
    )
    assert after["emotion_label"] == "happy"


@pytest.mark.asyncio
async def test_increment_access_count_does_not_trigger_updated_at(memory_db):
    """UPDATE access_count 不应触发 updated_at 更新。"""
    mem_db, _ = memory_db
    mem_id = await mem_db.insert_episodic_memory(
        summary="测试摘要",
        importance=0.5,
    )

    # 先用 update_memory_summary 让 updated_at 有值，再测 access_count 是否会污染
    await asyncio.sleep(1.1)
    await mem_db.update_memory_summary(mem_id, "更新后的摘要")
    after_summary = await mem_db.get_memory_by_id(mem_id)
    updated_at_after_summary = after_summary["updated_at"]
    assert updated_at_after_summary > 0

    # 等待时间戳变化
    await asyncio.sleep(1.1)

    # 递增 access_count
    await mem_db.increment_access_count(mem_id)

    after_access = await mem_db.get_memory_by_id(mem_id)
    assert after_access["updated_at"] == updated_at_after_summary, (
        f"递增 access_count 不应触发 updated_at 变更: "
        f"before={updated_at_after_summary}, after={after_access['updated_at']}"
    )
    assert after_access["access_count"] == 1


@pytest.mark.asyncio
async def test_backfill_existing_records_updated_at(memory_db):
    """v19 回填：已有记录的 updated_at 应初始化为 timestamp。"""
    mem_db, db_path = memory_db

    # 直接插入一条记录（updated_at 默认 0），然后用 SQL 模拟旧数据回填
    mem_id = await mem_db.insert_episodic_memory(
        summary="回填测试",
        importance=0.5,
    )

    # 手动把 updated_at 重置为 0（模拟旧数据）
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("UPDATE episodic_memories SET updated_at=0 WHERE id=?", (mem_id,))
        await conn.commit()

    # 触发回填逻辑（与 v19 migrate 中的 SQL 一致）
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "UPDATE episodic_memories SET updated_at = timestamp "
            "WHERE updated_at = 0 AND timestamp > 0"
        )
        await conn.commit()

    result = await mem_db.get_memory_by_id(mem_id)
    assert result["updated_at"] == result["timestamp"], (
        f"回填失败: updated_at={result['updated_at']}, timestamp={result['timestamp']}"
    )


@pytest.mark.asyncio
async def test_update_fallback_raw_triggers_updated_at(memory_db):
    """update_fallback_raw 同时更新 summary 时也应触发 updated_at。"""
    mem_db, _ = memory_db
    mem_id = await mem_db.insert_episodic_memory(
        summary="原始摘要",
        importance=0.5,
    )

    await asyncio.sleep(1.1)

    # update_fallback_raw 会 UPDATE summary + emotion_label + distill_status
    await mem_db.update_fallback_raw(
        mem_id,
        new_summary="fallback 更新后的摘要",
        label="neutral",
        distill_status="completed",
    )

    result = await mem_db.get_memory_by_id(mem_id)
    assert result["updated_at"] > 0, (
        f"update_fallback_raw 未触发 updated_at 更新: {result['updated_at']}"
    )
    assert result["summary"] == "fallback 更新后的摘要"
    assert result["emotion_label"] == "neutral"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
