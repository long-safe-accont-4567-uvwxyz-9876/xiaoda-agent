# xiaoda-agent 记忆系统优化实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 基于 AMT 30种记忆技术，为 xiaoda-agent 实现 P0+P1 共6个核心记忆技术，将记忆覆盖度从47%提升至~75%。

**Architecture:** 增量式集成——新建5个模块（hierarchical_memory, memory_router, temporal_memory, cross_session_manager, reflection_store），扩展4个现有模块（agent_context, episodic_limiter, memory_manager, meta_cognition）。通过DB迁移v13添加分层/时间/反思相关列和表。

**Tech Stack:** Python 3.11, asyncio, aiosqlite, sqlite-vec, pytest, loguru

## Global Constraints

- Python 3.11 + asyncio + aiosqlite，虚拟环境 `.venv`
- 不引入 Neo4j 等重依赖，仅用 sqlite-vec
- 所有时间相关函数使用 `ZoneInfo("Asia/Shanghai")`
- 保持与现有代码风格一致（loguru 日志，type hints，dataclass）
- 不破坏现有线上稳定性（QQ Bot + Web UI 双进程）
- DB 迁移遵循现有 schema_version 机制（当前版本 12，新增 v13）
- `episodic_memories` 表已有 `access_count` 和 `importance` 列，无需重复添加
- 测试用 pytest，conftest.py 已配置 `temp_db_path` fixture

**Spec:** `docs/superpowers/specs/2026-07-11-memory-optimization-design.md`

---

## File Structure

**新建文件：**

| 文件 | 职责 |
|------|------|
| `memory/hierarchical_memory.py` | MemoryTier enum, TieredMemory dataclass, HierarchicalMemoryManager |
| `memory/memory_router.py` | MemoryType enum, MemoryRouter |
| `memory/temporal_memory.py` | TemporalMemory dataclass, TemporalMemoryStore |
| `memory/cross_session_manager.py` | SessionState dataclass, CrossSessionManager |
| `memory/reflection_store.py` | Reflection dataclass, ReflectionStore |
| `tests/test_hierarchical_memory.py` | #13 测试 |
| `tests/test_working_memory.py` | #12 测试 |
| `tests/test_memory_router.py` | #17 测试 |
| `tests/test_cross_session.py` | #21 测试 |
| `tests/test_temporal_memory.py` | #18 测试 |
| `tests/test_reflection_store.py` | #16 测试 |

**扩展文件：**

| 文件 | 扩展内容 |
|------|---------|
| `db/database.py` | 新增 `_migrate_v13` 迁移方法 |
| `agent_context.py` | 增加 WorkingMemoryItem, SalienceScorer |
| `memory/episodic_limiter.py` | 增加 EvictionEngine |
| `memory/memory_manager.py` | 集成 MemoryRouter 和 HierarchicalMemoryManager |
| `core/meta_cognition.py` | 增加 ReflectiveAgent |

---

## Task 1: DB Migration v13

**Files:**
- Modify: `db/database.py:195-208` (migrations 列表)
- Modify: `db/database.py` (新增 `_migrate_v13` 方法)
- Test: `tests/test_db_migration_v13.py`

**Interfaces:**
- Produces: `episodic_memories` 表新增 `tier TEXT DEFAULT 'l2_warm'`, `is_pinned INTEGER DEFAULT 0`, `event_time REAL`, `is_stable INTEGER DEFAULT 0` 列；新建 `reflection_store` 表

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_migration_v13.py
"""DB Migration v13 测试：验证新增列和表存在"""
import pytest
import aiosqlite
import tempfile
import os
from pathlib import Path


@pytest.fixture
async def migrated_db(tmp_path):
    """创建并迁移到 v13 的数据库"""
    db_path = tmp_path / "test_v13.db"
    from db.database import DatabaseManager
    db = DatabaseManager(db_path)
    await db.init()
    yield db
    await db.close()


class TestMigrationV13:
    async def test_episodic_memories_has_tier_column(self, migrated_db):
        """验证 episodic_memories 表有 tier 列"""
        cursor = await migrated_db._conn.execute("PRAGMA table_info(episodic_memories)")
        columns = [row[1] for row in await cursor.fetchall()]
        assert "tier" in columns

    async def test_episodic_memories_has_is_pinned_column(self, migrated_db):
        """验证 episodic_memories 表有 is_pinned 列"""
        cursor = await migrated_db._conn.execute("PRAGMA table_info(episodic_memories)")
        columns = [row[1] for row in await cursor.fetchall()]
        assert "is_pinned" in columns

    async def test_episodic_memories_has_event_time_column(self, migrated_db):
        """验证 episodic_memories 表有 event_time 列"""
        cursor = await migrated_db._conn.execute("PRAGMA table_info(episodic_memories)")
        columns = [row[1] for row in await cursor.fetchall()]
        assert "event_time" in columns

    async def test_episodic_memories_has_is_stable_column(self, migrated_db):
        """验证 episodic_memories 表有 is_stable 列"""
        cursor = await migrated_db._conn.execute("PRAGMA table_info(episodic_memories)")
        columns = [row[1] for row in await cursor.fetchall()]
        assert "is_stable" in columns

    async def test_reflection_store_table_exists(self, migrated_db):
        """验证 reflection_store 表存在"""
        cursor = await migrated_db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='reflection_store'"
        )
        row = await cursor.fetchone()
        assert row is not None

    async def test_reflection_store_has_correct_columns(self, migrated_db):
        """验证 reflection_store 表结构"""
        cursor = await migrated_db._conn.execute("PRAGMA table_info(reflection_store)")
        columns = [row[1] for row in await cursor.fetchall()]
        expected = {"id", "task_type", "outcome", "insight", "context_hash", "created_at"}
        assert expected.issubset(set(columns))

    async def test_schema_version_is_13(self, migrated_db):
        """验证 schema_version 为 13"""
        cursor = await migrated_db._conn.execute("SELECT MAX(version) FROM schema_version")
        row = await cursor.fetchone()
        assert row[0] == 13

    async def test_tier_default_value(self, migrated_db):
        """验证 tier 默认值为 l2_warm"""
        cursor = await migrated_db._conn.execute(
            "INSERT INTO episodic_memories (timestamp, summary) VALUES (?, ?)",
            (0, "test")
        )
        await migrated_db._conn.commit()
        cursor = await migrated_db._conn.execute("SELECT tier FROM episodic_memories WHERE summary='test'")
        row = await cursor.fetchone()
        assert row[0] == "l2_warm"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_db_migration_v13.py -v`
Expected: FAIL — columns and table don't exist yet

- [ ] **Step 3: Implement the migration method**

Add `_migrate_v13` method to `DatabaseManager` class in `db/database.py`:

```python
async def _migrate_v13(self) -> None:
    """v13: 记忆系统优化 — 分层记忆/时间推理/自反思支持"""
    # episodic_memories 新增列
    try:
        await self._conn.execute(
            "ALTER TABLE episodic_memories ADD COLUMN tier TEXT DEFAULT 'l2_warm'"
        )
    except Exception:
        pass  # 列已存在
    try:
        await self._conn.execute(
            "ALTER TABLE episodic_memories ADD COLUMN is_pinned INTEGER DEFAULT 0"
        )
    except Exception:
        pass
    try:
        await self._conn.execute(
            "ALTER TABLE episodic_memories ADD COLUMN event_time REAL"
        )
    except Exception:
        pass
    try:
        await self._conn.execute(
            "ALTER TABLE episodic_memories ADD COLUMN is_stable INTEGER DEFAULT 0"
        )
    except Exception:
        pass

    # reflection_store 新表
    await self._conn.execute("""
        CREATE TABLE IF NOT EXISTS reflection_store (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT NOT NULL,
            outcome TEXT NOT NULL,
            insight TEXT NOT NULL,
            context_hash TEXT,
            created_at REAL NOT NULL
        )
    """)
    await self._conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reflection_type ON reflection_store(task_type)"
    )

    # 更新 schema.sql 的 CURRENT_SCHEMA_VERSION
    logger.info("database.migrate_v13: memory optimization columns + reflection_store")
```

- [ ] **Step 4: Register migration in the migrations list**

In `db/database.py`, update the migrations list (around line 207):

```python
        migrations = [
            (1, "temporal_knowledge_graph", self._migrate_v1),
            (2, "conversation_logs.session_id", self._migrate_v2),
            (3, "fts5_index+consolidation_candidates", self._migrate_v3),
            (4, "episodic_memories.source", self._migrate_v4),
            (5, "knowledge_entities_fts_backfill", self._migrate_v5),
            (6, "episodic_memories.access_count", self._migrate_v6),
            (7, "episodic_memories.session_id+embedding_id", self._migrate_v7),
            (8, "episodic_memories.rag_status+rag_synced_at+doc_id", self._migrate_v8),
            (9, "memory_summaries+episodic_memories.distilled", self._migrate_v9),
            (10, "episodic_memories.entities+event_type+metadata_json", self._migrate_v10),
            (11, "memory_recall_notes", self._migrate_v11),
            (12, "episodic_memories.content_hash+version+memory_versions+context_audit_log", self._migrate_v12),
            (13, "memory_optimization_tier+temporal+reflection", self._migrate_v13),
        ]
```

Also update `CURRENT_SCHEMA_VERSION = 13` at the top of the file.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_db_migration_v13.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
cd /home/orangepi/ai-agent && git add db/database.py tests/test_db_migration_v13.py && git commit -m "feat(db): v13 migration for memory optimization (tier/temporal/reflection)"
```

---

## Task 2: Hierarchical Memory Architecture (#13)

**Files:**
- Create: `memory/hierarchical_memory.py`
- Test: `tests/test_hierarchical_memory.py`

**Interfaces:**
- Consumes: `aiosqlite.Connection` (from DatabaseManager), `VectorStore` (existing)
- Produces: `MemoryTier`, `TieredMemory`, `HierarchicalMemoryManager`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hierarchical_memory.py
"""分层记忆架构 #13 测试"""
import pytest
import asyncio
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock


class TestMemoryTier:
    def test_tier_values(self):
        from memory.hierarchical_memory import MemoryTier
        assert MemoryTier.L1_HOT.value == "l1_hot"
        assert MemoryTier.L2_WARM.value == "l2_warm"
        assert MemoryTier.L3_COLD.value == "l3_cold"


class TestTieredMemory:
    def test_default_values(self):
        from memory.hierarchical_memory import TieredMemory
        m = TieredMemory(
            id="test-1",
            content="hello",
            embedding=[0.1, 0.2],
        )
        assert m.tier.value == "l2_warm"
        assert m.access_count == 0
        assert m.importance == 0.5
        assert m.emotional_weight == 0.0
        assert m.is_pinned is False


class TestHierarchicalMemoryManager:
    @pytest.fixture
    def mock_conn(self):
        conn = AsyncMock()
        # 模拟 execute 返回
        cursor = AsyncMock()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []
        conn.execute.return_value = cursor
        return conn

    async def test_store_default_l2(self, mock_conn):
        """新记忆默认进入 L2"""
        from memory.hierarchical_memory import HierarchicalMemoryManager, MemoryTier
        mgr = HierarchicalMemoryManager(conn=mock_conn)
        memory = await mgr.store(
            content="test content",
            embedding=[0.1, 0.2],
            importance=0.5,
        )
        assert memory.tier == MemoryTier.L2_WARM

    async def test_store_high_importance_direct_l1(self, mock_conn):
        """高重要性+高情感权重直接入 L1"""
        from memory.hierarchical_memory import HierarchicalMemoryManager, MemoryTier
        mgr = HierarchicalMemoryManager(conn=mock_conn)
        memory = await mgr.store(
            content="用户倾诉了创伤事件",
            embedding=[0.1, 0.2],
            importance=0.9,
            emotional_weight=0.8,
        )
        assert memory.tier == MemoryTier.L1_HOT

    async def test_pin_memory(self, mock_conn):
        """固定记忆到 L1"""
        from memory.hierarchical_memory import HierarchicalMemoryManager, MemoryTier
        mgr = HierarchicalMemoryManager(conn=mock_conn)
        await mgr.pin(memory_id=1)
        # 验证 SQL 调用
        mock_conn.execute.assert_called()

    async def test_demote_skips_pinned(self, mock_conn):
        """降级跳过 is_pinned 的记忆"""
        from memory.hierarchical_memory import HierarchicalMemoryManager
        mgr = HierarchicalMemoryManager(conn=mock_conn, demote_staleness_hours=0)
        # 设置 mock 返回非 pinned 记忆
        cursor = mock_conn.execute.return_value
        cursor.fetchall.return_value = [(1, "content", "l2_warm", 0)]
        demoted = await mgr.demote()
        assert isinstance(demoted, int)

    async def test_promote_after_threshold(self, mock_conn):
        """访问次数超阈值时晋升"""
        from memory.hierarchical_memory import HierarchicalMemoryManager
        mgr = HierarchicalMemoryManager(conn=mock_conn, promote_threshold=3)
        # 模拟访问计数达到阈值
        cursor = mock_conn.execute.return_value
        cursor.fetchall.return_value = [(1, "content", "l2_warm", 3)]
        promoted = await mgr.promote(memory_id=1)
        assert promoted is True

    async def test_emotional_weight_prevents_demote(self, mock_conn):
        """高情感权重防止降级"""
        from memory.hierarchical_memory import HierarchicalMemoryManager
        mgr = HierarchicalMemoryManager(
            conn=mock_conn,
            demote_staleness_hours=0,
            emotional_weight_threshold=0.7,
        )
        # mock 返回高情感权重的记忆
        cursor = mock_conn.execute.return_value
        cursor.fetchall.return_value = []  # 高情感权重的被跳过
        demoted = await mgr.demote()
        assert demoted == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_hierarchical_memory.py -v`
Expected: FAIL — `memory.hierarchical_memory` module not found

- [ ] **Step 3: Implement hierarchical_memory.py**

```python
# memory/hierarchical_memory.py
"""分层记忆架构 — AMT #13

三层记忆管理：L1热层（上下文窗口）→ L2温层（近期高频）→ L3冷层（归档）
情感陪伴特殊逻辑：高情感权重记忆优先晋升，创伤记忆永不降级。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from loguru import logger


class MemoryTier(Enum):
    """记忆层级"""
    L1_HOT = "l1_hot"       # 上下文窗口内，内存
    L2_WARM = "l2_warm"     # 近期高频，sqlite-vec + 内存缓存
    L3_COLD = "l3_cold"     # 历史归档，sqlite-vec 冷分区


@dataclass
class TieredMemory:
    """带层级标签和访问追踪的记忆条目"""
    id: str
    content: str
    embedding: list[float] = field(default_factory=list)
    tier: MemoryTier = MemoryTier.L2_WARM
    access_count: int = 0
    last_accessed: datetime = field(default_factory=datetime.now)
    created_at: datetime = field(default_factory=datetime.now)
    importance: float = 0.5
    emotional_weight: float = 0.0
    is_pinned: bool = False
    metadata: dict = field(default_factory=dict)


class HierarchicalMemoryManager:
    """三层记忆管理器

    集成点：
    - agent_context.py 的 MAX_HISTORY_TOKENS 作为 L1 容量参考
    - sqlite-vec 作为 L2/L3 后端，通过 tier 字段区分
    - emotional_memory.py 的 emotional_weight 影响晋升/降级
    """

    def __init__(
        self,
        conn: Any = None,
        l1_capacity_tokens: int = 4096,
        l2_capacity: int = 1000,
        promote_threshold: int = 3,
        demote_staleness_hours: int = 48,
        emotional_weight_threshold: float = 0.7,
        importance_threshold: float = 0.7,
    ) -> None:
        self._conn = conn
        self.l1_capacity_tokens = l1_capacity_tokens
        self.l2_capacity = l2_capacity
        self.promote_threshold = promote_threshold
        self.demote_staleness_hours = demote_staleness_hours
        self.emotional_weight_threshold = emotional_weight_threshold
        self.importance_threshold = importance_threshold
        # L1 内存中的有序列表
        self._l1_items: list[TieredMemory] = []

    async def store(
        self,
        content: str,
        embedding: list[float],
        importance: float = 0.5,
        emotional_weight: float = 0.0,
        metadata: dict | None = None,
    ) -> TieredMemory:
        """新记忆默认进入 L2，高情感权重+高重要性直接进入 L1"""
        memory = TieredMemory(
            id=str(uuid.uuid4()),
            content=content,
            embedding=embedding,
            importance=importance,
            emotional_weight=emotional_weight,
            metadata=metadata or {},
        )

        # 情感陪伴特殊逻辑：高情感权重 + 高重要性 → 直接入 L1
        if importance >= self.importance_threshold and emotional_weight >= self.emotional_weight_threshold:
            memory.tier = MemoryTier.L1_HOT
            self._l1_items.append(memory)
            logger.debug(
                "hierarchical.store_l1",
                content=content[:50],
                importance=importance,
                emotional_weight=emotional_weight,
            )
        else:
            memory.tier = MemoryTier.L2_WARM

        # 持久化到 DB
        if self._conn:
            try:
                await self._conn.execute(
                    "UPDATE episodic_memories SET tier=?, importance=?, is_pinned=? "
                    "WHERE id=?",
                    (memory.tier.value, importance, 0, memory.id),
                )
                await self._conn.commit()
            except Exception as e:
                logger.warning(f"hierarchical.store_db_failed: {e}")

        return memory

    async def retrieve(
        self,
        query: str,
        embedding: list[float] | None = None,
        limit: int = 10,
    ) -> list[TieredMemory]:
        """级联检索：L1 → L2 → L3，先命中先返回"""
        results: list[TieredMemory] = []

        # L1: 内存中线性搜索
        for item in self._l1_items:
            if query.lower() in item.content.lower():
                item.access_count += 1
                item.last_accessed = datetime.now(timezone.utc)
                results.append(item)
                if len(results) >= limit:
                    return results

        # L2/L3: 数据库检索
        if self._conn and len(results) < limit:
            try:
                cursor = await self._conn.execute(
                    "SELECT id, summary, tier, access_count, importance, is_pinned "
                    "FROM episodic_memories "
                    "WHERE summary LIKE ? AND tier IN ('l2_warm', 'l3_cold') "
                    "ORDER BY tier ASC, importance DESC, access_count DESC "
                    "LIMIT ?",
                    (f"%{query}%", limit - len(results)),
                )
                rows = await cursor.fetchall()
                for row in rows:
                    results.append(TieredMemory(
                        id=str(row[0]),
                        content=row[1],
                        tier=MemoryTier(row[2]),
                        access_count=row[3],
                        importance=row[4],
                        is_pinned=bool(row[5]),
                    ))
            except Exception as e:
                logger.warning(f"hierarchical.retrieve_db_failed: {e}")

        return results

    async def promote(self, memory_id: int | str) -> bool:
        """访问次数超阈值时 L3→L2 或 L2→L1 晋升"""
        if not self._conn:
            return False
        try:
            cursor = await self._conn.execute(
                "SELECT tier, access_count FROM episodic_memories WHERE id=?",
                (memory_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return False

            current_tier = MemoryTier(row[0])
            access_count = row[1]

            if access_count < self.promote_threshold:
                return False

            # 晋升逻辑
            if current_tier == MemoryTier.L3_COLD:
                new_tier = MemoryTier.L2_WARM
            elif current_tier == MemoryTier.L2_WARM:
                new_tier = MemoryTier.L1_HOT
            else:
                return False  # 已在 L1

            await self._conn.execute(
                "UPDATE episodic_memories SET tier=? WHERE id=?",
                (new_tier.value, memory_id),
            )
            await self._conn.commit()
            logger.debug(f"hierarchical.promote: {memory_id} {current_tier.value}→{new_tier.value}")
            return True
        except Exception as e:
            logger.warning(f"hierarchical.promote_failed: {e}")
            return False

    async def demote(self) -> int:
        """定时扫描，超时未访问降级（跳过 is_pinned 和高 emotional_weight）"""
        if not self._conn:
            return 0
        try:
            cutoff = time.time() - self.demote_staleness_hours * 3600
            # 查找需要降级的 L2 记忆（跳过 is_pinned）
            cursor = await self._conn.execute(
                "SELECT id FROM episodic_memories "
                "WHERE tier='l2_warm' AND is_pinned=0 "
                "AND timestamp < ? "
                "AND importance < ?",
                (cutoff, self.importance_threshold),
            )
            rows = await cursor.fetchall()
            demoted_count = 0
            for row in rows:
                await self._conn.execute(
                    "UPDATE episodic_memories SET tier='l3_cold' WHERE id=?",
                    (row[0],),
                )
                demoted_count += 1
            if demoted_count > 0:
                await self._conn.commit()
                logger.info(f"hierarchical.demote: {demoted_count} items L2→L3")
            return demoted_count
        except Exception as e:
            logger.warning(f"hierarchical.demote_failed: {e}")
            return 0

    async def pin(self, memory_id: int | str) -> None:
        """固定记忆到 L1（如用户核心情感状态）"""
        if not self._conn:
            return
        try:
            await self._conn.execute(
                "UPDATE episodic_memories SET tier='l1_hot', is_pinned=1 WHERE id=?",
                (memory_id,),
            )
            await self._conn.commit()
            logger.debug(f"hierarchical.pin: {memory_id}")
        except Exception as e:
            logger.warning(f"hierarchical.pin_failed: {e}")

    async def unpin(self, memory_id: int | str) -> None:
        """取消固定"""
        if not self._conn:
            return
        try:
            await self._conn.execute(
                "UPDATE episodic_memories SET is_pinned=0 WHERE id=?",
                (memory_id,),
            )
            await self._conn.commit()
        except Exception as e:
            logger.warning(f"hierarchical.unpin_failed: {e}")

    def get_l1_items(self) -> list[TieredMemory]:
        """获取当前 L1 热层所有记忆"""
        return list(self._l1_items)

    async def get_timeline(self, start: datetime, end: datetime) -> list[TieredMemory]:
        """时间范围查询（为 #18 预留接口）"""
        if not self._conn:
            return []
        try:
            cursor = await self._conn.execute(
                "SELECT id, summary, tier, access_count, importance, is_pinned, timestamp "
                "FROM episodic_memories "
                "WHERE timestamp BETWEEN ? AND ? "
                "ORDER BY timestamp ASC",
                (start.timestamp(), end.timestamp()),
            )
            rows = await cursor.fetchall()
            return [
                TieredMemory(
                    id=str(row[0]),
                    content=row[1],
                    tier=MemoryTier(row[2]),
                    access_count=row[3],
                    importance=row[4],
                    is_pinned=bool(row[5]),
                )
                for row in rows
            ]
        except Exception as e:
            logger.warning(f"hierarchical.get_timeline_failed: {e}")
            return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_hierarchical_memory.py -v`
Expected: PASS

- [ ] **Step 5: Verify no regression**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m py_compile memory/hierarchical_memory.py`
Expected: No errors

- [ ] **Step 6: Commit**

```bash
cd /home/orangepi/ai-agent && git add memory/hierarchical_memory.py tests/test_hierarchical_memory.py && git commit -m "feat(memory): #13 hierarchical memory architecture (L1/L2/L3 tiers)"
```

---

## Task 3: Working Memory Context Window Management (#12)

**Files:**
- Modify: `agent_context.py` (add WorkingMemoryItem, SalienceScorer)
- Modify: `memory/episodic_limiter.py` (add EvictionEngine)
- Test: `tests/test_working_memory.py`

**Interfaces:**
- Consumes: `VectorStore` embedding function (existing), `AgentContext` (existing)
- Produces: `WorkingMemoryItem`, `SalienceScorer`, `EvictionEngine`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_working_memory.py
"""工作记忆上下文窗口管理 #12 测试"""
import pytest
import time
import math
from dataclasses import dataclass
from unittest.mock import MagicMock


class TestWorkingMemoryItem:
    def test_creation(self):
        from agent_context import WorkingMemoryItem
        item = WorkingMemoryItem(
            content="hello",
            role="user",
            token_count=5,
            salience_score=0.8,
            is_pinned=False,
            timestamp=time.time(),
            emotional_weight=0.5,
        )
        assert item.content == "hello"
        assert item.role == "user"
        assert item.salience_score == 0.8


class TestSalienceScorer:
    def test_score_returns_float(self):
        from agent_context import SalienceScorer
        scorer = SalienceScorer()
        item = type('Item', (), {
            'content': 'test',
            'embedding': [0.1, 0.2],
            'timestamp': time.time(),
            'role': 'user',
        })()
        score = scorer.score(item, current_query_embedding=[0.1, 0.2])
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0

    def test_high_relevance_high_score(self):
        from agent_context import SalienceScorer
        scorer = SalienceScorer()
        now = time.time()
        item = type('Item', (), {
            'content': 'test',
            'embedding': [1.0, 0.0],
            'timestamp': now,
            'role': 'user',
        })()
        # 完全匹配的 embedding 应得高分
        score = scorer.score(item, current_query_embedding=[1.0, 0.0])
        assert score > 0.5

    def test_old_item_lower_score(self):
        from agent_context import SalienceScorer
        scorer = SalienceScorer()
        old_time = time.time() - 3600  # 1小时前
        recent_time = time.time()
        old_item = type('Item', (), {
            'content': 'test',
            'embedding': [1.0, 0.0],
            'timestamp': old_time,
            'role': 'user',
        })()
        recent_item = type('Item', (), {
            'content': 'test',
            'embedding': [1.0, 0.0],
            'timestamp': recent_time,
            'role': 'user',
        })()
        old_score = scorer.score(old_item, current_query_embedding=[1.0, 0.0])
        recent_score = scorer.score(recent_item, current_query_embedding=[1.0, 0.0])
        assert recent_score > old_score


class TestEvictionEngine:
    def test_evict_lru(self):
        from memory.episodic_limiter import EvictionEngine
        engine = EvictionEngine()
        items = [
            {"id": 1, "access_count": 5, "last_accessed": 100},
            {"id": 2, "access_count": 1, "last_accessed": 50},
            {"id": 3, "access_count": 10, "last_accessed": 200},
        ]
        evicted = engine.select_for_eviction_lru(items, count=1)
        assert evicted[0]["id"] == 2  # 最久未访问

    def test_evict_importance_weighted_skips_pinned(self):
        from memory.episodic_limiter import EvictionEngine
        engine = EvictionEngine()
        items = [
            {"id": 1, "salience": 0.3, "emotional_weight": 0.2, "recency": 0.5, "is_pinned": False},
            {"id": 2, "salience": 0.1, "emotional_weight": 0.1, "recency": 0.1, "is_pinned": True},  # pinned, 不可逐出
            {"id": 3, "salience": 0.2, "emotional_weight": 0.1, "recency": 0.2, "is_pinned": False},
        ]
        evicted = engine.select_for_eviction_weighted(items, count=1)
        assert all(not e["is_pinned"] for e in evicted)

    def test_evict_weighted_formula(self):
        """验证加权公式：salience * 0.3 + emotional_weight * 0.4 + recency * 0.3"""
        from memory.episodic_limiter import EvictionEngine
        engine = EvictionEngine()
        items = [
            {"id": 1, "salience": 0.1, "emotional_weight": 0.0, "recency": 0.1, "is_pinned": False},
            {"id": 2, "salience": 0.9, "emotional_weight": 0.9, "recency": 0.9, "is_pinned": False},
        ]
        evicted = engine.select_for_eviction_weighted(items, count=1)
        assert evicted[0]["id"] == 1  # 分数最低的被逐出
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_working_memory.py -v`
Expected: FAIL — `WorkingMemoryItem`, `SalienceScorer`, `EvictionEngine` not defined

- [ ] **Step 3: Add WorkingMemoryItem and SalienceScorer to agent_context.py**

Add these classes to `agent_context.py` (before the `AgentContext` class):

```python
# Add these imports at the top if not present
import math
from dataclasses import dataclass


@dataclass
class WorkingMemoryItem:
    """工作记忆条目 — 带显著性评分和固定标记"""
    content: str
    role: str               # user/assistant/system
    token_count: int
    salience_score: float = 0.5   # 0-1 显著性分数
    is_pinned: bool = False       # 固定标记
    timestamp: float = 0.0
    emotional_weight: float = 0.0


# 来源权重：不同角色的消息有不同的默认重要性
_SOURCE_WEIGHTS = {
    "user": 1.0,       # 用户消息最重要
    "assistant": 0.7,  # 助手回复次之
    "system": 0.5,     # 系统消息最低
}


class SalienceScorer:
    """三维融合评分：embedding相似度 x 指数衰减 x 来源权重"""

    def __init__(self, alpha: float = 0.001) -> None:
        """alpha: 衰减率，越大旧消息分数越低"""
        self._alpha = alpha

    @staticmethod
    def _cosine_sim(a: list[float], b: list[float]) -> float:
        """余弦相似度"""
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return max(0.0, min(1.0, dot / (norm_a * norm_b)))

    def score(self, item, current_query_embedding: list[float] | None = None) -> float:
        """计算显著性分数 (0-1)"""
        # 1. 相关性：embedding 余弦相似度
        relevance = 0.5  # 默认中等相关
        item_embedding = getattr(item, 'embedding', None)
        if current_query_embedding and item_embedding:
            relevance = self._cosine_sim(item_embedding, current_query_embedding)

        # 2. 时间衰减：指数衰减
        now = time.time()
        item_time = getattr(item, 'timestamp', now)
        age = now - item_time
        recency = math.exp(-self._alpha * age)

        # 3. 来源权重
        role = getattr(item, 'role', 'user')
        source_weight = _SOURCE_WEIGHTS.get(role, 0.5)

        # 融合分数
        return relevance * recency * source_weight
```

- [ ] **Step 4: Add EvictionEngine to episodic_limiter.py**

Add this class to `memory/episodic_limiter.py` (before `EpisodicLimiter` class):

```python
class EvictionEngine:
    """动态逐出引擎 — 扩展 EpisodicLimiter 的评分公式

    两种逐出策略：
    1. LRU: 最久未访问优先逐出
    2. 重要性加权: salience * 0.3 + emotional_weight * 0.4 + recency * 0.3
       跳过 is_pinned=True 的项目
    """

    # 权重常量
    WEIGHT_SALIENCE = 0.3
    WEIGHT_EMOTIONAL = 0.4
    WEIGHT_RECENCY = 0.3

    def select_for_eviction_lru(self, items: list[dict], count: int = 1) -> list[dict]:
        """LRU 策略：按 last_accessed 升序选择"""
        sorted_items = sorted(items, key=lambda x: x.get("last_accessed", 0))
        return sorted_items[:count]

    def select_for_eviction_weighted(self, items: list[dict], count: int = 1) -> list[dict]:
        """重要性加权策略：跳过 is_pinned，按综合分数升序选择"""
        # 过滤掉 pinned 的项目
        candidates = [item for item in items if not item.get("is_pinned", False)]
        if not candidates:
            return []

        # 计算综合分数
        def _score(item: dict) -> float:
            salience = item.get("salience", 0.5)
            emotional = item.get("emotional_weight", 0.0)
            recency = item.get("recency", 0.5)
            return (
                salience * self.WEIGHT_SALIENCE
                + emotional * self.WEIGHT_EMOTIONAL
                + recency * self.WEIGHT_RECENCY
            )

        # 按分数升序排列，分数最低的优先逐出
        sorted_candidates = sorted(candidates, key=_score)
        return sorted_candidates[:count]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_working_memory.py -v`
Expected: PASS

- [ ] **Step 6: Verify no regression**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m py_compile agent_context.py memory/episodic_limiter.py`
Expected: No errors

- [ ] **Step 7: Commit**

```bash
cd /home/orangepi/ai-agent && git add agent_context.py memory/episodic_limiter.py tests/test_working_memory.py && git commit -m "feat(memory): #12 working memory with salience scoring and eviction engine"
```

---

## Task 4: Memory Router (#17)

**Files:**
- Create: `memory/memory_router.py`
- Test: `tests/test_memory_router.py`

**Interfaces:**
- Consumes: Various memory stores (EmotionalMemory, VectorStore, KnowledgeGraph, etc.)
- Produces: `MemoryType`, `MemoryRouter`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_router.py
"""记忆路由 #17 测试"""
import pytest
from unittest.mock import AsyncMock, MagicMock


class TestMemoryType:
    def test_enum_values(self):
        from memory.memory_router import MemoryType
        assert MemoryType.EPISODIC.value == "episodic"
        assert MemoryType.SEMANTIC.value == "semantic"
        assert MemoryType.PROCEDURAL.value == "procedural"
        assert MemoryType.EMOTIONAL.value == "emotional"
        assert MemoryType.TEMPORAL.value == "temporal"


class TestMemoryRouter:
    @pytest.fixture
    def mock_stores(self):
        episodic = AsyncMock()
        episodic.retrieve.return_value = [{"content": "episodic result"}]
        semantic = AsyncMock()
        semantic.retrieve.return_value = [{"content": "semantic result"}]
        emotional = AsyncMock()
        emotional.retrieve.return_value = [{"content": "emotional result"}]
        temporal = AsyncMock()
        temporal.retrieve.return_value = [{"content": "temporal result"}]
        return {
            "episodic": episodic,
            "semantic": semantic,
            "emotional": emotional,
            "temporal": temporal,
        }

    async def test_route_temporal_keywords(self, mock_stores):
        """时间词路由到 TEMPORAL"""
        from memory.memory_router import MemoryRouter, MemoryType
        router = MemoryRouter(stores=mock_stores)
        results = await router.route("上次我们聊了什么")
        mock_stores["temporal"].retrieve.assert_called_once()
        assert results == [{"content": "temporal result"}]

    async def test_route_emotional_keywords(self, mock_stores):
        """情绪词路由到 EMOTIONAL"""
        from memory.memory_router import MemoryRouter, MemoryType
        router = MemoryRouter(stores=mock_stores)
        results = await router.route("用户现在心情怎样")
        mock_stores["emotional"].retrieve.assert_called_once()
        assert results == [{"content": "emotional result"}]

    async def test_route_fallback_search(self, mock_stores):
        """分类未命中时走 fallback"""
        from memory.memory_router import MemoryRouter
        fallback = AsyncMock()
        fallback.retrieve.return_value = [{"content": "fallback"}]
        router = MemoryRouter(stores=mock_stores, fallback_store=fallback)
        # 用一个不匹配任何规则词的查询
        results = await router.route("用户的猫叫小橘")
        # 应该走 semantic (LLM 分类 fallback) 或 fallback store
        assert len(results) > 0

    def test_has_temporal_keywords(self):
        from memory.memory_router import MemoryRouter
        router = MemoryRouter(stores={})
        assert router._has_temporal_keywords("上次你说了什么") is True
        assert router._has_temporal_keywords("昨天聊的话题") is True
        assert router._has_temporal_keywords("用户的猫") is False

    def test_has_emotional_keywords(self):
        from memory.memory_router import MemoryRouter
        router = MemoryRouter(stores={})
        assert router._has_emotional_keywords("用户心情怎样") is True
        assert router._has_emotional_keywords("用户很伤心") is True
        assert router._has_emotional_keywords("用户的猫叫小橘") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_memory_router.py -v`
Expected: FAIL — `memory.memory_router` module not found

- [ ] **Step 3: Implement memory_router.py**

```python
# memory/memory_router.py
"""记忆路由 — AMT #17

按内容类型路由查询到对应记忆后端。
分类策略：规则层（毫秒级）→ 缓存层 → LLM 层（仅规则未命中时）。
"""
from __future__ import annotations

import re
import time
from enum import Enum
from typing import Any

from loguru import logger


class MemoryType(Enum):
    """记忆类型"""
    EPISODIC = "episodic"      # 事件回忆
    SEMANTIC = "semantic"      # 事实知识
    PROCEDURAL = "procedural"  # 过程性知识
    EMOTIONAL = "emotional"    # 情感状态
    TEMPORAL = "temporal"      # 时间推理


# 时间关键词（与 memory_manager.py 的 _TEMPORAL_PATTERNS 对齐）
_TEMPORAL_KEYWORDS = [
    "上次", "昨天", "前天", "今天", "上周", "上个月", "前几天", "最近",
    "之前", "以前", "后来", "那之后", "什么时候", "哪一天",
]

# 情绪关键词
_EMOTIONAL_KEYWORDS = [
    "心情", "情绪", "开心", "伤心", "难过", "生气", "焦虑", "抑郁",
    "害怕", "担心", "压力", "累", "孤独", "幸福", "感觉",
]

# 缓存 TTL
_CLASSIFY_CACHE_TTL = 300  # 5 分钟


class MemoryRouter:
    """记忆路由分发器"""

    def __init__(
        self,
        stores: dict[str, Any] | dict[MemoryType, Any],
        fallback_store: Any = None,
    ) -> None:
        # 统一转为字符串键
        self._stores: dict[str, Any] = {}
        for key, value in stores.items():
            if isinstance(key, MemoryType):
                self._stores[key.value] = value
            else:
                self._stores[key] = value
        self._fallback = fallback_store
        self._classify_cache: dict[str, tuple[MemoryType, float]] = {}

    async def route(
        self,
        query: str,
        embedding: list[float] | None = None,
    ) -> list:
        """路由查询到对应记忆存储"""
        mem_type = await self._classify(query)

        # 获取对应存储
        store = self._stores.get(mem_type.value)
        if store:
            try:
                results = await store.retrieve(query, embedding)
                if results:
                    return results
            except Exception as e:
                logger.warning(f"router.store_failed: type={mem_type.value}, error={e}")

        # fallback: 全量搜索
        return await self._fallback_search(query, embedding)

    async def _classify(self, query: str) -> MemoryType:
        """轻量分类器：规则优先 → 缓存 → LLM fallback"""
        # 检查缓存
        cached = self._classify_cache.get(query)
        if cached and time.time() - cached[1] < _CLASSIFY_CACHE_TTL:
            return cached[0]

        # 规则层
        if self._has_temporal_keywords(query):
            result = MemoryType.TEMPORAL
        elif self._has_emotional_keywords(query):
            result = MemoryType.EMOTIONAL
        else:
            # LLM fallback (简化版：默认走 EPISODIC)
            # 完整实现会用轻量 LLM prompt 分类
            result = MemoryType.EPISODIC

        # 写入缓存
        self._classify_cache[query] = (result, time.time())
        return result

    def _has_temporal_keywords(self, query: str) -> bool:
        """检测时间关键词"""
        return any(kw in query for kw in _TEMPORAL_KEYWORDS)

    def _has_emotional_keywords(self, query: str) -> bool:
        """检测情绪关键词"""
        return any(kw in query for kw in _EMOTIONAL_KEYWORDS)

    async def _fallback_search(
        self,
        query: str,
        embedding: list[float] | None = None,
    ) -> list:
        """全量搜索 fallback"""
        if self._fallback:
            try:
                return await self._fallback.retrieve(query, embedding)
            except Exception as e:
                logger.warning(f"router.fallback_failed: {e}")

        # 遍历所有存储
        all_results: list = []
        for store in self._stores.values():
            try:
                results = await store.retrieve(query, embedding)
                all_results.extend(results or [])
            except Exception:
                continue
        return all_results

    def clear_cache(self) -> None:
        """清空分类缓存"""
        self._classify_cache.clear()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_memory_router.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent && git add memory/memory_router.py tests/test_memory_router.py && git commit -m "feat(memory): #17 memory router with rule-based classification"
```

---

## Task 5: Cross-Session Memory Bridge (#21)

**Files:**
- Create: `memory/cross_session_manager.py`
- Test: `tests/test_cross_session.py`

**Interfaces:**
- Consumes: `SessionStoreProtocol` (existing in db/session_store.py), `HierarchicalMemoryManager`
- Produces: `SessionState`, `CrossSessionManager`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cross_session.py
"""跨会话记忆桥接 #21 测试"""
import pytest
import time
from unittest.mock import AsyncMock, MagicMock


class TestSessionState:
    def test_creation(self):
        from memory.cross_session_manager import SessionState
        state = SessionState(
            facts=[{"key": "name", "value": "test"}],
            conversation_summary="用户讨论了工作压力",
            recent_messages=[{"role": "user", "content": "hi"}],
            user_preferences={"tone": "casual"},
            emotional_snapshot={"dominant_emotion": "anxious", "intensity": 0.6},
            last_active=time.time(),
        )
        assert state.facts[0]["value"] == "test"
        assert state.emotional_snapshot["dominant_emotion"] == "anxious"

    def test_cold_start_state(self):
        from memory.cross_session_manager import SessionState
        state = SessionState(
            facts=[],
            conversation_summary="",
            recent_messages=[],
            user_preferences={},
            emotional_snapshot={},
            last_active=0,
            metadata={"cold_start": True},
        )
        assert state.metadata.get("cold_start") is True


class TestCrossSessionManager:
    @pytest.fixture
    def mock_store(self):
        store = AsyncMock()
        return store

    async def test_restore_full_for_short_offline(self, mock_store):
        """离线<1小时 → full 策略"""
        from memory.cross_session_manager import CrossSessionManager
        mgr = CrossSessionManager(store=mock_store)
        # 模拟 30 分钟前活跃
        mock_store.load_session.return_value = [
            {"timestamp": time.time() * 1000 - 1800_000, "data": {}}
        ]
        state = await mgr.restore_session("test-session")
        assert state is not None

    async def test_restore_summary_for_long_offline(self, mock_store):
        """离线>24小时 → summary 策略"""
        from memory.cross_session_manager import CrossSessionManager
        mgr = CrossSessionManager(store=mock_store)
        # 模拟 48 小时前活跃
        mock_store.load_session.return_value = None
        state = await mgr.restore_session("test-session")
        # 数据不存在时走 cold_start
        assert state is not None

    async def test_cold_start_on_corruption(self, mock_store):
        """数据损坏时走 cold_start"""
        from memory.cross_session_manager import CrossSessionManager, SessionState
        mock_store.load_session.side_effect = Exception("DB corrupted")
        mgr = CrossSessionManager(store=mock_store)
        state = await mgr.restore_session("test-session")
        assert state is not None
        assert state.metadata.get("cold_start") is True

    async def test_save_session(self, mock_store):
        """保存会话状态"""
        from memory.cross_session_manager import CrossSessionManager, SessionState
        mgr = CrossSessionManager(store=mock_store)
        state = SessionState(
            facts=[],
            conversation_summary="test",
            recent_messages=[],
            user_preferences={},
            emotional_snapshot={},
            last_active=time.time(),
        )
        await mgr.save_session("test-session", state)
        mock_store.append_session_entry.assert_called_once()

    def test_calc_offline_hours(self, mock_store):
        """计算离线时间"""
        from memory.cross_session_manager import CrossSessionManager
        mgr = CrossSessionManager(store=mock_store)
        # 2小时前
        hours = mgr._calc_offline_hours(time.time() * 1000 - 7200_000)
        assert 1.5 <= hours <= 2.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_cross_session.py -v`
Expected: FAIL — `memory.cross_session_manager` module not found

- [ ] **Step 3: Implement cross_session_manager.py**

```python
# memory/cross_session_manager.py
"""跨会话记忆桥接 — AMT #21

三种加载策略：full（<1h）/ last_n（<24h）/ summary（>=24h）
情感陪伴：归巢问候、情感连续性、渐进式了解、失忆恢复
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass
class SessionState:
    """会话状态快照"""
    facts: list[dict] = field(default_factory=list)
    conversation_summary: str = ""
    recent_messages: list[dict] = field(default_factory=list)
    user_preferences: dict = field(default_factory=dict)
    emotional_snapshot: dict = field(default_factory=dict)  # {dominant_emotion, intensity, pad_values}
    last_active: float = 0.0
    metadata: dict = field(default_factory=dict)


class CrossSessionManager:
    """跨会话记忆管理器"""

    # 加载策略阈值
    FULL_THRESHOLD_HOURS = 1
    LAST_N_THRESHOLD_HOURS = 24
    LAST_N_COUNT = 10

    def __init__(
        self,
        store: Any = None,
        hierarchical_mgr: Any = None,
    ) -> None:
        self._store = store
        self._hierarchical_mgr = hierarchical_mgr

    async def save_session(self, session_id: str, state: SessionState) -> None:
        """会话结束时序列化状态"""
        if not self._store:
            return
        try:
            entry = {
                "type": "session_state",
                "facts": state.facts,
                "conversation_summary": state.conversation_summary,
                "recent_messages": state.recent_messages[-self.LAST_N_COUNT:],
                "user_preferences": state.user_preferences,
                "emotional_snapshot": state.emotional_snapshot,
                "last_active": time.time(),
                "timestamp": int(time.time() * 1000),
            }
            await self._store.append_session_entry(session_id, entry)
            logger.debug(f"cross_session.save: {session_id}")
        except Exception as e:
            logger.warning(f"cross_session.save_failed: {e}")

    async def restore_session(self, session_id: str) -> SessionState | None:
        """根据离线时间选择加载策略"""
        if not self._store:
            return None

        try:
            entries = await self._store.load_session(session_id)
            if not entries:
                return self._cold_start()

            # 找最近的 session_state 条目
            last_active = 0
            latest_state_entry = None
            for entry in entries:
                if isinstance(entry, dict) and entry.get("type") == "session_state":
                    ts = entry.get("timestamp", 0)
                    if ts > last_active:
                        last_active = ts
                        latest_state_entry = entry

            if not latest_state_entry:
                # 没有 state 条目，用原始消息构建
                return await self._load_from_raw_entries(session_id, entries)

            offline_hours = self._calc_offline_hours(last_active)

            if offline_hours < self.FULL_THRESHOLD_HOURS:
                return await self._load_full(session_id, entries, latest_state_entry)
            elif offline_hours < self.LAST_N_THRESHOLD_HOURS:
                return await self._load_last_n(session_id, latest_state_entry)
            else:
                return await self._load_summary(session_id, latest_state_entry)

        except Exception as e:
            logger.warning(f"cross_session.restore_failed: {e}, using cold_start")
            return self._cold_start()

    def _calc_offline_hours(self, last_active_ms: float) -> float:
        """计算离线时间（小时）"""
        if last_active_ms <= 0:
            return float("inf")
        # last_active_ms 是毫秒时间戳
        diff_seconds = (time.time() * 1000 - last_active_ms) / 1000
        return diff_seconds / 3600

    async def _load_full(
        self, session_id: str, entries: list, state_entry: dict
    ) -> SessionState:
        """全量加载：无缝衔接"""
        return SessionState(
            facts=state_entry.get("facts", []),
            conversation_summary=state_entry.get("conversation_summary", ""),
            recent_messages=state_entry.get("recent_messages", []),
            user_preferences=state_entry.get("user_preferences", {}),
            emotional_snapshot=state_entry.get("emotional_snapshot", {}),
            last_active=state_entry.get("last_active", 0),
            metadata={"strategy": "full"},
        )

    async def _load_last_n(
        self, session_id: str, state_entry: dict
    ) -> SessionState:
        """最近N轮 + 摘要"""
        return SessionState(
            facts=state_entry.get("facts", []),
            conversation_summary=state_entry.get("conversation_summary", ""),
            recent_messages=state_entry.get("recent_messages", [])[-self.LAST_N_COUNT:],
            user_preferences=state_entry.get("user_preferences", {}),
            emotional_snapshot=state_entry.get("emotional_snapshot", {}),
            last_active=state_entry.get("last_active", 0),
            metadata={"strategy": "last_n"},
        )

    async def _load_summary(
        self, session_id: str, state_entry: dict
    ) -> SessionState:
        """仅摘要 + 关键事实 + 情感状态快照"""
        return SessionState(
            facts=state_entry.get("facts", []),
            conversation_summary=state_entry.get("conversation_summary", ""),
            recent_messages=[],  # 不加载消息
            user_preferences=state_entry.get("user_preferences", {}),
            emotional_snapshot=state_entry.get("emotional_snapshot", {}),
            last_active=state_entry.get("last_active", 0),
            metadata={"strategy": "summary"},
        )

    def _cold_start(self) -> SessionState:
        """冷启动：优雅降级"""
        return SessionState(
            facts=[],
            conversation_summary="",
            recent_messages=[],
            user_preferences={},
            emotional_snapshot={},
            last_active=0,
            metadata={"cold_start": True},
        )

    async def _load_from_raw_entries(
        self, session_id: str, entries: list
    ) -> SessionState:
        """从原始消息条目构建会话状态（无 state 快照时的 fallback）"""
        messages = []
        for entry in entries:
            if isinstance(entry, dict) and entry.get("type") != "session_state":
                messages.append(entry)

        return SessionState(
            facts=[],
            conversation_summary="",
            recent_messages=messages[-self.LAST_N_COUNT:],
            user_preferences={},
            emotional_snapshot={},
            last_active=messages[-1].get("timestamp", 0) if messages else 0,
            metadata={"strategy": "raw_fallback"},
        )

    def should_generate_greeting(self, state: SessionState) -> bool:
        """是否需要生成归巢问候"""
        strategy = state.metadata.get("strategy", "")
        return strategy == "summary" or state.metadata.get("cold_start", False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_cross_session.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent && git add memory/cross_session_manager.py tests/test_cross_session.py && git commit -m "feat(memory): #21 cross-session memory bridge with adaptive loading"
```

---

## Task 6: Temporal Reasoning Memory (#18)

**Files:**
- Create: `memory/temporal_memory.py`
- Test: `tests/test_temporal_memory.py`

**Interfaces:**
- Consumes: `aiosqlite.Connection` (from DatabaseManager), `_parse_temporal_query` (existing in memory_manager.py)
- Produces: `TemporalMemory`, `TemporalMemoryStore`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_temporal_memory.py
"""时间推理记忆 #18 测试"""
import pytest
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock


class TestTemporalMemory:
    def test_creation(self):
        from memory.temporal_memory import TemporalMemory
        now = datetime.now(timezone.utc)
        m = TemporalMemory(
            content="用户提到了工作压力",
            created_at=now,
            event_time=now,
            last_accessed=now,
            is_stable=False,
            half_life_hours=48.0,
        )
        assert m.is_stable is False
        assert m.half_life_hours == 48.0

    def test_stable_memory_no_decay(self):
        """稳定事实跳过衰减"""
        from memory.temporal_memory import TemporalMemory
        now = datetime.now(timezone.utc)
        m = TemporalMemory(
            content="用户名：张三",
            created_at=now,
            event_time=now,
            last_accessed=now,
            is_stable=True,
            half_life_hours=0,  # 无限
        )
        assert m.is_stable is True


class TestTemporalMemoryStore:
    @pytest.fixture
    def mock_conn(self):
        conn = AsyncMock()
        cursor = AsyncMock()
        cursor.fetchall.return_value = []
        conn.execute.return_value = cursor
        return conn

    async def test_query_range(self, mock_conn):
        """时间范围查询"""
        from memory.temporal_memory import TemporalMemoryStore
        store = TemporalMemoryStore(conn=mock_conn)
        start = datetime.now(timezone.utc) - timedelta(days=7)
        end = datetime.now(timezone.utc)
        results = await store.query_range(start, end)
        assert isinstance(results, list)
        mock_conn.execute.assert_called_once()

    async def test_query_as_of(self, mock_conn):
        """截至查询"""
        from memory.temporal_memory import TemporalMemoryStore
        store = TemporalMemoryStore(conn=mock_conn)
        as_of = datetime.now(timezone.utc) - timedelta(days=30)
        results = await store.query_as_of(as_of, "工作")
        assert isinstance(results, list)

    async def test_build_timeline(self, mock_conn):
        """时间线构建"""
        from memory.temporal_memory import TemporalMemoryStore
        store = TemporalMemoryStore(conn=mock_conn)
        # mock 返回一些记忆
        cursor = mock_conn.execute.return_value
        cursor.fetchall.return_value = [
            (1, "第一次聊天", time.time() - 86400 * 30, None, 0),
            (2, "第二次聊天", time.time() - 86400 * 15, None, 0),
            (3, "最近聊天", time.time() - 86400, None, 0),
        ]
        timeline = await store.build_timeline(topic="聊天")
        assert len(timeline) == 3
        # 验证按时间排序
        assert timeline[0].content == "第一次聊天"

    async def test_store_with_event_time(self, mock_conn):
        """存储带 event_time 的记忆"""
        from memory.temporal_memory import TemporalMemoryStore
        store = TemporalMemoryStore(conn=mock_conn)
        now = datetime.now(timezone.utc)
        await store.store(
            content="用户回忆了童年事件",
            created_at=now,
            event_time=now - timedelta(days=365 * 10),
            is_stable=True,
        )
        mock_conn.execute.assert_called()

    def test_half_life_config(self):
        """验证半衰期配置"""
        from memory.temporal_memory import HALF_LIFE_CONFIG
        assert HALF_LIFE_CONFIG["stable"] == 0  # 无限
        assert HALF_LIFE_CONFIG["emotional"] == 6
        assert HALF_LIFE_CONFIG["project"] == 48
        assert HALF_LIFE_CONFIG["general"] == 168
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_temporal_memory.py -v`
Expected: FAIL — `memory.temporal_memory` module not found

- [ ] **Step 3: Implement temporal_memory.py**

```python
# memory/temporal_memory.py
"""时间推理记忆 — AMT #18

四种查询模式：标准查询、时间范围查询、截至查询、时间线构建
关键设计：stable 事实跳过衰减，event_time 独立于 created_at
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from loguru import logger


# 半衰期配置（小时）
HALF_LIFE_CONFIG = {
    "stable": 0,       # 无限（跳过衰减）
    "emotional": 6,    # 情绪状态
    "project": 48,     # 当前项目/工作
    "general": 168,    # 一般对话（7天）
}


@dataclass
class TemporalMemory:
    """时间推理记忆条目"""
    content: str
    created_at: datetime
    event_time: datetime | None = None    # 事件发生时间（回忆过去时 != created_at）
    last_accessed: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_stable: bool = False                # 稳定事实跳过衰减
    half_life_hours: float = HALF_LIFE_CONFIG["general"]
    memory_id: int | None = None


class TemporalMemoryStore:
    """时间推理记忆存储"""

    def __init__(self, conn: Any = None) -> None:
        self._conn = conn

    async def store(
        self,
        content: str,
        created_at: datetime,
        event_time: datetime | None = None,
        is_stable: bool = False,
        memory_type: str = "general",
    ) -> TemporalMemory | None:
        """存储时间记忆"""
        if not self._conn:
            return None

        half_life = HALF_LIFE_CONFIG.get(memory_type, HALF_LIFE_CONFIG["general"])
        if is_stable:
            half_life = 0

        try:
            await self._conn.execute(
                "UPDATE episodic_memories SET event_time=?, is_stable=? "
                "WHERE summary=?",
                (
                    event_time.timestamp() if event_time else None,
                    1 if is_stable else 0,
                    content,
                ),
            )
            await self._conn.commit()
        except Exception as e:
            logger.warning(f"temporal.store_failed: {e}")

        return TemporalMemory(
            content=content,
            created_at=created_at,
            event_time=event_time,
            is_stable=is_stable,
            half_life_hours=half_life,
        )

    async def query(self, query: str, limit: int = 10) -> list[TemporalMemory]:
        """标准语义查询"""
        if not self._conn:
            return []
        try:
            cursor = await self._conn.execute(
                "SELECT id, summary, timestamp, event_time, is_stable "
                "FROM episodic_memories "
                "WHERE summary LIKE ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (f"%{query}%", limit),
            )
            rows = await cursor.fetchall()
            return [self._row_to_memory(row) for row in rows]
        except Exception as e:
            logger.warning(f"temporal.query_failed: {e}")
            return []

    async def query_range(
        self, start: datetime, end: datetime, limit: int = 50
    ) -> list[TemporalMemory]:
        """时间范围查询："上周三我们聊了什么" """
        if not self._conn:
            return []
        try:
            cursor = await self._conn.execute(
                "SELECT id, summary, timestamp, event_time, is_stable "
                "FROM episodic_memories "
                "WHERE timestamp BETWEEN ? AND ? "
                "ORDER BY timestamp ASC LIMIT ?",
                (start.timestamp(), end.timestamp(), limit),
            )
            rows = await cursor.fetchall()
            return [self._row_to_memory(row) for row in rows]
        except Exception as e:
            logger.warning(f"temporal.query_range_failed: {e}")
            return []

    async def query_as_of(
        self, as_of: datetime, topic: str, limit: int = 10
    ) -> list[TemporalMemory]:
        """截至查询："截至上个月用户住在哪里" """
        if not self._conn:
            return []
        try:
            cursor = await self._conn.execute(
                "SELECT id, summary, timestamp, event_time, is_stable "
                "FROM episodic_memories "
                "WHERE timestamp <= ? AND summary LIKE ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (as_of.timestamp(), f"%{topic}%", limit),
            )
            rows = await cursor.fetchall()
            return [self._row_to_memory(row) for row in rows]
        except Exception as e:
            logger.warning(f"temporal.query_as_of_failed: {e}")
            return []

    async def build_timeline(
        self, topic: str, limit: int = 100
    ) -> list[TemporalMemory]:
        """时间线构建："我们认识以来一起经历了什么" """
        if not self._conn:
            return []
        try:
            cursor = await self._conn.execute(
                "SELECT id, summary, timestamp, event_time, is_stable "
                "FROM episodic_memories "
                "WHERE summary LIKE ? "
                "ORDER BY timestamp ASC LIMIT ?",
                (f"%{topic}%", limit),
            )
            rows = await cursor.fetchall()
            timeline = [self._row_to_memory(row) for row in rows]
            logger.debug(f"temporal.timeline: {len(timeline)} items for '{topic}'")
            return timeline
        except Exception as e:
            logger.warning(f"temporal.build_timeline_failed: {e}")
            return []

    def _row_to_memory(self, row) -> TemporalMemory:
        """数据库行转 TemporalMemory"""
        memory_id = row[0]
        content = row[1]
        timestamp = row[2]
        event_time_val = row[3]
        is_stable = bool(row[4]) if row[4] else False

        created_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        event_time = None
        if event_time_val:
            event_time = datetime.fromtimestamp(event_time_val, tz=timezone.utc)

        half_life = 0 if is_stable else HALF_LIFE_CONFIG["general"]

        return TemporalMemory(
            content=content,
            created_at=created_at,
            event_time=event_time,
            is_stable=is_stable,
            half_life_hours=half_life,
            memory_id=memory_id,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_temporal_memory.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent && git add memory/temporal_memory.py tests/test_temporal_memory.py && git commit -m "feat(memory): #18 temporal reasoning memory with timeline and as-of queries"
```

---

## Task 7: Self-Reflection Memory (#16)

**Files:**
- Create: `memory/reflection_store.py`
- Modify: `core/meta_cognition.py` (add ReflectiveAgent)
- Test: `tests/test_reflection_store.py`

**Interfaces:**
- Consumes: `aiosqlite.Connection` (from DatabaseManager)
- Produces: `Reflection`, `ReflectionStore`, `ReflectiveAgent`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reflection_store.py
"""自反思记忆 #16 测试"""
import pytest
import time
from unittest.mock import AsyncMock


class TestReflection:
    def test_creation(self):
        from memory.reflection_store import Reflection
        r = Reflection(
            task_type="comfort",
            outcome="positive",
            insight="直接给建议不如先共情",
            context_hash="abc123",
            created_at=time.time(),
        )
        assert r.task_type == "comfort"
        assert r.outcome == "positive"
        assert "共情" in r.insight


class TestReflectionStore:
    @pytest.fixture
    def mock_conn(self):
        conn = AsyncMock()
        cursor = AsyncMock()
        cursor.fetchone.return_value = (1,)
        cursor.fetchall.return_value = []
        conn.execute.return_value = cursor
        return conn

    async def test_add_reflection(self, mock_conn):
        """添加反思"""
        from memory.reflection_store import ReflectionStore, Reflection
        store = ReflectionStore(conn=mock_conn)
        reflection = Reflection(
            task_type="comfort",
            outcome="positive",
            insight="先共情再给建议",
            context_hash="hash1",
            created_at=time.time(),
        )
        reflection_id = await store.add(reflection)
        assert reflection_id is not None
        mock_conn.execute.assert_called()

    async def test_query_by_scene(self, mock_conn):
        """按场景查询反思"""
        from memory.reflection_store import ReflectionStore
        store = ReflectionStore(conn=mock_conn)
        # mock 返回反思列表
        cursor = mock_conn.execute.return_value
        cursor.fetchall.return_value = [
            (1, "comfort", "positive", "先共情再给建议", "hash1", time.time()),
            (2, "comfort", "negative", "不要直接说不要伤心", "hash2", time.time()),
        ]
        reflections = await store.query_by_scene("comfort", "用户焦虑")
        assert len(reflections) == 2
        assert reflections[0].task_type == "comfort"

    async def test_get_effective_strategies(self, mock_conn):
        """获取有效策略"""
        from memory.reflection_store import ReflectionStore
        store = ReflectionStore(conn=mock_conn)
        cursor = mock_conn.execute.return_value
        cursor.fetchall.return_value = [
            (1, "comfort", "positive", "先共情再给建议", "hash1", time.time()),
        ]
        strategies = await store.get_effective_strategies("comfort")
        assert len(strategies) == 1
        assert "共情" in strategies[0]


class TestReflectiveAgent:
    async def test_pre_task_inject_returns_string(self):
        """pre_task_inject 返回字符串"""
        from core.meta_cognition import ReflectiveAgent
        mock_store = AsyncMock()
        mock_store.query_by_scene.return_value = []
        agent = ReflectiveAgent(reflection_store=mock_store)
        result = await agent.pre_task_inject("comfort", "用户焦虑")
        assert isinstance(result, str)

    async def test_pre_task_inject_with_reflections(self):
        """有反思时注入提示"""
        from core.meta_cognition import ReflectiveAgent
        from memory.reflection_store import Reflection
        mock_store = AsyncMock()
        mock_store.query_by_scene.return_value = [
            Reflection(
                task_type="comfort",
                outcome="positive",
                insight="先共情再给建议",
                context_hash="hash1",
                created_at=time.time(),
            ),
        ]
        agent = ReflectiveAgent(reflection_store=mock_store)
        result = await agent.pre_task_inject("comfort", "用户焦虑")
        assert "共情" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_reflection_store.py -v`
Expected: FAIL — `memory.reflection_store` and `core.meta_cognition.ReflectiveAgent` not found

- [ ] **Step 3: Implement reflection_store.py**

```python
# memory/reflection_store.py
"""反思存储 — AMT #16

按 task_type + outcome 索引的结构化反思存储
场景类型：comfort / topic_avoidance / emotion_transition / style
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger


@dataclass
class Reflection:
    """反思条目"""
    task_type: str           # 场景类型
    outcome: str             # positive/negative/neutral
    insight: str             # 可复用经验
    context_hash: str = ""   # 上下文指纹
    created_at: float = 0.0
    id: int | None = None


class ReflectionStore:
    """反思存储与检索"""

    def __init__(self, conn: Any = None) -> None:
        self._conn = conn

    async def add(self, reflection: Reflection) -> int | None:
        """添加反思"""
        if not self._conn:
            return None
        try:
            cursor = await self._conn.execute(
                "INSERT INTO reflection_store "
                "(task_type, outcome, insight, context_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    reflection.task_type,
                    reflection.outcome,
                    reflection.insight,
                    reflection.context_hash,
                    reflection.created_at or time.time(),
                ),
            )
            await self._conn.commit()
            return cursor.lastrowid
        except Exception as e:
            logger.warning(f"reflection.add_failed: {e}")
            return None

    async def query_by_scene(
        self, task_type: str, context: str, limit: int = 5
    ) -> list[Reflection]:
        """按场景类型查询反思"""
        if not self._conn:
            return []
        try:
            cursor = await self._conn.execute(
                "SELECT id, task_type, outcome, insight, context_hash, created_at "
                "FROM reflection_store "
                "WHERE task_type=? "
                "ORDER BY created_at DESC LIMIT ?",
                (task_type, limit),
            )
            rows = await cursor.fetchall()
            return [
                Reflection(
                    id=row[0],
                    task_type=row[1],
                    outcome=row[2],
                    insight=row[3],
                    context_hash=row[4],
                    created_at=row[5],
                )
                for row in rows
            ]
        except Exception as e:
            logger.warning(f"reflection.query_failed: {e}")
            return []

    async def get_effective_strategies(self, task_type: str) -> list[str]:
        """获取有效策略（outcome=positive 的 insight）"""
        if not self._conn:
            return []
        try:
            cursor = await self._conn.execute(
                "SELECT insight FROM reflection_store "
                "WHERE task_type=? AND outcome='positive' "
                "ORDER BY created_at DESC LIMIT 5",
                (task_type,),
            )
            rows = await cursor.fetchall()
            return [row[0] for row in rows]
        except Exception as e:
            logger.warning(f"reflection.get_effective_failed: {e}")
            return []

    @staticmethod
    def compute_context_hash(context: str) -> str:
        """计算上下文指纹"""
        return hashlib.sha256(context.encode()).hexdigest()[:16]
```

- [ ] **Step 4: Add ReflectiveAgent to meta_cognition.py**

Add this class to `core/meta_cognition.py` (after the `MetaCognition` class):

```python
class ReflectiveAgent:
    """反思循环：评估 → 反思 → 提取 → 存储 → 注入

    触发条件：
    - 每 5 轮对话触发一次自动反思
    - 用户明确表达满意/不满时立即触发
    - 检测到对话冷场时触发
    """

    REFLECTION_INTERVAL = 5  # 每 N 轮触发一次

    def __init__(self, reflection_store: Any = None) -> None:
        self._reflection_store = reflection_store
        self._turn_count = 0

    async def post_task_reflect(
        self,
        conversation_turn: list[dict],
        user_reaction: str,
    ) -> list:
        """任务后反思循环"""
        self._turn_count += 1
        if self._turn_count % self.REFLECTION_INTERVAL != 0:
            return []

        if not self._reflection_store:
            return []

        try:
            # 1. 评估结果
            outcome = self._evaluate(user_reaction)
            # 2. 分析对话
            task_type = self._classify_scene(conversation_turn)
            # 3. 提取经验
            insights = self._extract_insights(conversation_turn, outcome, task_type)

            # 4. 存储
            from memory.reflection_store import Reflection, ReflectionStore
            stored = []
            for insight in insights:
                reflection = Reflection(
                    task_type=task_type,
                    outcome=outcome,
                    insight=insight,
                    context_hash=ReflectionStore.compute_context_hash(
                        str(conversation_turn[-1].get("content", ""))
                    ) if conversation_turn else "",
                    created_at=__import__("time").time(),
                )
                rid = await self._reflection_store.add(reflection)
                if rid:
                    stored.append(reflection)
            return stored
        except Exception as e:
            __import__("loguru").logger.warning(f"reflective.post_task_failed: {e}")
            return []

    async def pre_task_inject(self, task_type: str, context: str) -> str:
        """任务前注入相关经验"""
        if not self._reflection_store:
            return ""

        try:
            reflections = await self._reflection_store.query_by_scene(task_type, context)
            if not reflections:
                return ""

            # 格式化为提示
            lines = []
            for r in reflections[:3]:  # 最多注入3条
                if r.outcome == "positive":
                    lines.append(f"- 有效策略：{r.insight}")
                elif r.outcome == "negative":
                    lines.append(f"- 避免策略：{r.insight}")
            if lines:
                return "经验参考：\n" + "\n".join(lines)
            return ""
        except Exception:
            return ""

    def _evaluate(self, user_reaction: str) -> str:
        """评估用户反应"""
        positive_indicators = ["谢谢", "好的", "嗯嗯", "理解", "有帮助", "好多了"]
        negative_indicators = ["不对", "不是", "烦", "不想", "算了", "..."]

        for indicator in positive_indicators:
            if indicator in user_reaction:
                return "positive"
        for indicator in negative_indicators:
            if indicator in user_reaction:
                return "negative"
        return "neutral"

    def _classify_scene(self, conversation_turn: list[dict]) -> str:
        """分类场景类型"""
        if not conversation_turn:
            return "general"
        last_msg = conversation_turn[-1].get("content", "")
        if any(kw in last_msg for kw in ["难过", "伤心", "焦虑", "压力", "害怕"]):
            return "comfort"
        if any(kw in last_msg for kw in ["不要", "别", "不想谈"]):
            return "topic_avoidance"
        return "general"

    def _extract_insights(
        self, conversation_turn: list[dict], outcome: str, task_type: str
    ) -> list[str]:
        """提取可复用经验（简化版，完整版用 LLM）"""
        insights = []
        if task_type == "comfort" and outcome == "positive":
            insights.append("先共情再给建议，用户更容易接受")
        elif task_type == "comfort" and outcome == "negative":
            insights.append("避免直接说不要伤心，先认可用户感受")
        return insights
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_reflection_store.py -v`
Expected: PASS

- [ ] **Step 6: Verify no regression**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m py_compile memory/reflection_store.py core/meta_cognition.py`
Expected: No errors

- [ ] **Step 7: Commit**

```bash
cd /home/orangepi/ai-agent && git add memory/reflection_store.py core/meta_cognition.py tests/test_reflection_store.py && git commit -m "feat(memory): #16 self-reflection memory with reflection store and reflective agent"
```

---

## Post-Implementation Verification

After all 7 tasks are complete, run full verification:

- [ ] **Step 1: Run all new tests**

```bash
cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_db_migration_v13.py tests/test_hierarchical_memory.py tests/test_working_memory.py tests/test_memory_router.py tests/test_cross_session.py tests/test_temporal_memory.py tests/test_reflection_store.py -v
```
Expected: All PASS

- [ ] **Step 2: Run existing tests for regression check**

```bash
cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_context_governance.py tests/test_fluid_memory.py tests/test_memory_distiller.py tests/test_emotional_memory.py -v
```
Expected: All PASS (no regression)

- [ ] **Step 3: Compile check all new/modified files**

```bash
cd /home/orangepi/ai-agent && .venv/bin/python -m py_compile memory/hierarchical_memory.py memory/memory_router.py memory/temporal_memory.py memory/cross_session_manager.py memory/reflection_store.py agent_context.py memory/episodic_limiter.py core/meta_cognition.py db/database.py
```
Expected: No errors

- [ ] **Step 4: Final commit**

```bash
cd /home/orangepi/ai-agent && git add -A && git commit -m "feat(memory): complete P0+P1 memory optimization (6 techniques, AMT coverage 47%→75%)"
```
