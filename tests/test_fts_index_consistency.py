"""FTS 索引一致性测试 — 验证 update_memory_summary / update_fallback_raw /
update_memory_enrichment 在 FTS 更新失败时不会丢失旧的 FTS 索引条目。

根因：原代码使用 DELETE + INSERT 两步更新 FTS，若 INSERT 失败，
旧的 FTS 条目已被 DELETE 删除，导致记忆对全文搜索不可见。
修复：改用 INSERT OR REPLACE，保证原子性——失败时旧条目仍在。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.db_memory import MemoryDB
from db.database import DatabaseManager


@pytest.fixture
async def memory_db(tmp_path):
    """构造一个临时数据库 + MemoryDB 实例（跑完迁移）。"""
    db_path = tmp_path / "test_fts.db"
    db_manager = DatabaseManager(db_path)
    await db_manager.init()

    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    memory_db = MemoryDB(conn)

    yield memory_db, db_path

    await conn.close()


async def _fts_entry_exists(db_path, mem_id: int) -> bool:
    """检查 FTS 表中是否存在指定 id 的条目。"""
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT count(*) FROM episodic_memory_fts WHERE id = ?", (mem_id,)
        )
        row = await cursor.fetchone()
        return row[0] > 0


@pytest.mark.asyncio
async def test_update_summary_preserves_fts_on_failure(memory_db):
    """update_memory_summary 时若 FTS 更新失败，旧 FTS 条目仍应存在。

    场景：先插入记忆（FTS 写入成功），再 update_summary 时让 FTS 写入失败。
    期望：旧 FTS 条目仍存在（INSERT OR REPLACE 失败时不影响旧条目）。
    """
    mem_db, db_path = memory_db

    # 1. 插入记忆（FTS 正常写入）
    mem_id = await mem_db.insert_episodic_memory(
        summary="原始摘要内容用于全文搜索",
        importance=0.5,
    )
    assert await _fts_entry_exists(db_path, mem_id), "插入后 FTS 应有条目"

    # 2. 模拟 FTS 更新失败（_tokenize_for_fts 抛异常）
    with patch("db.fts_utils._tokenize_for_fts", side_effect=RuntimeError("FTS boom")):
        await mem_db.update_memory_summary(mem_id, "更新后的摘要")

    # 3. FTS 条目仍应存在（INSERT OR REPLACE 失败时旧条目保留）
    assert await _fts_entry_exists(db_path, mem_id), (
        "FTS 更新失败时旧索引条目不应丢失（INSERT OR REPLACE 语义）"
    )

    # 4. 主表 summary 已更新
    mem = await mem_db.get_memory_by_id(mem_id)
    assert mem["summary"] == "更新后的摘要"


@pytest.mark.asyncio
async def test_update_fallback_raw_preserves_fts_on_failure(memory_db):
    """update_fallback_raw 时若 FTS 更新失败，旧 FTS 条目仍应存在。"""
    mem_db, db_path = memory_db

    mem_id = await mem_db.insert_episodic_memory(
        summary="fallback 原始摘要",
        importance=0.5,
    )
    assert await _fts_entry_exists(db_path, mem_id)

    with patch("db.fts_utils._tokenize_for_fts", side_effect=RuntimeError("FTS boom")):
        await mem_db.update_fallback_raw(mem_id, "新摘要", "happy", "done")

    assert await _fts_entry_exists(db_path, mem_id), (
        "update_fallback_raw FTS 更新失败时旧索引条目不应丢失"
    )

    mem = await mem_db.get_memory_by_id(mem_id)
    assert mem["summary"] == "新摘要"


@pytest.mark.asyncio
async def test_update_enrichment_preserves_fts_on_failure(memory_db):
    """update_memory_enrichment 时若 FTS 更新失败，旧 FTS 条目仍应存在。"""
    mem_db, db_path = memory_db

    mem_id = await mem_db.insert_episodic_memory(
        summary="enrichment 原始摘要",
        importance=0.5,
    )
    assert await _fts_entry_exists(db_path, mem_id)

    with patch("db.fts_utils._tokenize_for_fts", side_effect=RuntimeError("FTS boom")):
        result = await mem_db.update_memory_enrichment(
            mem_id, summary="enrichment 新摘要"
        )

    # update_memory_enrichment 返回 True（主表更新成功）
    assert result is True

    # FTS 条目仍应存在
    assert await _fts_entry_exists(db_path, mem_id), (
        "update_memory_enrichment FTS 更新失败时旧索引条目不应丢失"
    )


@pytest.mark.asyncio
async def test_update_summary_fts_searchable_after_success(memory_db):
    """正常路径：update_summary 后新内容可通过 FTS 搜索到。"""
    mem_db, _db_path = memory_db

    mem_id = await mem_db.insert_episodic_memory(
        summary="旧摘要苹果",
        importance=0.5,
    )

    # 更新为全新内容
    await mem_db.update_memory_summary(mem_id, "新摘要香蕉橙子")

    # FTS 搜索应能找到新内容
    results = await mem_db.search_memories_fts("香蕉橙子")
    found_ids = [r["id"] for r in results]
    assert mem_id in found_ids, "更新后 FTS 应能搜到新内容"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
