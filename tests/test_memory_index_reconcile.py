"""记忆编码 fire-and-forget 索引任务「静默丢失 + 无对账」修复的单元测试。

覆盖两部分：
(A) `_indexing_task` 中向量索引/概念图/子 chunk 的普通失败日志级别应从 debug 提升为 warning
    （超时保持 error 不变）。
(B) 向量索引对账检测：主表 is_raw=1 已落盘但 memories_vec 缺失的记录数应能被统计并触发 warning。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import aiosqlite
import pytest
from loguru import logger

from db.db_memory import MemoryDB, compute_missing_vec_ids


def _capture_warning_sink() -> tuple[list[str], int]:
    """挂一个 loguru sink 收集 WARNING 日志文本，返回 (列表, handler_id)。"""
    seen: list[str] = []

    def _sink(message) -> None:
        seen.append(str(message))

    handler_id = logger.add(_sink, level="WARNING", format="{message}")
    return seen, handler_id


# ── (B) 纯函数 seam ─────────────────────────────────────────

class TestComputeMissingVecIds:
    def test_returns_ids_missing_from_vec(self):
        assert compute_missing_vec_ids([1, 2, 3], {1, 2}) == [3]

    def test_empty_memory_ids_returns_empty(self):
        assert compute_missing_vec_ids([], {1}) == []

    def test_all_present_returns_empty(self):
        assert compute_missing_vec_ids([1, 2], {1, 2}) == []

    def test_preserves_memory_id_order(self):
        assert compute_missing_vec_ids([3, 1, 2], {2}) == [3, 1]


# ── (B) MemoryManager 对账方法 ───────────────────────────────

def _make_minimal_manager(memdb, vec) -> "object":
    """绕过 __init__ 的轻量 MemoryManager，仅设置对账所需依赖。"""
    from memory.memory_manager import MemoryManager

    mm = MemoryManager.__new__(MemoryManager)
    mm.memory = memdb
    mm.vec = vec
    return mm


async def _make_memory_db() -> MemoryDB:
    """构造最小内存库：只有 episodic_memories 表。"""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("""
        CREATE TABLE episodic_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            summary TEXT NOT NULL,
            importance REAL DEFAULT 0.5,
            emotion_label TEXT DEFAULT '',
            session_id TEXT DEFAULT 'user',
            embedding_id INTEGER DEFAULT -1,
            rag_status TEXT DEFAULT 'pending',
            rag_synced_at REAL DEFAULT 0,
            doc_id TEXT DEFAULT '',
            source TEXT DEFAULT 'user',
            access_count INTEGER DEFAULT 0,
            distilled INTEGER DEFAULT 0,
            entities TEXT DEFAULT '',
            event_type TEXT DEFAULT '',
            metadata_json TEXT DEFAULT '{}',
            content_hash TEXT DEFAULT '',
            version INTEGER DEFAULT 1,
            user_id TEXT DEFAULT 'default',
            agent_id TEXT DEFAULT 'xiaoda',
            is_raw INTEGER DEFAULT 0,
            updated_at REAL DEFAULT 0
        )
    """)
    await conn.commit()
    return MemoryDB(conn)


class TestReconcileVectorIndexGap:
    @pytest.mark.asyncio
    async def test_detects_raw_memory_missing_vec_and_warns(self):
        memdb = await _make_memory_db()
        await memdb.insert_episodic_memory("原始记忆A", is_raw=1, auto_commit=True)

        fake_vec = MagicMock()
        fake_vec._vec_conn = object()  # 模拟已初始化
        fake_vec.get_memories_vec_rowids = MagicMock(return_value=set())

        mm = _make_minimal_manager(memdb, fake_vec)
        seen, handler_id = _capture_warning_sink()
        try:
            missing = await mm.reconcile_vector_index_gap()
        finally:
            logger.remove(handler_id)

        assert missing == 1
        assert any("memory.vector_index_gap_detected" in m for m in seen), \
            f"应记录对账缺失 warning，实际 warnings: {seen}"

    @pytest.mark.asyncio
    async def test_returns_zero_when_all_indexed(self):
        memdb = await _make_memory_db()
        await memdb.insert_episodic_memory("原始记忆B", is_raw=1, auto_commit=True)

        fake_vec = MagicMock()
        fake_vec._vec_conn = object()
        # 向量表已有对应 rowid → 无缺失
        fake_vec.get_memories_vec_rowids = MagicMock(return_value={1})

        mm = _make_minimal_manager(memdb, fake_vec)
        seen, handler_id = _capture_warning_sink()
        try:
            missing = await mm.reconcile_vector_index_gap()
        finally:
            logger.remove(handler_id)

        assert missing == 0
        assert not any("memory.vector_index_gap_detected" in m for m in seen)

    @pytest.mark.asyncio
    async def test_returns_zero_when_vec_unavailable(self):
        memdb = await _make_memory_db()
        await memdb.insert_episodic_memory("原始记忆C", is_raw=1, auto_commit=True)

        mm = _make_minimal_manager(memdb, None)
        assert await mm.reconcile_vector_index_gap() == 0

    @pytest.mark.asyncio
    async def test_returns_zero_when_vec_not_initialized(self):
        memdb = await _make_memory_db()
        await memdb.insert_episodic_memory("原始记忆D", is_raw=1, auto_commit=True)

        fake_vec = MagicMock()
        fake_vec._vec_conn = None  # 未初始化
        mm = _make_minimal_manager(memdb, fake_vec)
        assert await mm.reconcile_vector_index_gap() == 0


# ── (C) 生产接线：启动流程应调用 reconcile ────────────────────

class TestReconcileWiredAtStartup:
    def test_bootstrap_wires_reconcile_after_memory_init(self):
        src = (Path(__file__).resolve().parent.parent
               / "core" / "bootstrap.py").read_text(encoding="utf-8")

        # 生产代码（非测试）必须调用 reconcile_vector_index_gap
        assert "reconcile_vector_index_gap" in src, \
            "bootstrap 应在 memory 初始化后接线向量索引对账"
        assert "_spawn(core.memory.reconcile_vector_index_gap()" in src, \
            "应以 fire-and-forget 方式调用 reconcile_vector_index_gap"

    def test_reconcile_wired_after_memory_construction(self):
        src = (Path(__file__).resolve().parent.parent
               / "core" / "bootstrap.py").read_text(encoding="utf-8")

        init_pos = src.find("core.memory = MemoryManager(")
        reconcile_pos = src.find("reconcile_vector_index_gap")
        assert init_pos != -1, "bootstrap 应构造 MemoryManager"
        assert reconcile_pos > init_pos, \
            "reconcile 调用应位于 MemoryManager 构造之后"


# ── (A) 普通失败日志级别提升 ─────────────────────────────────

class TestIndexingTaskFailureLogLevel:
    def test_normal_failures_are_warning_not_debug(self):
        src = (Path(__file__).resolve().parent.parent
               / "memory" / "memory_manager.py").read_text(encoding="utf-8")

        for key in (
            "memory.initial_vec_upsert_failed",
            "memory.concept_dual_write_failed",
            "memory.child_chunk_failed",
        ):
            assert f'logger.warning("{key}"' in src, f"{key} 应为 warning 级别"
            assert f'logger.debug("{key}"' not in src, f"{key} 不应为 debug 级别"

    def test_timeouts_stay_error(self):
        src = (Path(__file__).resolve().parent.parent
               / "memory" / "memory_manager.py").read_text(encoding="utf-8")

        for key in (
            "memory.encode_vec_upsert_timeout",
            "memory.encode_concept_timeout",
            "memory.encode_children_section_timeout",
        ):
            assert f'logger.error(' in src
