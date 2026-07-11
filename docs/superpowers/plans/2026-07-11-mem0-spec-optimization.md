# mem0 SPEC 记忆系统优化实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 基于 mem0 SPEC 对 xiaoda-agent 记忆系统进行对齐优化，新增实体链接机制（EntityExtractor + EntityStore）、ADD-only 混合架构、User/Session/Agent 三级 Scope 隔离、以及五路 RRF + Entity Boost 检索增强。

**Architecture:** 增量式集成——新建 3 个模块（scope, entity_extractor, entity_store），扩展 3 个现有模块（memory_manager, memory_distiller, db_memory）。通过 DB 迁移 v13 为 `episodic_memories` 新增 3 列（user_id/agent_id/is_raw）+ 新建 3 表（memory_entities, memory_entities_fts, entity_memory_links）+ 1 复合索引。所有新增组件都是可选注入，失败时降级到已有流程。

**Tech Stack:** Python 3.11, asyncio, aiosqlite, sqlite-vec, jieba, loguru, pytest

## Global Constraints

- Python 3.11 + asyncio + aiosqlite + sqlite-vec，虚拟环境 `.venv`
- 不引入 Neo4j 等重依赖，仅用 sqlite-vec + FTS5
- 所有时间相关函数使用 `ZoneInfo("Asia/Shanghai")`
- 保持与现有代码风格一致（loguru 日志，type hints，dataclass）
- 不破坏现有线上稳定性（全量测试 1311 passed 不回归）
- DB 迁移遵循现有 `schema_version` 机制（当前版本 12，本计划新增 v13）
  - 注：若 `2026-07-11-memory-optimization.md` 计划先执行（已占用 v13），则本计划迁移版本号需顺延为 v14
- `episodic_memories` 表已有 `session_id`/`entities`/`version`/`content_hash`/`rag_status` 字段，无需重复添加
- 测试用 pytest，`tests/conftest.py` 已配置 `temp_db_path` fixture
- 错误处理统一用 `try-except + logger.debug` 降级，不抛出阻断主流程
- `reciprocal_rank_fusion(ranked_lists, k=60, limit=10, weights=None)` 已存在（memory_manager.py:121）

**Spec:** `docs/superpowers/specs/2026-07-11-mem0-spec-optimization-design.md`

---

## File Structure

**新建文件：**

| 文件 | 职责 |
|------|------|
| `memory/scope.py` | Scope dataclass，三级隔离（user_id/session_id/agent_id）+ SQL 过滤生成 |
| `memory/entity_extractor.py` | EntityExtractor 混合实体提取（jieba+规则快抽 → LLM 精抽）+ Entity dataclass |
| `memory/entity_store.py` | EntityStore 实体存储管理（CRUD + 反向链接 + recall_by_entities） |
| `tests/test_scope_isolation.py` | Task 2 测试：Scope 过滤逻辑 |
| `tests/test_db_migration_v13.py` | Task 1 测试：Schema 迁移验证 |
| `tests/test_entity_crud.py` | Task 3 测试：实体 CRUD + 反向链接 |
| `tests/test_entity_extractor.py` | Task 4 测试：混合实体提取 |
| `tests/test_entity_store.py` | Task 5 测试：EntityStore 链接与反向查询 |
| `tests/test_add_only_architecture.py` | Task 6 测试：ADD-only 编码流程 |
| `tests/test_distill_merge.py` | Task 7 测试：蒸馏合并逻辑 |
| `tests/test_five_path_rrf_entity_boost.py` | Task 8 测试：五路 RRF + Entity Boost |
| `tests/test_temporal_scope_enhancement.py` | Task 9 测试：时间感知 + scope 过滤 |
| `tests/test_mem0_optimization_e2e.py` | Task 10 测试：端到端集成 |

**扩展文件：**

| 文件 | 扩展内容 |
|------|---------|
| `db/schema.sql` | episodic_memories 加 3 字段 + 新建 3 表 + 1 复合索引 |
| `db/database.py` | 新增 `_migrate_v13` 方法 + 注册到 migrations 列表 + CURRENT_SCHEMA_VERSION → 13 |
| `db/db_memory.py` | 新增 scope 过滤方法 + entity 相关 CRUD + insert_episodic_memory 扩展 is_raw/user_id/agent_id |
| `memory/memory_manager.py` | encode_memory 接入 ADD-only + 实体提取；retrieve_memories_hybrid 接入第5路 + Entity Boost；_has_duplicate 改为只对 is_raw=0 生效 |
| `memory/memory_distiller.py` | 新增 `merge_knowledge()` 方法 |

---

## Task 1: Schema 迁移 — 新增 3 字段 + 3 表 + 索引

**Files:**
- Modify: `db/schema.sql:37-56` (episodic_memories 表定义加 3 字段)
- Modify: `db/schema.sql` (末尾新增 3 表 + 索引)
- Modify: `db/database.py:24` (CURRENT_SCHEMA_VERSION = 13)
- Modify: `db/database.py:195-208` (migrations 列表注册 v13)
- Modify: `db/database.py` (新增 `_migrate_v13` 方法，在 `_migrate_v12` 之后)
- Modify: `db/database.py:876-912` (_create_indexes 新增复合索引)
- Test: `tests/test_db_migration_v13.py`

**Interfaces:**
- Produces: `episodic_memories` 表新增 `user_id TEXT DEFAULT 'default'`, `agent_id TEXT DEFAULT 'xiaoda'`, `is_raw INTEGER DEFAULT 0` 列；新建 `memory_entities` 表（id/name/entity_type/kind/observations/memory_count/first_seen/last_seen/metadata_json + UNIQUE(name, entity_type)）；新建 `memory_entities_fts` 虚拟表 + 3 触发器；新建 `entity_memory_links` 表（entity_id/memory_id/confidence/created_at + UNIQUE(entity_id, memory_id)）；新建 `idx_episodic_scope` 复合索引

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_migration_v13.py
"""DB Migration v13 测试：验证 mem0 SPEC 优化新增的列、表、索引存在"""
import pytest
import aiosqlite
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
    """验证 v13 迁移：episodic_memories 新增3字段 + 3新表 + 索引"""

    async def test_episodic_memories_has_user_id_column(self, migrated_db):
        """验证 episodic_memories 表有 user_id 列，默认 'default'"""
        cursor = await migrated_db._conn.execute("PRAGMA table_info(episodic_memories)")
        columns = [row[1] for row in await cursor.fetchall()]
        assert "user_id" in columns

    async def test_episodic_memories_has_agent_id_column(self, migrated_db):
        """验证 episodic_memories 表有 agent_id 列，默认 'xiaoda'"""
        cursor = await migrated_db._conn.execute("PRAGMA table_info(episodic_memories)")
        columns = [row[1] for row in await cursor.fetchall()]
        assert "agent_id" in columns

    async def test_episodic_memories_has_is_raw_column(self, migrated_db):
        """验证 episodic_memories 表有 is_raw 列，默认 0"""
        cursor = await migrated_db._conn.execute("PRAGMA table_info(episodic_memories)")
        columns = [row[1] for row in await cursor.fetchall()]
        assert "is_raw" in columns

    async def test_memory_entities_table_exists(self, migrated_db):
        """验证 memory_entities 表存在且有正确字段"""
        cursor = await migrated_db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_entities'"
        )
        row = await cursor.fetchone()
        assert row is not None
        # 验证字段
        cursor = await migrated_db._conn.execute("PRAGMA table_info(memory_entities)")
        cols = {row[1]: row for row in await cursor.fetchall()}
        assert "name" in cols
        assert "entity_type" in cols
        assert "kind" in cols
        assert "observations" in cols
        assert "memory_count" in cols
        assert "first_seen" in cols
        assert "last_seen" in cols
        assert "metadata_json" in cols

    async def test_memory_entities_fts_table_exists(self, migrated_db):
        """验证 memory_entities_fts 虚拟表存在"""
        cursor = await migrated_db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_entities_fts'"
        )
        row = await cursor.fetchone()
        assert row is not None

    async def test_entity_memory_links_table_exists(self, migrated_db):
        """验证 entity_memory_links 表存在且有正确字段"""
        cursor = await migrated_db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='entity_memory_links'"
        )
        row = await cursor.fetchone()
        assert row is not None
        cursor = await migrated_db._conn.execute("PRAGMA table_info(entity_memory_links)")
        cols = {row[1]: row for row in await cursor.fetchall()}
        assert "entity_id" in cols
        assert "memory_id" in cols
        assert "confidence" in cols
        assert "created_at" in cols

    async def test_episodic_scope_index_exists(self, migrated_db):
        """验证 idx_episodic_scope 复合索引存在"""
        cursor = await migrated_db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_episodic_scope'"
        )
        row = await cursor.fetchone()
        assert row is not None

    async def test_schema_version_is_13(self, migrated_db):
        """验证 schema_version 表记录了 v13"""
        cursor = await migrated_db._conn.execute(
            "SELECT MAX(version) FROM schema_version"
        )
        row = await cursor.fetchone()
        assert row[0] >= 13

    async def test_existing_memories_backfill_is_raw(self, migrated_db):
        """验证现有记忆被回填 is_raw=0, user_id='default', agent_id='xiaoda'"""
        # 先插入一条记忆（模拟旧数据，不传新字段）
        import time
        await migrated_db._conn.execute(
            "INSERT INTO episodic_memories (timestamp, summary) VALUES (?, ?)",
            (time.time(), "测试旧记忆"),
        )
        await migrated_db._conn.commit()
        # 查询验证默认值
        cursor = await migrated_db._conn.execute(
            "SELECT user_id, agent_id, is_raw FROM episodic_memories WHERE summary='测试旧记忆'"
        )
        row = await cursor.fetchone()
        assert row["user_id"] == "default"
        assert row["agent_id"] == "xiaoda"
        assert row["is_raw"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_db_migration_v13.py -v`
Expected: FAIL with "column not found: user_id" or "no such table: memory_entities"

- [ ] **Step 3: Write minimal implementation**

修改 `db/schema.sql`，在 episodic_memories 表定义中（第 37-56 行后）追加 3 字段：

```sql
-- 在 episodic_memories 表的 version 列之后追加（schema.sql 第55行后）
    user_id TEXT DEFAULT 'default',
    agent_id TEXT DEFAULT 'xiaoda',
    is_raw INTEGER DEFAULT 0
```

在 `db/schema.sql` 末尾（第 403 行后）追加新表和索引：

```sql
-- ============================================================
-- mem0 SPEC 优化 (v13): 实体链接 + Scope 隔离
-- ============================================================

-- 实体存储表（与 KG 的 knowledge_entities 分离，职责不同）
CREATE TABLE IF NOT EXISTS memory_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    entity_type TEXT DEFAULT 'TOPIC',
    kind TEXT DEFAULT '',
    observations TEXT DEFAULT '[]',
    memory_count INTEGER DEFAULT 0,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    metadata_json TEXT DEFAULT '{}',
    UNIQUE(name, entity_type)
);
CREATE INDEX IF NOT EXISTS idx_memory_entities_name ON memory_entities(name);
CREATE INDEX IF NOT EXISTS idx_memory_entities_type ON memory_entities(entity_type);

-- 实体名称全文索引（用于快速名称匹配）
CREATE VIRTUAL TABLE IF NOT EXISTS memory_entities_fts USING fts5(
    id UNINDEXED, name_index
);

CREATE TRIGGER IF NOT EXISTS memory_entities_fts_ai AFTER INSERT ON memory_entities BEGIN
    INSERT INTO memory_entities_fts(id, name_index) VALUES (new.id, new.name);
END;
CREATE TRIGGER IF NOT EXISTS memory_entities_fts_ad AFTER DELETE ON memory_entities BEGIN
    INSERT INTO memory_entities_fts(memory_entities_fts, id, name_index)
    VALUES ('delete', old.id, old.name);
END;
CREATE TRIGGER IF NOT EXISTS memory_entities_fts_au AFTER UPDATE ON memory_entities BEGIN
    INSERT INTO memory_entities_fts(memory_entities_fts, id, name_index)
    VALUES ('delete', old.id, old.name);
    INSERT INTO memory_entities_fts(id, name_index) VALUES (new.id, new.name);
END;

-- 实体↔记忆反向链接表
CREATE TABLE IF NOT EXISTS entity_memory_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER NOT NULL,
    memory_id INTEGER NOT NULL,
    confidence REAL DEFAULT 1.0,
    created_at REAL NOT NULL,
    FOREIGN KEY (entity_id) REFERENCES memory_entities(id) ON DELETE CASCADE,
    FOREIGN KEY (memory_id) REFERENCES episodic_memories(id) ON DELETE CASCADE,
    UNIQUE(entity_id, memory_id)
);
CREATE INDEX IF NOT EXISTS idx_eml_entity ON entity_memory_links(entity_id);
CREATE INDEX IF NOT EXISTS idx_eml_memory ON entity_memory_links(memory_id);

-- episodic_memories scope 复合索引
CREATE INDEX IF NOT EXISTS idx_episodic_scope
    ON episodic_memories(user_id, agent_id, is_raw, timestamp DESC);
```

修改 `db/database.py:24`，更新版本号：

```python
CURRENT_SCHEMA_VERSION = 13
```

修改 `db/database.py:195-208`，在 migrations 列表末尾追加 v13：

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
            (13, "mem0_spec:user_id+agent_id+is_raw+memory_entities+entity_memory_links", self._migrate_v13),
        ]
```

在 `db/database.py` 的 `_migrate_v12` 方法之后（约第 460 行后）新增 `_migrate_v13` 方法：

```python
    async def _migrate_v13(self) -> None:
        """v13: mem0 SPEC 优化 — episodic_memories 新增 user_id/agent_id/is_raw + 实体链接三表。

        - episodic_memories: user_id/agent_id/is_raw（ALTER TABLE 加列，SQLite 不锁表）
        - memory_entities: 实体存储（与 KG 的 knowledge_entities 职责分离）
        - memory_entities_fts: 实体名称全文索引 + 3 触发器
        - entity_memory_links: 实体↔记忆反向链接
        - idx_episodic_scope: scope 复合索引
        - 回填现有记忆的 user_id/agent_id/is_raw 默认值
        """
        # 1. episodic_memories 新增 3 列（幂等：先检查列是否存在）
        cols = [r["name"] for r in await self.fetch_all("PRAGMA table_info(episodic_memories)")]
        if "user_id" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN user_id TEXT DEFAULT 'default'"
            )
        if "agent_id" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN agent_id TEXT DEFAULT 'xiaoda'"
            )
        if "is_raw" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN is_raw INTEGER DEFAULT 0"
            )

        # 2. 回填现有记忆的默认值（确保旧数据有 scope 字段）
        await self._conn.execute(
            "UPDATE episodic_memories SET user_id='default' WHERE user_id IS NULL OR user_id=''"
        )
        await self._conn.execute(
            "UPDATE episodic_memories SET agent_id='xiaoda' WHERE agent_id IS NULL OR agent_id=''"
        )
        await self._conn.execute(
            "UPDATE episodic_memories SET is_raw=0 WHERE is_raw IS NULL"
        )

        # 3. 新建 memory_entities 表
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                entity_type TEXT DEFAULT 'TOPIC',
                kind TEXT DEFAULT '',
                observations TEXT DEFAULT '[]',
                memory_count INTEGER DEFAULT 0,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                metadata_json TEXT DEFAULT '{}',
                UNIQUE(name, entity_type)
            );
            CREATE INDEX IF NOT EXISTS idx_memory_entities_name ON memory_entities(name);
            CREATE INDEX IF NOT EXISTS idx_memory_entities_type ON memory_entities(entity_type);
        """)

        # 4. 新建 memory_entities_fts 虚拟表 + 触发器
        await self._conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_entities_fts USING fts5(
                id UNINDEXED, name_index
            );
            CREATE TRIGGER IF NOT EXISTS memory_entities_fts_ai AFTER INSERT ON memory_entities BEGIN
                INSERT INTO memory_entities_fts(id, name_index) VALUES (new.id, new.name);
            END;
            CREATE TRIGGER IF NOT EXISTS memory_entities_fts_ad AFTER DELETE ON memory_entities BEGIN
                INSERT INTO memory_entities_fts(memory_entities_fts, id, name_index)
                VALUES ('delete', old.id, old.name);
            END;
            CREATE TRIGGER IF NOT EXISTS memory_entities_fts_au AFTER UPDATE ON memory_entities BEGIN
                INSERT INTO memory_entities_fts(memory_entities_fts, id, name_index)
                VALUES ('delete', old.id, old.name);
                INSERT INTO memory_entities_fts(id, name_index) VALUES (new.id, new.name);
            END;
        """)

        # 5. 新建 entity_memory_links 表
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS entity_memory_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id INTEGER NOT NULL,
                memory_id INTEGER NOT NULL,
                confidence REAL DEFAULT 1.0,
                created_at REAL NOT NULL,
                FOREIGN KEY (entity_id) REFERENCES memory_entities(id) ON DELETE CASCADE,
                FOREIGN KEY (memory_id) REFERENCES episodic_memories(id) ON DELETE CASCADE,
                UNIQUE(entity_id, memory_id)
            );
            CREATE INDEX IF NOT EXISTS idx_eml_entity ON entity_memory_links(entity_id);
            CREATE INDEX IF NOT EXISTS idx_eml_memory ON entity_memory_links(memory_id);
        """)

        # 6. scope 复合索引
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_episodic_scope "
            "ON episodic_memories(user_id, agent_id, is_raw, timestamp DESC)"
        )

        logger.info("database.migration_v13_mem0_spec_done")
```

修改 `db/database.py:876-912` 的 `_create_indexes` 方法，在 executescript 末尾追加：

```python
            CREATE INDEX IF NOT EXISTS idx_episodic_scope
                ON episodic_memories(user_id, agent_id, is_raw, timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_memory_entities_name ON memory_entities(name);
            CREATE INDEX IF NOT EXISTS idx_memory_entities_type ON memory_entities(entity_type);
            CREATE INDEX IF NOT EXISTS idx_eml_entity ON entity_memory_links(entity_id);
            CREATE INDEX IF NOT EXISTS idx_eml_memory ON entity_memory_links(memory_id);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_db_migration_v13.py -v`
Expected: PASS (9 tests passed)

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add db/schema.sql db/database.py tests/test_db_migration_v13.py
git commit -m "feat(db): v13 migration — mem0 SPEC schema (user_id/agent_id/is_raw + entity tables)"
```

---

## Task 2: Scope 对象 + 数据迁移脚本

**Files:**
- Create: `memory/scope.py`
- Modify: `db/db_memory.py:28-55` (insert_episodic_memory 扩展 scope 参数)
- Modify: `db/db_memory.py` (新增 scope 过滤的检索方法)
- Test: `tests/test_scope_isolation.py`

**Interfaces:**
- Consumes: Task 1 的 `episodic_memories.user_id/agent_id/is_raw` 列
- Produces: `Scope` dataclass（`user_id: str = "default"`, `session_id: str = "user"`, `agent_id: str = "xiaoda"`）；`Scope.to_sql_filter(table)` → str；`Scope.to_sql_params()` → list；`MemoryDB.insert_episodic_memory(..., scope: Scope | None = None, is_raw: int = 0)`；`MemoryDB.search_memories_fts_scoped(query, scope, limit)`；`MemoryDB.search_memories_by_time_scoped(start_ts, end_ts, scope, limit)`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scope_isolation.py
"""Scope 三级隔离测试：user_id/session_id/agent_id 过滤逻辑"""
import asyncio
import time
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.scope import Scope


class TestScopeDataclass:
    """Scope dataclass 基础功能"""

    def test_default_scope(self):
        """默认 scope: user='default', session='user', agent='xiaoda'"""
        scope = Scope()
        assert scope.user_id == "default"
        assert scope.session_id == "user"
        assert scope.agent_id == "xiaoda"

    def test_custom_scope(self):
        """自定义 scope"""
        scope = Scope(user_id="alice", session_id="sess-123", agent_id="xiaoli")
        assert scope.user_id == "alice"
        assert scope.session_id == "sess-123"
        assert scope.agent_id == "xiaoli"

    def test_to_sql_filter_default_table(self):
        """SQL WHERE 子句生成（默认表名 episodic_memories）"""
        scope = Scope(user_id="alice", agent_id="xiaoli")
        where = scope.to_sql_filter()
        assert "episodic_memories.user_id" in where
        assert "episodic_memories.agent_id" in where
        assert "alice" in where
        assert "xiaoli" in where

    def test_to_sql_filter_custom_table(self):
        """SQL WHERE 子句生成（自定义表名）"""
        scope = Scope(user_id="bob", agent_id="xiaoke")
        where = scope.to_sql_filter(table="em")
        assert "em.user_id" in where
        assert "em.agent_id" in where

    def test_to_sql_params(self):
        """参数化 SQL 返回参数列表"""
        scope = Scope(user_id="alice", agent_id="xiaoli")
        params = scope.to_sql_params()
        assert "alice" in params
        assert "xiaoli" in params
        assert len(params) == 2


class TestScopeDBIntegration:
    """Scope 与 DB 集成：验证 scope 过滤的检索"""

    @pytest.fixture
    async def scoped_db(self, tmp_path):
        """创建带 scope 数据的测试数据库"""
        from db.database import DatabaseManager
        db_path = tmp_path / "test_scope.db"
        db = DatabaseManager(db_path)
        await db.init()
        # 插入不同 scope 的记忆
        import time
        now = time.time()
        # alice + xiaoli 的记忆
        await db._conn.execute(
            "INSERT INTO episodic_memories (timestamp, summary, user_id, agent_id, is_raw) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, "alice的记忆", "alice", "xiaoli", 0),
        )
        # bob + xiaoke 的记忆
        await db._conn.execute(
            "INSERT INTO episodic_memories (timestamp, summary, user_id, agent_id, is_raw) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, "bob的记忆", "bob", "xiaoke", 0),
        )
        # default + xiaoda 的记忆
        await db._conn.execute(
            "INSERT INTO episodic_memories (timestamp, summary, user_id, agent_id, is_raw) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, "default的记忆", "default", "xiaoda", 0),
        )
        await db._conn.commit()
        yield db
        await db.close()

    async def test_search_scoped_alice(self, scoped_db):
        """alice scope 只查到 alice 的记忆"""
        scope = Scope(user_id="alice", agent_id="xiaoli")
        results = await scoped_db.memory.search_memories_fts_scoped(
            "记忆", scope=scope, limit=10
        )
        assert len(results) == 1
        assert results[0]["summary"] == "alice的记忆"

    async def test_search_scoped_bob(self, scoped_db):
        """bob scope 只查到 bob 的记忆"""
        scope = Scope(user_id="bob", agent_id="xiaoke")
        results = await scoped_db.memory.search_memories_fts_scoped(
            "记忆", scope=scope, limit=10
        )
        assert len(results) == 1
        assert results[0]["summary"] == "bob的记忆"

    async def test_search_scoped_default(self, scoped_db):
        """default scope 只查到 default 的记忆"""
        scope = Scope()
        results = await scoped_db.memory.search_memories_fts_scoped(
            "记忆", scope=scope, limit=10
        )
        assert len(results) == 1
        assert results[0]["summary"] == "default的记忆"

    async def test_insert_with_scope(self, tmp_path):
        """通过 insert_episodic_memory 传入 scope，验证字段写入正确"""
        from db.database import DatabaseManager
        db_path = tmp_path / "test_insert_scope.db"
        db = DatabaseManager(db_path)
        await db.init()
        scope = Scope(user_id="charlie", session_id="sess-456", agent_id="xiaolian")
        mem_id = await db.memory.insert_episodic_memory(
            summary="charlie的新记忆", scope=scope
        )
        # 查询验证
        mem = await db.memory.get_memory_by_id(mem_id)
        assert mem["user_id"] == "charlie"
        assert mem["session_id"] == "sess-456"
        assert mem["agent_id"] == "xiaolian"
        assert mem["is_raw"] == 0  # 默认 is_raw=0
        await db.close()

    async def test_insert_raw_with_scope(self, tmp_path):
        """插入 is_raw=1 的原始记忆"""
        from db.database import DatabaseManager
        db_path = tmp_path / "test_insert_raw.db"
        db = DatabaseManager(db_path)
        await db.init()
        scope = Scope(user_id="charlie", agent_id="xiaolian")
        mem_id = await db.memory.insert_episodic_memory(
            summary="原始记录", scope=scope, is_raw=1
        )
        mem = await db.memory.get_memory_by_id(mem_id)
        assert mem["is_raw"] == 1
        assert mem["user_id"] == "charlie"
        await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_scope_isolation.py -v`
Expected: FAIL with "No module named 'memory.scope'"

- [ ] **Step 3: Write minimal implementation**

创建 `memory/scope.py`：

```python
"""Scope 三级隔离 — user_id/session_id/agent_id 作用域控制。

用于 mem0 SPEC 优化的记忆隔离：
- user_id: 用户标识（默认 'default'，单用户桌面应用）
- session_id: 会话标识（复用已有字段，会话级隔离）
- agent_id: Agent 标识（xiaoda/xiaoli/xiaolian/xiaoke）
"""
from dataclasses import dataclass


@dataclass
class Scope:
    """记忆隔离的三级 scope。

    默认值对应单用户桌面应用场景：
    - user_id='default': 单用户
    - session_id='user': 默认会话
    - agent_id='xiaoda': 默认 agent
    """
    user_id: str = "default"
    session_id: str = "user"
    agent_id: str = "xiaoda"

    def to_sql_filter(self, table: str = "episodic_memories") -> str:
        """生成 SQL WHERE 子句（user_id + agent_id 过滤）。

        注意：默认不含 session_id 过滤（跨会话检索是最常见场景）。
        session_id 过滤由调用方通过 session_only=True 参数触发。

        Args:
            table: 表名前缀，默认 'episodic_memories'

        Returns:
            SQL WHERE 子句字符串，如 "episodic_memories.user_id = 'default' AND episodic_memories.agent_id = 'xiaoda'"
        """
        return (
            f"{table}.user_id = '{self.user_id}' "
            f"AND {table}.agent_id = '{self.agent_id}'"
        )

    def to_sql_params(self) -> list[str]:
        """返回参数化 SQL 的参数列表（用于 WHERE ... AND ... 占位符）。

        Returns:
            [user_id, agent_id]
        """
        return [self.user_id, self.agent_id]

    def to_sql_filter_parametrized(self, table: str = "episodic_memories") -> tuple[str, list[str]]:
        """生成参数化 SQL WHERE 子句（防注入）。

        Returns:
            (where_clause, params) 如 ("em.user_id = ? AND em.agent_id = ?", ["default", "xiaoda"])
        """
        where = f"{table}.user_id = ? AND {table}.agent_id = ?"
        return where, [self.user_id, self.agent_id]
```

修改 `db/db_memory.py:28-55` 的 `insert_episodic_memory` 方法，扩展 scope 参数：

```python
    async def insert_episodic_memory(self, summary: str, importance: float = 0.5,
                                      emotion_label: str = "", session_id: str = "user",
                                      embedding_id: int = -1, auto_commit: bool = True,
                                      source: str = "user",
                                      scope: Any | None = None,
                                      is_raw: int = 0) -> Any:
        """插入情景记忆。

        Args:
            scope: Scope 对象（mem0 SPEC 优化）。传入时使用 scope 的 user_id/session_id/agent_id。
            is_raw: 0=提炼知识（允许 UPDATE/DELETE），1=原始记录（append-only）。
        """
        # scope 优先级高于单独的 session_id 参数
        if scope is not None:
            user_id = scope.user_id
            agent_id = scope.agent_id
            session_id = scope.session_id
        else:
            user_id = "default"
            agent_id = "xiaoda"
        cursor = await self._conn.execute(
            """INSERT INTO episodic_memories
               (timestamp, summary, importance, emotion_label, session_id,
                embedding_id, source, user_id, agent_id, is_raw)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), summary, importance, emotion_label, session_id,
             embedding_id, source, user_id, agent_id, is_raw),
        )
        mem_id = cursor.lastrowid
        if auto_commit:
            await self._conn.commit()
        # 同步写入 FTS 索引
        try:
            from db.fts_utils import _tokenize_for_fts
            tokenized = _tokenize_for_fts(summary)
            if tokenized.strip():
                await self._conn.execute(
                    "INSERT INTO episodic_memory_fts(id, summary_index) VALUES(?, ?)",
                    (mem_id, tokenized),
                )
                if auto_commit:
                    await self._conn.commit()
        except Exception as e:
            from loguru import logger
            logger.debug("db_memory.fts_insert_failed", error=str(e))
        return mem_id
```

在 `db/db_memory.py` 中（`search_memories_fts` 方法之后，约第 183 行）新增 scope 过滤的检索方法：

```python
    async def search_memories_fts_scoped(self, query: str, scope: Any,
                                          limit: int = 10,
                                          is_raw: int | None = None) -> list[dict]:
        """FTS5 全文检索 + scope 过滤（mem0 SPEC 优化）。

        Args:
            scope: Scope 对象
            limit: 返回条数上限
            is_raw: None=不限, 0=只查提炼知识, 1=只查原始记录
        """
        from db.fts_utils import _build_fts_query
        fts_query = _build_fts_query(query)
        if not fts_query:
            return []
        try:
            where_extra = ""
            params: list = [fts_query, scope.user_id, scope.agent_id]
            if is_raw is not None:
                where_extra = " AND em.is_raw = ?"
                params.append(is_raw)
            params.append(limit)
            cursor = await self._conn.execute(
                f"""SELECT em.*, bm25(episodic_memory_fts) AS score
                   FROM episodic_memory_fts
                   JOIN episodic_memories em ON em.id = episodic_memory_fts.id
                   WHERE episodic_memory_fts MATCH ?
                     AND em.user_id = ? AND em.agent_id = ?{where_extra}
                   ORDER BY score ASC, em.importance DESC, em.timestamp DESC
                   LIMIT ?""",
                params,
            )
            rows = await cursor.fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d["score"] = -d.get("score", 0)
                results.append(d)
            return results
        except Exception as e:
            logger.warning("db_memory.fts_scoped_search_failed", error=str(e))
            return []

    async def search_memories_by_time_scoped(self, start_ts: float, end_ts: float,
                                              scope: Any, limit: int = 20,
                                              is_raw: int | None = None) -> list[dict]:
        """按时间范围检索记忆 + scope 过滤（mem0 SPEC 优化）。

        Args:
            scope: Scope 对象
            is_raw: None=不限, 0=只查提炼知识, 1=只查原始记录
        """
        try:
            where_extra = ""
            params: list = [start_ts, end_ts, scope.user_id, scope.agent_id]
            if is_raw is not None:
                where_extra = " AND is_raw = ?"
                params.append(is_raw)
            params.append(limit)
            cursor = await self._conn.execute(
                f"""SELECT * FROM episodic_memories
                   WHERE timestamp >= ? AND timestamp < ?
                     AND user_id = ? AND agent_id = ?{where_extra}
                   ORDER BY timestamp DESC LIMIT ?""",
                params,
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("db_memory.time_scoped_search_failed", error=str(e))
            return []

    async def search_memories_vec_scoped(self, memory_ids: list[int], scope: Any,
                                          limit: int = 50,
                                          is_raw: int | None = None) -> list[dict]:
        """向量检索结果 + scope 过滤（从 memory_ids 中筛选符合 scope 的记录）。

        Args:
            memory_ids: 向量检索返回的 memory_id 列表
            scope: Scope 对象
            is_raw: None=不限, 0=只查提炼知识, 1=只查原始记录
        """
        if not memory_ids:
            return []
        try:
            placeholders = ",".join("?" * len(memory_ids))
            where_extra = ""
            params: list = list(memory_ids) + [scope.user_id, scope.agent_id]
            if is_raw is not None:
                where_extra = " AND is_raw = ?"
                params.append(is_raw)
            params.append(limit)
            cursor = await self._conn.execute(
                f"""SELECT * FROM episodic_memories
                   WHERE id IN ({placeholders})
                     AND user_id = ? AND agent_id = ?{where_extra}
                   LIMIT ?""",
                params,
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("db_memory.vec_scoped_search_failed", error=str(e))
            return []

    async def get_episodic_count_scoped(self, scope: Any, is_raw: int | None = None) -> int:
        """获取 scope 内的记忆总数（用于冷启动档位判断）"""
        try:
            where_extra = ""
            params: list = [scope.user_id, scope.agent_id]
            if is_raw is not None:
                where_extra = " AND is_raw = ?"
                params.append(is_raw)
            cursor = await self._conn.execute(
                f"SELECT COUNT(*) as cnt FROM episodic_memories "
                f"WHERE user_id = ? AND agent_id = ?{where_extra}",
                params,
            )
            row = await cursor.fetchone()
            return row["cnt"] if row else 0
        except Exception as e:
            logger.warning("db_memory.count_scoped_failed", error=str(e))
            return 0
```

在 `db/db_memory.py:1` 的导入区域添加 `Any` 类型导入（如未导入）：

```python
from typing import Any
```

（注：`Any` 已在第 1 行导入，无需重复添加）

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_scope_isolation.py -v`
Expected: PASS (10 tests passed)

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add memory/scope.py db/db_memory.py tests/test_scope_isolation.py
git commit -m "feat(scope): add Scope dataclass + scoped DB search methods"
```

---

## Task 3: db_memory.py 新增实体相关 CRUD

**Files:**
- Modify: `db/db_memory.py` (新增 memory_entities 表 CRUD + entity_memory_links 表 CRUD)
- Test: `tests/test_entity_crud.py`

**Interfaces:**
- Consumes: Task 1 的 `memory_entities` 表 + `entity_memory_links` 表
- Produces: `MemoryDB.insert_memory_entity(name, entity_type, kind, ...) -> int`；`MemoryDB.find_memory_entity_by_name(name, entity_type) -> dict | None`；`MemoryDB.update_memory_entity(entity_id, ...) -> bool`；`MemoryDB.increment_entity_memory_count(entity_id) -> None`；`MemoryDB.update_entity_last_seen(entity_id, ts) -> None`；`MemoryDB.insert_entity_memory_link(entity_id, memory_id, confidence) -> int | None`；`MemoryDB.get_entity_memory_links(entity_id) -> list[dict]`；`MemoryDB.get_memories_by_entity_names_scoped(entity_names, scope, limit) -> list[dict]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_entity_crud.py
"""memory_entities + entity_memory_links 表 CRUD 测试"""
import asyncio
import time
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
async def entity_db(tmp_path):
    """创建带 v13 schema 的测试数据库"""
    from db.database import DatabaseManager
    db_path = tmp_path / "test_entity.db"
    db = DatabaseManager(db_path)
    await db.init()
    yield db
    await db.close()


class TestMemoryEntityCRUD:
    """memory_entities 表 CRUD"""

    async def test_insert_entity(self, entity_db):
        """插入实体，返回 id"""
        entity_id = await entity_db.memory.insert_memory_entity(
            name="Python", entity_type="IDENTIFIER", kind="技术"
        )
        assert entity_id > 0

    async def test_insert_duplicate_returns_none(self, entity_db):
        """重复插入相同 (name, entity_type) 返回 None"""
        await entity_db.memory.insert_memory_entity(
            name="Python", entity_type="IDENTIFIER", kind="技术"
        )
        result = await entity_db.memory.insert_memory_entity(
            name="Python", entity_type="IDENTIFIER", kind="技术"
        )
        assert result is None

    async def test_find_by_name(self, entity_db):
        """按名称+类型查找实体"""
        await entity_db.memory.insert_memory_entity(
            name="张三", entity_type="PROPER", kind="人物"
        )
        entity = await entity_db.memory.find_memory_entity_by_name("张三", "PROPER")
        assert entity is not None
        assert entity["name"] == "张三"
        assert entity["entity_type"] == "PROPER"
        assert entity["kind"] == "人物"

    async def test_find_by_name_not_found(self, entity_db):
        """查找不存在的实体返回 None"""
        entity = await entity_db.memory.find_memory_entity_by_name("不存在", "TOPIC")
        assert entity is None

    async def test_increment_memory_count(self, entity_db):
        """递增实体链接的记忆数"""
        entity_id = await entity_db.memory.insert_memory_entity(
            name="React", entity_type="IDENTIFIER", kind="技术"
        )
        await entity_db.memory.increment_entity_memory_count(entity_id)
        await entity_db.memory.increment_entity_memory_count(entity_id)
        entity = await entity_db.memory.find_memory_entity_by_id(entity_id)
        assert entity["memory_count"] == 2

    async def test_update_last_seen(self, entity_db):
        """更新实体最后出现时间"""
        entity_id = await entity_db.memory.insert_memory_entity(
            name="Vue", entity_type="IDENTIFIER", kind="技术"
        )
        new_ts = time.time() + 1000
        await entity_db.memory.update_entity_last_seen(entity_id, new_ts)
        entity = await entity_db.memory.find_memory_entity_by_id(entity_id)
        assert entity["last_seen"] == new_ts

    async def test_search_entity_by_fts(self, entity_db):
        """通过 FTS 模糊搜索实体"""
        await entity_db.memory.insert_memory_entity(
            name="人工智能", entity_type="TOPIC", kind="概念"
        )
        await entity_db.memory.insert_memory_entity(
            name="深度学习", entity_type="TOPIC", kind="技术"
        )
        results = await entity_db.memory.search_entities_by_fts("人工", limit=5)
        assert len(results) >= 1
        assert any(r["name"] == "人工智能" for r in results)


class TestEntityMemoryLinksCRUD:
    """entity_memory_links 表 CRUD"""

    async def test_insert_link(self, entity_db):
        """插入实体↔记忆反向链接"""
        # 先创建实体和记忆
        entity_id = await entity_db.memory.insert_memory_entity(
            name="Python", entity_type="IDENTIFIER", kind="技术"
        )
        mem_id = await entity_db.memory.insert_episodic_memory(summary="学习Python编程")

        link_id = await entity_db.memory.insert_entity_memory_link(
            entity_id=entity_id, memory_id=mem_id, confidence=0.95
        )
        assert link_id is not None
        assert link_id > 0

    async def test_insert_duplicate_link_returns_none(self, entity_db):
        """重复插入相同 (entity_id, memory_id) 返回 None"""
        entity_id = await entity_db.memory.insert_memory_entity(
            name="Java", entity_type="IDENTIFIER", kind="技术"
        )
        mem_id = await entity_db.memory.insert_episodic_memory(summary="学习Java")

        await entity_db.memory.insert_entity_memory_link(entity_id, mem_id)
        result = await entity_db.memory.insert_entity_memory_link(entity_id, mem_id)
        assert result is None

    async def test_get_links_by_entity(self, entity_db):
        """按实体 ID 查询反向链接的记忆"""
        entity_id = await entity_db.memory.insert_memory_entity(
            name="Rust", entity_type="IDENTIFIER", kind="技术"
        )
        mem1 = await entity_db.memory.insert_episodic_memory(summary="Rust入门")
        mem2 = await entity_db.memory.insert_episodic_memory(summary="Rust进阶")

        await entity_db.memory.insert_entity_memory_link(entity_id, mem1)
        await entity_db.memory.insert_entity_memory_link(entity_id, mem2)

        links = await entity_db.memory.get_entity_memory_links(entity_id)
        assert len(links) == 2
        memory_ids = [l["memory_id"] for l in links]
        assert mem1 in memory_ids
        assert mem2 in memory_ids

    async def test_get_memories_by_entity_names_scoped(self, entity_db):
        """按实体名列表 + scope 反查记忆"""
        from memory.scope import Scope
        scope = Scope(user_id="alice", agent_id="xiaoli")

        entity_id = await entity_db.memory.insert_memory_entity(
            name="TypeScript", entity_type="IDENTIFIER", kind="技术"
        )
        mem_id = await entity_db.memory.insert_episodic_memory(
            summary="TypeScript类型系统", scope=scope
        )
        await entity_db.memory.insert_entity_memory_link(entity_id, mem_id)

        results = await entity_db.memory.get_memories_by_entity_names_scoped(
            ["TypeScript"], scope=scope, limit=10
        )
        assert len(results) == 1
        assert results[0]["summary"] == "TypeScript类型系统"

    async def test_get_memories_by_entity_names_scoped_isolated(self, entity_db):
        """不同 scope 的记忆互不串"""
        from memory.scope import Scope
        scope_alice = Scope(user_id="alice", agent_id="xiaoli")
        scope_bob = Scope(user_id="bob", agent_id="xiaoke")

        entity_id = await entity_db.memory.insert_memory_entity(
            name="Go", entity_type="IDENTIFIER", kind="技术"
        )
        mem_alice = await entity_db.memory.insert_episodic_memory(
            summary="alice的Go笔记", scope=scope_alice
        )
        mem_bob = await entity_db.memory.insert_episodic_memory(
            summary="bob的Go笔记", scope=scope_bob
        )
        await entity_db.memory.insert_entity_memory_link(entity_id, mem_alice)
        await entity_db.memory.insert_entity_memory_link(entity_id, mem_bob)

        # alice scope 只查到 alice 的记忆
        results_alice = await entity_db.memory.get_memories_by_entity_names_scoped(
            ["Go"], scope=scope_alice, limit=10
        )
        assert len(results_alice) == 1
        assert results_alice[0]["summary"] == "alice的Go笔记"

        # bob scope 只查到 bob 的记忆
        results_bob = await entity_db.memory.get_memories_by_entity_names_scoped(
            ["Go"], scope=scope_bob, limit=10
        )
        assert len(results_bob) == 1
        assert results_bob[0]["summary"] == "bob的Go笔记"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_entity_crud.py -v`
Expected: FAIL with "AttributeError: 'MemoryDB' object has no attribute 'insert_memory_entity'"

- [ ] **Step 3: Write minimal implementation**

在 `db/db_memory.py` 中（`get_episodic_count_scoped` 方法之后）新增实体相关 CRUD 方法：

```python
    # ── mem0 SPEC: memory_entities 表 CRUD ──

    async def insert_memory_entity(self, name: str, entity_type: str = "TOPIC",
                                    kind: str = "", observations: str = "[]",
                                    metadata_json: str = "{}",
                                    auto_commit: bool = True) -> int | None:
        """插入实体记录。重复 (name, entity_type) 返回 None。

        Args:
            name: 实体名称
            entity_type: PROPER/QUOTED/TOPIC/IDENTIFIER
            kind: 人物/地点/组织/概念/技术
            observations: JSON 数组字符串
        Returns:
            新建实体 ID，重复时返回 None
        """
        now = time.time()
        try:
            cursor = await self._conn.execute(
                """INSERT OR IGNORE INTO memory_entities
                   (name, entity_type, kind, observations, memory_count,
                    first_seen, last_seen, metadata_json)
                   VALUES (?, ?, ?, ?, 0, ?, ?, ?)""",
                (name, entity_type, kind, observations, now, now, metadata_json),
            )
            if cursor.rowcount == 0:
                return None  # 重复插入
            entity_id = cursor.lastrowid
            if auto_commit:
                await self._conn.commit()
            return entity_id
        except Exception as e:
            logger.debug("db_memory.insert_entity_failed", error=str(e))
            return None

    async def find_memory_entity_by_name(self, name: str,
                                          entity_type: str = "TOPIC") -> dict | None:
        """按名称+类型查找实体"""
        try:
            cursor = await self._conn.execute(
                "SELECT * FROM memory_entities WHERE name=? AND entity_type=?",
                (name, entity_type),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.debug("db_memory.find_entity_failed", error=str(e))
            return None

    async def find_memory_entity_by_id(self, entity_id: int) -> dict | None:
        """按 ID 查找实体"""
        try:
            cursor = await self._conn.execute(
                "SELECT * FROM memory_entities WHERE id=?", (entity_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.debug("db_memory.find_entity_by_id_failed", error=str(e))
            return None

    async def search_entities_by_fts(self, query: str, limit: int = 10) -> list[dict]:
        """通过 FTS5 模糊搜索实体名称"""
        from db.fts_utils import _build_fts_query
        fts_query = _build_fts_query(query)
        if not fts_query:
            return []
        try:
            cursor = await self._conn.execute(
                """SELECT me.* FROM memory_entities_fts
                   JOIN memory_entities me ON me.id = memory_entities_fts.id
                   WHERE memory_entities_fts MATCH ?
                   LIMIT ?""",
                (fts_query, limit),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.debug("db_memory.search_entities_fts_failed", error=str(e))
            return []

    async def increment_entity_memory_count(self, entity_id: int,
                                             auto_commit: bool = True) -> None:
        """递增实体链接的记忆数"""
        try:
            await self._conn.execute(
                "UPDATE memory_entities SET memory_count = memory_count + 1 WHERE id=?",
                (entity_id,),
            )
            if auto_commit:
                await self._conn.commit()
        except Exception as e:
            logger.debug("db_memory.increment_entity_count_failed", error=str(e))

    async def update_entity_last_seen(self, entity_id: int, ts: float | None = None,
                                       auto_commit: bool = True) -> None:
        """更新实体最后出现时间"""
        if ts is None:
            ts = time.time()
        try:
            await self._conn.execute(
                "UPDATE memory_entities SET last_seen=? WHERE id=?",
                (ts, entity_id),
            )
            if auto_commit:
                await self._conn.commit()
        except Exception as e:
            logger.debug("db_memory.update_entity_last_seen_failed", error=str(e))

    async def update_memory_entity(self, entity_id: int, kind: str = "",
                                    observations: str = "",
                                    metadata_json: str = "",
                                    auto_commit: bool = True) -> bool:
        """更新实体字段"""
        try:
            sets = []
            params = []
            if kind:
                sets.append("kind = ?")
                params.append(kind)
            if observations:
                sets.append("observations = ?")
                params.append(observations)
            if metadata_json:
                sets.append("metadata_json = ?")
                params.append(metadata_json)
            if not sets:
                return False
            params.append(entity_id)
            await self._conn.execute(
                f"UPDATE memory_entities SET {', '.join(sets)} WHERE id=?",
                params,
            )
            if auto_commit:
                await self._conn.commit()
            return True
        except Exception as e:
            logger.debug("db_memory.update_entity_failed", error=str(e))
            return False

    # ── mem0 SPEC: entity_memory_links 表 CRUD ──

    async def insert_entity_memory_link(self, entity_id: int, memory_id: int,
                                         confidence: float = 1.0,
                                         auto_commit: bool = True) -> int | None:
        """插入实体↔记忆反向链接。重复 (entity_id, memory_id) 返回 None。"""
        try:
            cursor = await self._conn.execute(
                """INSERT OR IGNORE INTO entity_memory_links
                   (entity_id, memory_id, confidence, created_at)
                   VALUES (?, ?, ?, ?)""",
                (entity_id, memory_id, confidence, time.time()),
            )
            if cursor.rowcount == 0:
                return None
            link_id = cursor.lastrowid
            if auto_commit:
                await self._conn.commit()
            return link_id
        except Exception as e:
            logger.debug("db_memory.insert_link_failed", error=str(e))
            return None

    async def get_entity_memory_links(self, entity_id: int) -> list[dict]:
        """按实体 ID 查询反向链接的记忆 ID 列表"""
        try:
            cursor = await self._conn.execute(
                "SELECT * FROM entity_memory_links WHERE entity_id=? ORDER BY created_at DESC",
                (entity_id,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.debug("db_memory.get_links_failed", error=str(e))
            return []

    async def get_memories_by_entity_names_scoped(self, entity_names: list[str],
                                                   scope: Any,
                                                   limit: int = 10,
                                                   is_raw: int | None = 0) -> list[dict]:
        """按实体名列表 + scope 反查记忆（第5路召回核心查询）。

        Args:
            entity_names: 实体名列表
            scope: Scope 对象
            limit: 返回条数上限
            is_raw: None=不限, 0=只查提炼知识（默认）, 1=只查原始记录
        """
        if not entity_names:
            return []
        try:
            placeholders = ",".join("?" * len(entity_names))
            where_raw = ""
            params: list = list(entity_names) + [scope.user_id, scope.agent_id]
            if is_raw is not None:
                where_raw = " AND em.is_raw = ?"
                params.append(is_raw)
            params.append(limit)
            cursor = await self._conn.execute(
                f"""SELECT DISTINCT em.* FROM entity_memory_links eml
                   JOIN memory_entities me ON me.id = eml.entity_id
                   JOIN episodic_memories em ON em.id = eml.memory_id
                   WHERE me.name IN ({placeholders})
                     AND em.user_id = ? AND em.agent_id = ?{where_raw}
                   ORDER BY em.importance DESC, em.timestamp DESC LIMIT ?""",
                params,
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("db_memory.entity_names_scoped_search_failed", error=str(e))
            return []

    async def get_entities_by_memory_id(self, memory_id: int) -> list[dict]:
        """按记忆 ID 查询关联的实体列表"""
        try:
            cursor = await self._conn.execute(
                """SELECT me.* FROM entity_memory_links eml
                   JOIN memory_entities me ON me.id = eml.entity_id
                   WHERE eml.memory_id=?""",
                (memory_id,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.debug("db_memory.get_entities_by_memory_failed", error=str(e))
            return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_entity_crud.py -v`
Expected: PASS (12 tests passed)

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add db/db_memory.py tests/test_entity_crud.py
git commit -m "feat(db): add memory_entities + entity_memory_links CRUD methods"
```

---

## Task 4: EntityExtractor 混合实体提取

**Files:**
- Create: `memory/entity_extractor.py`
- Test: `tests/test_entity_extractor.py`

**Interfaces:**
- Consumes: jieba（已有依赖）
- Produces: `Entity` dataclass（`name: str`, `entity_type: str`, `kind: str`, `confidence: float`）；`EntityExtractor` 类；`EntityExtractor.extract(text, importance=0.5) -> list[Entity]`；`EntityExtractor._rule_based_extract(text) -> list[Entity]`；`EntityExtractor._llm_extract(text) -> list[Entity]`；`EntityExtractor._merge_entities(base, llm) -> list[Entity]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_entity_extractor.py
"""EntityExtractor 混合实体提取测试：jieba+规则快抽 → LLM 精抽"""
import asyncio
import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.entity_extractor import EntityExtractor, Entity


class TestEntityDataclass:
    """Entity dataclass"""

    def test_entity_creation(self):
        """创建 Entity 对象"""
        e = Entity(name="Python", entity_type="IDENTIFIER", kind="技术", confidence=0.9)
        assert e.name == "Python"
        assert e.entity_type == "IDENTIFIER"
        assert e.kind == "技术"
        assert e.confidence == 0.9

    def test_entity_defaults(self):
        """Entity 默认值"""
        e = Entity(name="测试")
        assert e.entity_type == "TOPIC"
        assert e.kind == ""
        assert e.confidence == 0.5


class TestRuleBasedExtract:
    """jieba+规则快抽（第1层）"""

    def _make_extractor(self):
        """创建不依赖 LLM 的 extractor"""
        return EntityExtractor(router=None)

    def test_extract_proper_noun(self):
        """提取专有名词（人名）"""
        extractor = self._make_extractor()
        entities = extractor._rule_based_extract("张三今天去了北京")
        names = [e.name for e in entities]
        # jieba 应能识别 "张三" 或 "北京"
        assert len(entities) > 0

    def test_extract_quoted_content(self):
        """提取引号内容"""
        extractor = self._make_extractor()
        entities = extractor._rule_based_extract('用户强调了"机器学习"的重要性')
        quoted = [e for e in entities if e.entity_type == "QUOTED"]
        assert len(quoted) >= 1
        assert "机器学习" in quoted[0].name

    def test_extract_identifier(self):
        """提取英文标识符"""
        extractor = self._make_extractor()
        entities = extractor._rule_based_extract("我喜欢用 Python 和 React 编程")
        identifiers = [e for e in entities if e.entity_type == "IDENTIFIER"]
        id_names = [e.name for e in identifiers]
        assert "Python" in id_names or "React" in id_names

    def test_extract_topic_keywords(self):
        """提取主题关键词"""
        extractor = self._make_extractor()
        entities = extractor._rule_based_extract("深度学习在计算机视觉领域有很多应用")
        topics = [e for e in entities if e.entity_type == "TOPIC"]
        assert len(topics) > 0

    def test_extract_empty_text(self):
        """空文本返回空列表"""
        extractor = self._make_extractor()
        entities = extractor._rule_based_extract("")
        assert entities == []

    def test_extract_no_duplicates(self):
        """同一实体不重复提取"""
        extractor = self._make_extractor()
        entities = extractor._rule_based_extract("Python Python Python")
        names = [e.name for e in entities]
        # 同名实体应去重
        assert len(names) == len(set(names))


class TestLLMExtract:
    """LLM 精抽（第2层，低置信度触发）"""

    def _make_extractor_with_mock_router(self):
        """创建带 mock router 的 extractor"""
        mock_router = MagicMock()
        return EntityExtractor(router=mock_router)

    async def test_llm_extract_success(self):
        """LLM 精抽返回结构化 JSON"""
        extractor = self._make_extractor_with_mock_router()
        mock_response = '[{"name":"量子计算","type":"TOPIC","kind":"概念"}]'
        extractor._call_llm = AsyncMock(return_value=mock_response)
        entities = await extractor._llm_extract("量子计算是未来技术")
        assert len(entities) == 1
        assert entities[0].name == "量子计算"
        assert entities[0].entity_type == "TOPIC"

    async def test_llm_extract_failure_fallback(self):
        """LLM 精抽失败返回空列表"""
        extractor = self._make_extractor_with_mock_router()
        extractor._call_llm = AsyncMock(return_value=None)
        entities = await extractor._llm_extract("测试文本")
        assert entities == []

    async def test_llm_extract_invalid_json(self):
        """LLM 返回非法 JSON 返回空列表"""
        extractor = self._make_extractor_with_mock_router()
        extractor._call_llm = AsyncMock(return_value="not a json")
        entities = await extractor._llm_extract("测试文本")
        assert entities == []


class TestExtractIntegration:
    """extract() 集成：jieba+规则 → 低置信度触发 LLM"""

    async def test_extract_no_llm_when_confidence_high(self):
        """jieba 提取 ≥2 个实体且 importance ≤ 0.7 时不触发 LLM"""
        extractor = EntityExtractor(router=None)
        extractor._rule_based_extract = MagicMock(return_value=[
            Entity(name="Python", entity_type="IDENTIFIER", confidence=0.9),
            Entity(name="编程", entity_type="TOPIC", confidence=0.8),
        ])
        extractor._llm_extract = AsyncMock(return_value=[])
        entities = await extractor.extract("我喜欢Python编程", importance=0.5)
        extractor._llm_extract.assert_not_awaited()
        assert len(entities) == 2

    async def test_extract_triggers_llm_when_few_entities(self):
        """jieba 提取 <2 个实体时触发 LLM"""
        extractor = EntityExtractor(router=None)
        extractor._rule_based_extract = MagicMock(return_value=[
            Entity(name="只有", entity_type="TOPIC", confidence=0.5),
        ])
        extractor._llm_extract = AsyncMock(return_value=[
            Entity(name="LLM补充", entity_type="TOPIC", confidence=0.8),
        ])
        entities = await extractor.extract("一些文本", importance=0.5)
        extractor._llm_extract.assert_awaited_once()
        # 合并后应有2个实体
        assert len(entities) >= 2

    async def test_extract_triggers_llm_when_high_importance(self):
        """importance > 0.7 时触发 LLM 精抽"""
        extractor = EntityExtractor(router=None)
        extractor._rule_based_extract = MagicMock(return_value=[
            Entity(name="Python", entity_type="IDENTIFIER", confidence=0.9),
            Entity(name="编程", entity_type="TOPIC", confidence=0.8),
        ])
        extractor._llm_extract = AsyncMock(return_value=[])
        entities = await extractor.extract("重要记忆", importance=0.85)
        extractor._llm_extract.assert_awaited_once()

    def test_merge_entities_dedup(self):
        """合并 jieba 和 LLM 结果，去重"""
        extractor = EntityExtractor(router=None)
        base = [
            Entity(name="Python", entity_type="IDENTIFIER", confidence=0.9),
            Entity(name="编程", entity_type="TOPIC", confidence=0.8),
        ]
        llm = [
            Entity(name="Python", entity_type="IDENTIFIER", confidence=0.95),
            Entity(name="机器学习", entity_type="TOPIC", confidence=0.85),
        ]
        merged = extractor._merge_entities(base, llm)
        names = [e.name for e in merged]
        assert "Python" in names
        assert "编程" in names
        assert "机器学习" in names
        # Python 不重复
        assert names.count("Python") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_entity_extractor.py -v`
Expected: FAIL with "No module named 'memory.entity_extractor'"

- [ ] **Step 3: Write minimal implementation**

创建 `memory/entity_extractor.py`：

```python
"""EntityExtractor — 混合实体提取器。

两层策略：
1. jieba 词性标注 + 规则快抽（<10ms）
2. 低置信度时触发 LLM 精抽（异步，+200-500ms）

实体类型分类（参考 mem0 原版）：
- PROPER: 专有名词（人名/地名/组织名）
- QUOTED: 引号内容（用户强调的概念）
- TOPIC: 主题关键词（jieba.extract_tags）
- IDENTIFIER: 技术标识符（英文/代码符号）
"""
import re
import json
from dataclasses import dataclass, field
from typing import Any
from loguru import logger


@dataclass
class Entity:
    """提取的实体"""
    name: str
    entity_type: str = "TOPIC"  # PROPER/QUOTED/TOPIC/IDENTIFIER
    kind: str = ""  # 人物/地点/组织/概念/技术
    confidence: float = 0.5


# 英文标识符正则（技术名词/代码符号）
_IDENTIFIER_PATTERN = re.compile(r'\b[A-Z][a-zA-Z0-9+#]*\b|\b[a-z][a-zA-Z0-9]+(?:\.[a-zA-Z0-9]+)+\b')

# 引号内容正则（中文引号 + 英文引号）
_QUOTED_PATTERN = re.compile(r'[""''「」『』"]([^""''「」『』"]{2,30})[""''「」『』"]')

# 词性 → entity_type 映射
_POS_TO_TYPE = {
    "nr": ("PROPER", "人物"),
    "ns": ("PROPER", "地点"),
    "nt": ("PROPER", "组织"),
    "nz": ("PROPER", "专有"),
}


class EntityExtractor:
    """混合实体提取器：jieba+规则快抽 → 低置信度时 LLM 精抽"""

    def __init__(self, router: Any | None = None) -> None:
        """
        Args:
            router: ModelRouter 实例（用于 LLM 精抽）。None 时只走 jieba 规则。
        """
        self.router = router
        self._llm_prompt_template = (
            "提取以下文本中的实体，返回JSON数组。\n"
            "每项格式：{{\"name\":\"实体名\",\"type\":\"PROPER|QUOTED|TOPIC|IDENTIFIER\",\"kind\":\"人物|地点|组织|概念|技术\"}}\n"
            "文本：{text}"
        )
        logger.info("entity_extractor.ready")

    async def extract(self, text: str, importance: float = 0.5) -> list[Entity]:
        """提取实体（两层策略）。

        Args:
            text: 输入文本
            importance: 记忆重要性（>0.7 触发 LLM 精抽）
        Returns:
            Entity 列表
        """
        if not text or not text.strip():
            return []

        # 第1层：jieba + 规则快抽
        entities = self._rule_based_extract(text)

        # 第2层：低置信度时触发 LLM 精抽
        # 触发条件：jieba 提取 <2 个实体，或 importance > 0.7
        if (len(entities) < 2 or importance > 0.7) and self.router:
            try:
                llm_entities = await self._llm_extract(text)
                if llm_entities:
                    entities = self._merge_entities(entities, llm_entities)
            except Exception as e:
                logger.debug("entity_extractor.llm_failed", error=str(e))

        return entities

    def _rule_based_extract(self, text: str) -> list[Entity]:
        """jieba 词性标注 + 正则规则快抽（<10ms）。

        提取策略：
        1. jieba.posseg.cut → nr/ns/nt/nz → PROPER
        2. 引号匹配 → QUOTED
        3. jieba.analyse.extract_tags → TOPIC
        4. 英文标识符正则 → IDENTIFIER
        """
        entities: list[Entity] = []
        seen_names: set[str] = set()

        try:
            import jieba.posseg as pseg
            import jieba.analyse

            # 1. jieba 词性标注 → PROPER（专有名词）
            for word, flag in pseg.cut(text):
                if flag in _POS_TO_TYPE and len(word) >= 2:
                    if word not in seen_names:
                        entity_type, kind = _POS_TO_TYPE[flag]
                        entities.append(Entity(
                            name=word, entity_type=entity_type, kind=kind,
                            confidence=0.85,
                        ))
                        seen_names.add(word)

            # 2. 引号内容 → QUOTED
            for match in _QUOTED_PATTERN.finditer(text):
                quoted_text = match.group(1).strip()
                if len(quoted_text) >= 2 and quoted_text not in seen_names:
                    entities.append(Entity(
                        name=quoted_text, entity_type="QUOTED", kind="概念",
                        confidence=0.9,
                    ))
                    seen_names.add(quoted_text)

            # 3. jieba 关键词提取 → TOPIC
            try:
                keywords = jieba.analyse.extract_tags(
                    text, topK=5, withWeight=False,
                    allowPOS=("n", "vn", "v", "eng", "nz"),
                )
                for kw in keywords:
                    if len(kw) >= 2 and kw not in seen_names:
                        entities.append(Entity(
                            name=kw, entity_type="TOPIC", kind="概念",
                            confidence=0.7,
                        ))
                        seen_names.add(kw)
            except Exception as e:
                logger.debug("entity_extractor.jieba_tags_failed", error=str(e))

        except ImportError:
            logger.debug("entity_extractor.jieba_not_available, using n-gram fallback")
            # 降级到 n-gram
            for n in range(2, 5):
                for i in range(len(text) - n + 1):
                    word = text[i:i + n]
                    if word not in seen_names and not word.isspace():
                        entities.append(Entity(
                            name=word, entity_type="TOPIC", confidence=0.3,
                        ))
                        seen_names.add(word)
                        if len(entities) >= 10:
                            break
                if len(entities) >= 10:
                    break

        # 4. 英文标识符 → IDENTIFIER
        for match in _IDENTIFIER_PATTERN.finditer(text):
            identifier = match.group()
            if len(identifier) >= 2 and identifier not in seen_names:
                entities.append(Entity(
                    name=identifier, entity_type="IDENTIFIER", kind="技术",
                    confidence=0.8,
                ))
                seen_names.add(identifier)

        return entities

    async def _llm_extract(self, text: str) -> list[Entity]:
        """LLM 精抽（结构化 JSON 输出）。

        Args:
            text: 输入文本
        Returns:
            Entity 列表。失败返回空列表。
        """
        if not self.router:
            return []

        prompt = self._llm_prompt_template.format(text=text[:500])
        messages = [{"role": "user", "content": prompt}]

        result = await self._call_llm(messages)
        if not result:
            return []

        try:
            # 解析 JSON 数组
            data = json.loads(result)
            entities = []
            for item in data:
                name = item.get("name", "").strip()
                if not name:
                    continue
                entity_type = item.get("type", "TOPIC").upper()
                if entity_type not in ("PROPER", "QUOTED", "TOPIC", "IDENTIFIER"):
                    entity_type = "TOPIC"
                kind = item.get("kind", "")
                entities.append(Entity(
                    name=name, entity_type=entity_type, kind=kind,
                    confidence=0.85,
                ))
            return entities
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug("entity_extractor.llm_parse_failed", error=str(e), raw=result[:200])
            return []

    async def _call_llm(self, messages: list[dict]) -> str | None:
        """调用 LLM（通过 router）。失败返回 None。"""
        if not self.router:
            return None
        try:
            result = await self.router.route(
                task_type="entity_extraction",
                messages=messages,
                temperature=0.3,
                max_tokens=300,
            )
            if isinstance(result, str):
                return result
            return None
        except Exception as e:
            logger.debug("entity_extractor.llm_call_failed", error=str(e))
            return None

    def _merge_entities(self, base: list[Entity], llm: list[Entity]) -> list[Entity]:
        """合并 jieba 和 LLM 结果，去重（以 name 为主键）。

        Args:
            base: jieba 提取结果
            llm: LLM 提取结果
        Returns:
            合并去重后的 Entity 列表
        """
        merged: list[Entity] = []
        seen: set[str] = set()

        # 先加入 base
        for e in base:
            key = e.name.lower()
            if key not in seen:
                merged.append(e)
                seen.add(key)

        # 再加入 LLM 独有的
        for e in llm:
            key = e.name.lower()
            if key not in seen:
                merged.append(e)
                seen.add(key)

        return merged
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_entity_extractor.py -v`
Expected: PASS (14 tests passed)

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add memory/entity_extractor.py tests/test_entity_extractor.py
git commit -m "feat(entity): add EntityExtractor with jieba+rule+LLM hybrid extraction"
```

---

## Task 5: EntityStore 实体存储与链接

**Files:**
- Create: `memory/entity_store.py`
- Test: `tests/test_entity_store.py`

**Interfaces:**
- Consumes: Task 2 的 `Scope`；Task 3 的 `MemoryDB.insert_memory_entity/find_memory_entity_by_name/increment_entity_memory_count/insert_entity_memory_link/update_entity_last_seen/get_memories_by_entity_names_scoped`；Task 4 的 `Entity` dataclass
- Produces: `EntityStore` 类；`EntityStore.link_entities(memory_id, entities, scope) -> int`；`EntityStore.recall_by_entities(entity_names, scope, limit) -> list[dict]`；`EntityStore.compute_entity_boost(entity, query_entities, now) -> float`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_entity_store.py
"""EntityStore 测试：实体存储 + 反向链接 + recall_by_entities + Entity Boost"""
import asyncio
import time
import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.entity_store import EntityStore, ENTITY_BOOST_WEIGHT, compute_entity_boost
from memory.entity_extractor import Entity
from memory.scope import Scope


@pytest.fixture
async def store_db(tmp_path):
    """创建带 v13 schema 的测试数据库 + EntityStore"""
    from db.database import DatabaseManager
    db_path = tmp_path / "test_entity_store.db"
    db = DatabaseManager(db_path)
    await db.init()
    store = EntityStore(db.memory)
    yield db, store
    await db.close()


class TestLinkEntities:
    """link_entities: 实体链接到记忆"""

    async def test_link_new_entity(self, store_db):
        """新实体：创建实体 + 建立反向链接"""
        db, store = store_db
        mem_id = await db.memory.insert_episodic_memory(summary="学习Python编程")
        entities = [Entity(name="Python", entity_type="IDENTIFIER", kind="技术")]

        linked = await store.link_entities(mem_id, entities, scope=Scope())
        assert linked == 1

        # 验证实体已创建
        entity = await db.memory.find_memory_entity_by_name("Python", "IDENTIFIER")
        assert entity is not None
        assert entity["memory_count"] == 1

        # 验证反向链接已建立
        links = await db.memory.get_entity_memory_links(entity["id"])
        assert len(links) == 1
        assert links[0]["memory_id"] == mem_id

    async def test_link_existing_entity(self, store_db):
        """已有实体：只建立反向链接，不重复创建"""
        db, store = store_db
        # 先创建实体
        entity_id = await db.memory.insert_memory_entity(
            name="React", entity_type="IDENTIFIER", kind="技术"
        )
        # 链接到第一条记忆
        mem1 = await db.memory.insert_episodic_memory(summary="React入门")
        await store.link_entities(mem1, [Entity(name="React", entity_type="IDENTIFIER")], scope=Scope())

        # 链接到第二条记忆
        mem2 = await db.memory.insert_episodic_memory(summary="React进阶")
        await store.link_entities(mem2, [Entity(name="React", entity_type="IDENTIFIER")], scope=Scope())

        # 验证实体只创建了一个
        entity = await db.memory.find_memory_entity_by_name("React", "IDENTIFIER")
        assert entity is not None
        assert entity["memory_count"] == 2

        # 验证有两条反向链接
        links = await db.memory.get_entity_memory_links(entity["id"])
        assert len(links) == 2

    async def test_link_multiple_entities(self, store_db):
        """一次链接多个实体"""
        db, store = store_db
        mem_id = await db.memory.insert_episodic_memory(summary="张三学习Python和React")
        entities = [
            Entity(name="张三", entity_type="PROPER", kind="人物"),
            Entity(name="Python", entity_type="IDENTIFIER", kind="技术"),
            Entity(name="React", entity_type="IDENTIFIER", kind="技术"),
        ]
        linked = await store.link_entities(mem_id, entities, scope=Scope())
        assert linked == 3

    async def test_link_empty_entities(self, store_db):
        """空实体列表返回 0"""
        db, store = store_db
        mem_id = await db.memory.insert_episodic_memory(summary="无实体记忆")
        linked = await store.link_entities(mem_id, [], scope=Scope())
        assert linked == 0

    async def test_link_updates_last_seen(self, store_db):
        """链接时更新实体 last_seen（时间感知）"""
        db, store = store_db
        # 先创建实体
        old_ts = time.time() - 86400  # 1天前
        entity_id = await db.memory.insert_memory_entity(
            name="OldEntity", entity_type="TOPIC", kind="概念"
        )
        # 手动设置旧的 last_seen
        await db.memory.update_entity_last_seen(entity_id, old_ts)

        # 链接到新记忆（应更新 last_seen）
        mem_id = await db.memory.insert_episodic_memory(summary="新记忆")
        await store.link_entities(
            mem_id, [Entity(name="OldEntity", entity_type="TOPIC")], scope=Scope()
        )

        entity = await db.memory.find_memory_entity_by_id(entity_id)
        assert entity["last_seen"] > old_ts


class TestRecallByEntities:
    """recall_by_entities: 通过实体名反向查询记忆"""

    async def test_recall_single_entity(self, store_db):
        """单实体反查记忆"""
        db, store = store_db
        scope = Scope(user_id="alice", agent_id="xiaoli")
        mem_id = await db.memory.insert_episodic_memory(
            summary="alice的Python笔记", scope=scope
        )
        await store.link_entities(
            mem_id, [Entity(name="Python", entity_type="IDENTIFIER")], scope=scope
        )

        results = await store.recall_by_entities(["Python"], scope=scope, limit=10)
        assert len(results) >= 1
        assert any(r["id"] == mem_id for r in results)

    async def test_recall_multiple_entities(self, store_db):
        """多实体反查记忆（UNION）"""
        db, store = store_db
        scope = Scope()
        mem1 = await db.memory.insert_episodic_memory(summary="Python笔记")
        mem2 = await db.memory.insert_episodic_memory(summary="React笔记")
        await store.link_entities(
            mem1, [Entity(name="Python", entity_type="IDENTIFIER")], scope=scope
        )
        await store.link_entities(
            mem2, [Entity(name="React", entity_type="IDENTIFIER")], scope=scope
        )

        results = await store.recall_by_entities(["Python", "React"], scope=scope, limit=10)
        mem_ids = [r["id"] for r in results]
        assert mem1 in mem_ids
        assert mem2 in mem_ids

    async def test_recall_scope_isolated(self, store_db):
        """不同 scope 的记忆互不串"""
        db, store = store_db
        scope_alice = Scope(user_id="alice", agent_id="xiaoli")
        scope_bob = Scope(user_id="bob", agent_id="xiaoke")

        mem_alice = await db.memory.insert_episodic_memory(
            summary="alice的Go笔记", scope=scope_alice
        )
        mem_bob = await db.memory.insert_episodic_memory(
            summary="bob的Go笔记", scope=scope_bob
        )

        # 共享同一实体名，但不同 scope
        await store.link_entities(
            mem_alice, [Entity(name="Go", entity_type="IDENTIFIER")], scope=scope_alice
        )
        await store.link_entities(
            mem_bob, [Entity(name="Go", entity_type="IDENTIFIER")], scope=scope_bob
        )

        # alice scope 只查到 alice 的记忆
        results_alice = await store.recall_by_entities(["Go"], scope=scope_alice, limit=10)
        mem_ids = [r["id"] for r in results_alice]
        assert mem_alice in mem_ids
        assert mem_bob not in mem_ids

    async def test_recall_empty_names(self, store_db):
        """空实体名列表返回空"""
        db, store = store_db
        results = await store.recall_by_entities([], scope=Scope(), limit=10)
        assert results == []

    async def test_recall_no_match(self, store_db):
        """无匹配实体返回空"""
        db, store = store_db
        results = await store.recall_by_entities(
            ["不存在的实体"], scope=Scope(), limit=10
        )
        assert results == []


class TestEntityBoost:
    """compute_entity_boost: Entity Boost 计算"""

    def test_boost_match(self):
        """实体匹配查询实体时 boost > 0"""
        entity = {"name": "Python", "memory_count": 5, "last_seen": time.time()}
        query_entities = {"Python", "React"}
        boost = compute_entity_boost(entity, query_entities, now=time.time())
        assert boost > 0
        assert boost <= ENTITY_BOOST_WEIGHT

    def test_boost_no_match(self):
        """实体不匹配查询实体时 boost = 0"""
        entity = {"name": "Java", "memory_count": 5, "last_seen": time.time()}
        query_entities = {"Python", "React"}
        boost = compute_entity_boost(entity, query_entities, now=time.time())
        assert boost == 0.0

    def test_boost_recency_decay(self):
        """时间衰减：旧的实体 boost 更低"""
        now = time.time()
        entity_recent = {"name": "Python", "memory_count": 5, "last_seen": now}
        entity_old = {"name": "Python", "memory_count": 5, "last_seen": now - 86400 * 30}

        query_entities = {"Python"}
        boost_recent = compute_entity_boost(entity_recent, query_entities, now=now)
        boost_old = compute_entity_boost(entity_old, query_entities, now=now)
        assert boost_recent > boost_old

    def test_boost_memory_count_diminishing(self):
        """记忆数边际递减：count=100 vs count=5 的 boost 差距不大"""
        now = time.time()
        entity_few = {"name": "Python", "memory_count": 5, "last_seen": now}
        entity_many = {"name": "Python", "memory_count": 100, "last_seen": now}
        query_entities = {"Python"}

        boost_few = compute_entity_boost(entity_few, query_entities, now=now)
        boost_many = compute_entity_boost(entity_many, query_entities, now=now)
        # 两者都 > 0，且 many >= few（边际递减但不为负）
        assert boost_few > 0
        assert boost_many > 0
        assert boost_many >= boost_few * 0.9  # 差距不大
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_entity_store.py -v`
Expected: FAIL with "No module named 'memory.entity_store'"

- [ ] **Step 3: Write minimal implementation**

创建 `memory/entity_store.py`：

```python
"""EntityStore — 实体存储管理与反向链接。

职责：
1. link_entities: 将提取的实体链接到记忆（FTS5 名称匹配 + 反向链接）
2. recall_by_entities: 通过实体名反向查询记忆（第5路召回核心）
3. compute_entity_boost: 计算 Entity Boost（精排加分）

参考 mem0 原版 scoring.py 的 Entity Boost 公式。
"""
import time
from typing import Any
from loguru import logger

from memory.entity_extractor import Entity
from memory.scope import Scope


# Entity Boost 权重（与 mem0 原版一致）
ENTITY_BOOST_WEIGHT = 0.5


def compute_entity_boost(entity: dict, query_entities: set[str],
                          now: float | None = None) -> float:
    """计算实体对查询的 boost 值。

    参考 mem0 原版 scoring.py：
    boost = similarity × ENTITY_BOOST_WEIGHT × memory_count_weight × recency

    Args:
        entity: 实体 dict，包含 name/memory_count/last_seen
        query_entities: 查询中提取的实体名集合
        now: 当前时间戳（测试可注入），None 则用 time.time()
    Returns:
        Entity Boost 值 [0, ENTITY_BOOST_WEIGHT]
    """
    if now is None:
        now = time.time()

    # 1. 实体与查询实体的匹配度
    similarity = 1.0 if entity.get("name", "") in query_entities else 0.0
    if similarity == 0.0:
        return 0.0

    # 2. 记忆数权重：链接记忆越多越重要，但边际递减
    #    mem0 原版公式: 1/(1 + 0.001*(count-1)^2)
    count = max(1, entity.get("memory_count", 1))
    memory_count_weight = 1.0 / (1.0 + 0.001 * (count - 1) ** 2)

    # 3. 时间衰减因子（天级）
    last_seen = entity.get("last_seen", now)
    recency = 1.0 / (1.0 + max(0, now - last_seen) / 86400.0)

    # 4. Entity Boost
    return similarity * ENTITY_BOOST_WEIGHT * memory_count_weight * recency


class EntityStore:
    """实体存储管理：CRUD + 反向链接查询。

    与 KG 的 knowledge_graph.py 职责分离：
    - KG: 知识图谱（实体+关系+观察），用于推理
    - EntityStore: 记忆实体链接（实体↔记忆反向链接），用于检索召回
    """

    def __init__(self, memory_db: Any) -> None:
        """
        Args:
            memory_db: MemoryDB 实例（提供实体 CRUD 方法）
        """
        self.db = memory_db
        logger.info("entity_store.ready")

    async def link_entities(self, memory_id: int, entities: list[Entity],
                             scope: Scope | None = None) -> int:
        """将提取的实体链接到记忆。

        流程：
        1. FTS5/精确匹配查找已有实体
        2. 找到 → 建立反向链接 + memory_count++
        3. 未找到 → 创建新实体 + 建立反向链接
        4. 更新实体 last_seen（时间感知）

        Args:
            memory_id: 记忆 ID
            entities: Entity 列表
            scope: Scope 对象（用于未来扩展，当前链接不按 scope 隔离实体）
        Returns:
            成功链接的实体数
        """
        if not entities:
            return 0

        linked = 0
        for entity in entities:
            try:
                # 1. 查找已有实体（精确匹配 name + entity_type）
                existing = await self.db.find_memory_entity_by_name(
                    entity.name, entity.entity_type
                )

                if existing:
                    # 2a. 找到匹配 → 建立反向链接
                    link_id = await self.db.insert_entity_memory_link(
                        entity_id=existing["id"],
                        memory_id=memory_id,
                        confidence=entity.confidence,
                    )
                    if link_id is not None:
                        # 新链接才递增 count
                        await self.db.increment_entity_memory_count(existing["id"])
                    # 更新 last_seen
                    await self.db.update_entity_last_seen(existing["id"])
                else:
                    # 2b. 无匹配 → 创建新实体
                    new_id = await self.db.insert_memory_entity(
                        name=entity.name,
                        entity_type=entity.entity_type,
                        kind=entity.kind,
                    )
                    if new_id is not None:
                        await self.db.insert_entity_memory_link(
                            entity_id=new_id,
                            memory_id=memory_id,
                            confidence=entity.confidence,
                        )
                        # 新实体 memory_count 从 0 → 1
                        await self.db.increment_entity_memory_count(new_id)
                linked += 1
            except Exception as e:
                logger.debug("entity_store.link_failed",
                             entity=entity.name, error=str(e))
        return linked

    async def recall_by_entities(self, entity_names: list[str],
                                  scope: Scope, limit: int = 10,
                                  is_raw: int | None = 0) -> list[dict]:
        """通过实体名反向查询关联的记忆（检索时第5路召回）。

        Args:
            entity_names: 实体名列表
            scope: Scope 对象（scope 隔离）
            limit: 返回条数上限
            is_raw: None=不限, 0=只查提炼知识（默认）, 1=只查原始记录
        Returns:
            记忆 dict 列表
        """
        if not entity_names:
            return []
        try:
            return await self.db.get_memories_by_entity_names_scoped(
                entity_names, scope=scope, limit=limit, is_raw=is_raw
            )
        except Exception as e:
            logger.debug("entity_store.recall_failed", error=str(e))
            return []

    async def get_query_entities_boost(self, memory_id: int,
                                        query_entities: set[str],
                                        now: float | None = None) -> float:
        """计算指定记忆关联的所有实体对查询的总 boost 值。

        用于精排阶段：对每个候选记忆，计算其关联实体与查询实体的 boost 之和。

        Args:
            memory_id: 记忆 ID
            query_entities: 查询中提取的实体名集合
            now: 当前时间戳
        Returns:
            总 Entity Boost 值（0 表示无匹配）
        """
        try:
            entities = await self.db.get_entities_by_memory_id(memory_id)
            if not entities:
                return 0.0
            total_boost = 0.0
            for entity in entities:
                total_boost += compute_entity_boost(entity, query_entities, now)
            return total_boost
        except Exception as e:
            logger.debug("entity_store.get_boost_failed", error=str(e))
            return 0.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_entity_store.py -v`
Expected: PASS (13 tests passed)

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add memory/entity_store.py tests/test_entity_store.py
git commit -m "feat(entity): add EntityStore with link_entities + recall + Entity Boost"
```

---

## Task 6: ADD-only 编码流程

**Files:**
- Modify: `memory/memory_manager.py:322-340` (`_has_duplicate` 改为只对 is_raw=0 生效)
- Modify: `memory/memory_manager.py:1370-1501` (`encode_memory` 接入 ADD-only + 实体提取)
- Modify: `memory/memory_manager.py` (新增 `_extract_and_link_entities` 方法)
- Test: `tests/test_add_only_architecture.py`

**Interfaces:**
- Consumes: Task 2 的 `Scope` + `insert_episodic_memory(scope, is_raw)`；Task 4 的 `EntityExtractor.extract`；Task 5 的 `EntityStore.link_entities`
- Produces: `MemoryManager.encode_memory(context, scope=None)`（scope 参数可选，None 时用默认 Scope）；`MemoryManager._has_duplicate(summary, scope=None)`（只对 is_raw=0 生效）；`MemoryManager._extract_and_link_entities(memory_id, text, scope)`；`MemoryManager.entity_extractor` 属性；`MemoryManager.entity_store` 属性

- [ ] **Step 1: Write the failing test**

```python
# tests/test_add_only_architecture.py
"""ADD-only 编码流程测试：原始记忆 append-only + 异步实体提取"""
import asyncio
import time
import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.scope import Scope


@pytest.fixture
async def add_only_db(tmp_path):
    """创建带 v13 schema 的测试数据库 + MemoryManager"""
    from db.database import DatabaseManager
    from memory.memory_manager import MemoryManager
    db_path = tmp_path / "test_add_only.db"
    db = DatabaseManager(db_path)
    await db.init()

    # 创建最小化的 MemoryManager（mock 依赖）
    mgr = MemoryManager.__new__(MemoryManager)
    mgr.db = db
    mgr.memory = db.memory
    mgr.vec = None  # 测试不用向量
    mgr.kg = None
    mgr._security_filter = None
    mgr._reranker = None
    mgr._governance = None
    mgr._last_encode_time = 0
    mgr._pending_encode = False
    mgr._last_message_time = time.time()
    mgr.entity_extractor = None
    mgr.entity_store = None

    yield db, mgr
    await db.close()


class TestHasDuplicateScoped:
    """_has_duplicate 改为只对 is_raw=0 生效"""

    async def test_has_duplicate_checks_refined_only(self, add_only_db):
        """原始记忆（is_raw=1）不去重，提炼知识（is_raw=0）去重"""
        db, mgr = add_only_db
        scope = Scope()

        # 插入一条提炼知识
        await db.memory.insert_episodic_memory(
            summary="用户喜欢Python编程语言", scope=scope, is_raw=0
        )

        # 检查重复（应返回 True，因为有 is_raw=0 的相同记忆）
        is_dup = await mgr._has_duplicate("用户喜欢Python编程语言", scope=scope)
        assert is_dup is True

    async def test_has_duplicate_ignores_raw(self, add_only_db):
        """is_raw=1 的原始记忆不参与去重判断"""
        db, mgr = add_only_db
        scope = Scope()

        # 只插入原始记忆（is_raw=1）
        await db.memory.insert_episodic_memory(
            summary="这是一条原始记录", scope=scope, is_raw=1
        )

        # 检查重复（应返回 False，因为 is_raw=1 不参与去重）
        is_dup = await mgr._has_duplicate("这是一条原始记录", scope=scope)
        assert is_dup is False

    async def test_has_duplicate_scope_isolated(self, add_only_db):
        """不同 scope 的记忆不互相去重"""
        db, mgr = add_only_db
        scope_alice = Scope(user_id="alice", agent_id="xiaoli")
        scope_bob = Scope(user_id="bob", agent_id="xiaoke")

        # alice 的提炼知识
        await db.memory.insert_episodic_memory(
            summary="相同的记忆内容", scope=scope_alice, is_raw=0
        )

        # bob 检查相同内容（应返回 False，不同 scope）
        is_dup = await mgr._has_duplicate("相同的记忆内容", scope=scope_bob)
        assert is_dup is False


class TestEncodeMemoryAddOnly:
    """encode_memory: ADD-only 原始记忆写入"""

    async def test_encode_writes_raw_memory(self, add_only_db):
        """encode_memory 写入 is_raw=1 的原始记忆"""
        db, mgr = add_only_db
        scope = Scope(user_id="test_user", agent_id="test_agent")

        # mock _generate_summary 返回固定文本
        mgr._generate_summary = MagicMock(return_value="用户说: 我喜欢Python")
        mgr._estimate_importance = MagicMock(return_value=0.8)
        mgr._save_state_json = MagicMock()
        mgr.invalidate_memory_count_cache = MagicMock()

        context = {
            "exchanges": [
                {"role": "user", "content": "我喜欢Python"},
                {"role": "assistant", "content": "好的，记下了"},
            ],
            "emotion": {"primary": "开心"},
        }

        await mgr.encode_memory(context, scope=scope)

        # 验证写入了 is_raw=1 的原始记忆
        cursor = await db._conn.execute(
            "SELECT * FROM episodic_memories WHERE user_id=? AND agent_id=? AND is_raw=1",
            (scope.user_id, scope.agent_id),
        )
        rows = await cursor.fetchall()
        assert len(rows) >= 1
        assert "我喜欢Python" in rows[0]["summary"]

    async def test_encode_does_not_dedup_raw(self, add_only_db):
        """encode_memory 对原始记忆不去重（连续两次编码相同内容都写入）"""
        db, mgr = add_only_db
        scope = Scope()

        mgr._generate_summary = MagicMock(return_value="重复的记忆内容")
        mgr._estimate_importance = MagicMock(return_value=0.5)
        mgr._save_state_json = MagicMock()
        mgr.invalidate_memory_count_cache = MagicMock()

        context = {
            "exchanges": [
                {"role": "user", "content": "测试"},
                {"role": "assistant", "content": "回复"},
            ],
        }

        # 连续两次编码相同内容
        await mgr.encode_memory(context, scope=scope)
        await mgr.encode_memory(context, scope=scope)

        # 验证两条 is_raw=1 记录都存在
        cursor = await db._conn.execute(
            "SELECT COUNT(*) as cnt FROM episodic_memories WHERE is_raw=1 AND summary='重复的记忆内容'"
        )
        row = await cursor.fetchone()
        assert row["cnt"] == 2

    async def test_encode_triggers_entity_extraction(self, add_only_db):
        """encode_memory 异步触发实体提取+链接"""
        db, mgr = add_only_db
        scope = Scope()

        mgr._generate_summary = MagicMock(return_value="用户说: 我喜欢Python和React")
        mgr._estimate_importance = MagicMock(return_value=0.5)
        mgr._save_state_json = MagicMock()
        mgr.invalidate_memory_count_cache = MagicMock()

        # mock entity_extractor 和 entity_store
        from memory.entity_extractor import Entity
        mgr.entity_extractor = MagicMock()
        mgr.entity_extractor.extract = AsyncMock(return_value=[
            Entity(name="Python", entity_type="IDENTIFIER"),
            Entity(name="React", entity_type="IDENTIFIER"),
        ])
        mgr.entity_store = MagicMock()
        mgr.entity_store.link_entities = AsyncMock(return_value=2)

        context = {
            "exchanges": [
                {"role": "user", "content": "我喜欢Python和React"},
                {"role": "assistant", "content": "好的"},
            ],
        }

        await mgr.encode_memory(context, scope=scope)

        # 等待异步任务完成
        await asyncio.sleep(0.1)

        # 验证 entity_extractor.extract 被调用
        mgr.entity_extractor.extract.assert_awaited_once()
        # 验证 entity_store.link_entities 被调用
        mgr.entity_store.link_entities.assert_awaited_once()

    async def test_encode_without_scope_uses_default(self, add_only_db):
        """encode_memory 不传 scope 时使用默认 Scope()"""
        db, mgr = add_only_db

        mgr._generate_summary = MagicMock(return_value="默认scope测试")
        mgr._estimate_importance = MagicMock(return_value=0.5)
        mgr._save_state_json = MagicMock()
        mgr.invalidate_memory_count_cache = MagicMock()

        context = {
            "exchanges": [
                {"role": "user", "content": "测试"},
                {"role": "assistant", "content": "回复"},
            ],
        }

        await mgr.encode_memory(context)  # 不传 scope

        # 验证写入了默认 scope 的记忆
        cursor = await db._conn.execute(
            "SELECT * FROM episodic_memories WHERE user_id='default' AND agent_id='xiaoda' AND is_raw=1"
        )
        rows = await cursor.fetchall()
        assert len(rows) >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_add_only_architecture.py -v`
Expected: FAIL with "_has_duplicate() got an unexpected keyword argument 'scope'" or "encode_memory() got an unexpected keyword argument 'scope'"

- [ ] **Step 3: Write minimal implementation**

修改 `memory/memory_manager.py:322-340` 的 `_has_duplicate` 方法：

```python
    async def _has_duplicate(self, summary: str, scope: Any | None = None) -> bool:
        """检查是否存在归一化后内容相同的已有记忆（只对 is_raw=0 的提炼知识生效）。

        mem0 SPEC 优化：原始记忆（is_raw=1）不去重，保证 append-only 可追溯。

        Args:
            scope: Scope 对象。传入时只在同 scope 内查重。
        """
        normalized = _normalize_for_dedupe(summary)
        if len(normalized) < 10:
            return False
        try:
            # 用 FTS 搜索相关记忆，然后精确匹配
            if scope is not None:
                # scope 过滤：只查 is_raw=0 的提炼知识
                candidates = await self.memory.search_memories_fts_scoped(
                    summary, scope=scope, limit=5, is_raw=0
                )
            else:
                candidates = await self.memory.search_memories_fts(summary, limit=5)
            for c in candidates:
                # 只对 is_raw=0 的记忆判断重复
                if c.get("is_raw", 0) == 0 and _normalize_for_dedupe(c.get("summary", "")) == normalized:
                    return True
            # FTS 无结果时也检查最近记忆
            recent = await self.memory.get_episodic_recent(limit=10)
            for r in recent:
                if r.get("is_raw", 0) == 0 and _normalize_for_dedupe(r.get("summary", "")) == normalized:
                    return True
        except (OSError, TypeError):
            logger.debug("memory_manager.is_duplicate_check_failed", exc_info=True)
        return False
```

修改 `memory/memory_manager.py:1370` 的 `encode_memory` 方法签名和前半部分：

```python
    async def encode_memory(self, context: dict, scope: Any | None = None) -> None:
        """编码记忆（ADD-only 架构）。

        mem0 SPEC 优化：
        1. 写入 is_raw=1 的原始记忆（append-only，不去重，不覆盖）
        2. 异步触发实体提取+链接
        3. 异步触发蒸馏（生成 is_raw=0 的提炼知识，Task 7 实现）

        Args:
            context: 包含 exchanges 列表的上下文
            scope: Scope 对象。None 时使用默认 Scope()。
        """
        # scope 默认值
        if scope is None:
            from memory.scope import Scope
            scope = Scope()

        exchanges = context.get("exchanges", [])
        if not exchanges or len(exchanges) < 2:
            return

        summary = self._generate_summary(exchanges)

        # 安全过滤
        validation = validate_memory_content(summary)
        if validation:
            logger.warning("memory.safety_blocked", reason=validation)
            return

        # ADD-only: 原始记忆不去重，直接写入
        # （_has_duplicate 只在蒸馏时对 is_raw=0 生效，这里不调用）

        # 原有安全扫描（保留兼容）
        from security.security import SecurityFilter
        security = self._security_filter or SecurityFilter()
        threat_result = security.scan_threats(summary, scope="strict")
        if not threat_result.is_safe and threat_result.action == "block":
            logger.warning("memory.security_blocked", threat=threat_result.threat_type)
            return

        importance = self._estimate_importance(exchanges, context)
        emotion = context.get("emotion", {}).get("primary", "")

        # 规则提取增强重要性
        user_msg = ""
        assistant_msg = ""
        for msg in exchanges[-6:]:
            if msg.get("role") == "user":
                user_msg += msg.get("content", "") + " "
            elif msg.get("role") == "assistant":
                assistant_msg += msg.get("content", "") + " "
        rule_extractor = RuleBasedMemoryExtractor()
        rule_matches = rule_extractor.extract(user_msg, assistant_msg)
        if rule_matches:
            best_rule = max(rule_matches, key=lambda r: r["importance"])
            importance = max(importance, best_rule["importance"])

        try:
            # 写入候选审计表
            candidate_id = await self.memory.insert_consolidation_candidate(
                source="encode",
                kind=rule_matches[0]["kind"] if rule_matches else "episodic",
                summary=summary,
                confidence=rule_matches[0]["confidence"] if rule_matches else 0.5,
                importance=importance,
            )

            # ADD-only: 写入 is_raw=1 的原始记忆（不去重，不覆盖）
            mem_id = await self.memory.insert_episodic_memory(
                summary=summary,
                importance=importance,
                emotion_label=emotion,
                scope=scope,
                is_raw=1,
            )

            # 标记候选已应用
            await self.memory.mark_candidate_applied(candidate_id, mem_id)

            # ContextNest A3: 记录初始版本哈希链 (tamper-evident)
            if self._governance:
                try:
                    await self._governance.record_initial_version(mem_id, summary, auto_commit=False)
                except Exception as e:
                    logger.debug("memory.governance_init_failed", error=str(e))

            if self.vec and summary:
                try:
                    await self.vec.upsert(mem_id, summary)
                except Exception as e:
                    logger.debug("memory.initial_vec_upsert_failed", error=str(e))

            # ── 父子Chunk: 生成并写入子chunk ──
            import config as _cfg
            if getattr(_cfg, 'PARENT_CHILD_CHUNK_ENABLED', True):
                try:
                    children = self._split_into_children(exchanges, mem_id, summary)
                    if children and self.vec:
                        child_items = []
                        for child in children:
                            child_id = await self.memory.insert_child_chunk(
                                parent_id=mem_id,
                                content=child['content'],
                                embed_content=child['embed_content'],
                                chunk_type=child['chunk_type'],
                                importance=importance * child['weight'],
                                overlap_hash=child['overlap_hash'],
                            )
                            child_items.append((child_id, child['embed_content']))
                        # 批量嵌入子chunk
                        await self.vec.batch_upsert_children(child_items)
                        logger.debug("memory.child_chunks_created",
                                     parent_id=mem_id, count=len(children))
                except Exception as e:
                    logger.debug("memory.child_chunk_failed", error=str(e))

            # ── mem0 SPEC: 异步触发实体提取+链接 ──
            if self.entity_extractor and self.entity_store:
                try:
                    _entity_task = asyncio.create_task(
                        self._extract_and_link_entities(mem_id, summary, scope)
                    )
                    def _log_entity_exception(t: asyncio.Task) -> None:
                        if t.cancelled():
                            return
                        exc = t.exception()
                        if exc:
                            logger.warning("memory.entity_async_failed", error=str(exc))
                    _entity_task.add_done_callback(_log_entity_exception)
                except Exception as e:
                    logger.debug("memory.entity_spawn_failed", error=str(e))

            # ── mem0 SPEC: 异步触发蒸馏（Task 7 实现）──
            if hasattr(self, '_distill_to_knowledge'):
                try:
                    _distill_task = asyncio.create_task(
                        self._distill_to_knowledge(mem_id, summary, scope, importance, emotion)
                    )
                    def _log_distill_exception(t: asyncio.Task) -> None:
                        if t.cancelled():
                            return
                        exc = t.exception()
                        if exc:
                            logger.warning("memory.distill_async_failed", error=str(exc))
                    _distill_task.add_done_callback(_log_distill_exception)
                except Exception as e:
                    logger.debug("memory.distill_spawn_failed", error=str(e))

            self._last_encode_time = time.time()
            self._pending_encode = False
            logger.info("memory.encoded", summary=summary[:80], importance=importance, is_raw=1)

            # 冷启动路由: 新记忆写入后失效计数缓存, 下次检索立即感知档位变化
            self.invalidate_memory_count_cache()

            self._save_state_json(summary, importance, emotion)

            # fire-and-forget 后台 LLM 结构化提取（不阻塞主流程）
            # 用 GLM-4-9B-0414 提取实体/事件/决策/偏好，完成后更新记忆条目
            try:
                _enrich_task = asyncio.create_task(
                    self._enrich_memory_async(mem_id, exchanges)
                )
                def _log_enrich_exception(t: asyncio.Task) -> None:
                    if t.cancelled():
                        return
                    exc = t.exception()
                    if exc:
                        logger.warning("memory.enrich_async_failed", error=str(exc))

                _enrich_task.add_done_callback(_log_enrich_exception)
            except Exception as e:
                logger.debug("memory.enrich_spawn_failed", error=str(e))
        except Exception as e:
            logger.warning("memory.encode_failed", error=str(e))

        if self.kg and summary:
            try:
                await self.kg.auto_extract_and_merge(summary)
            except Exception as e:
                logger.debug("memory.kg_extract_failed", error=str(e))
```

在 `memory/memory_manager.py` 的 `encode_memory` 方法之后（约第 1503 行后）新增 `_extract_and_link_entities` 方法：

```python
    async def _extract_and_link_entities(self, memory_id: int, summary: str,
                                          scope: Any) -> None:
        """异步提取实体并建立反向链接（mem0 SPEC 优化）。

        Args:
            memory_id: 原始记忆 ID
            summary: 记忆摘要文本
            scope: Scope 对象
        """
        if not self.entity_extractor or not self.entity_store:
            return
        try:
            # 提取实体
            entities = await self.entity_extractor.extract(summary, importance=0.5)
            if not entities:
                return
            # 链接到记忆
            linked = await self.entity_store.link_entities(memory_id, entities, scope=scope)
            logger.debug("memory.entities_linked",
                         memory_id=memory_id, count=linked)
        except Exception as e:
            logger.debug("memory.extract_link_entities_failed", error=str(e))
```

修改 `memory/memory_manager.py` 的 `__init__` 方法（约第 215 行），添加 entity_extractor 和 entity_store 属性：

```python
    def __init__(self, db: DatabaseManager, memory: MemoryDB,
                 vector_store: VectorStore | None = None,
                 router: Any | None=None, knowledge_graph: Any | None=None, security_filter: Any | None=None,
                 reranker: Any | None=None, query_transformer: Any | None=None,
                 governance: Any | None=None,
                 entity_extractor: Any | None=None,
                 entity_store: Any | None=None) -> None:
        # ... 原有初始化代码 ...
        self.entity_extractor = entity_extractor
        self.entity_store = entity_store
```

（注：需在 `__init__` 方法末尾追加这两行赋值，不修改原有逻辑）

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_add_only_architecture.py -v`
Expected: PASS (7 tests passed)

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add memory/memory_manager.py tests/test_add_only_architecture.py
git commit -m "feat(memory): ADD-only encode_memory + scoped _has_duplicate + async entity extraction"
```

---

## Task 7: 蒸馏流程变更

**Files:**
- Modify: `memory/memory_distiller.py` (新增 `merge_knowledge` 方法)
- Modify: `memory/memory_manager.py` (新增 `_distill_to_knowledge` + `_find_similar_knowledge` + `_update_knowledge` 方法)
- Test: `tests/test_distill_merge.py`

**Interfaces:**
- Consumes: Task 6 的 `encode_memory`（异步触发 `_distill_to_knowledge`）；Task 2 的 `Scope` + `search_memories_fts_scoped(is_raw=0)`
- Produces: `MemoryDistiller.merge_knowledge(existing, new_content) -> str`；`MemoryManager._distill_to_knowledge(raw_id, summary, scope, importance, emotion)`；`MemoryManager._find_similar_knowledge(summary, scope) -> dict | None`；`MemoryManager._update_knowledge(knowledge_id, new_content, raw_id)`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_distill_merge.py
"""蒸馏流程测试：merge_knowledge + _distill_to_knowledge + _update_knowledge"""
import asyncio
import time
import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.scope import Scope
from memory.memory_distiller import MemoryDistiller


@pytest.fixture
async def distill_db(tmp_path):
    """创建带 v13 schema 的测试数据库 + MemoryManager"""
    from db.database import DatabaseManager
    from memory.memory_manager import MemoryManager
    db_path = tmp_path / "test_distill.db"
    db = DatabaseManager(db_path)
    await db.init()

    mgr = MemoryManager.__new__(MemoryManager)
    mgr.db = db
    mgr.memory = db.memory
    mgr.vec = None
    mgr.kg = None
    mgr._security_filter = None
    mgr._reranker = None
    mgr._governance = None
    mgr.entity_extractor = None
    mgr.entity_store = None
    mgr.distiller = MemoryDistiller(router=None)

    yield db, mgr
    await db.close()


class TestMergeKnowledge:
    """MemoryDistiller.merge_knowledge: LLM 合并相似知识"""

    def _make_distiller(self):
        """创建带 mock 的 distiller"""
        distiller = MemoryDistiller(router=None)
        distiller._free_api_key = "fake-key"
        return distiller

    async def test_merge_success(self):
        """LLM 合并两段知识"""
        distiller = self._make_distiller()
        distiller._call_free_model = AsyncMock(return_value="合并后的知识：用户喜欢Python和React")
        result = await distiller.merge_knowledge(
            existing="用户喜欢Python",
            new_content="用户也喜欢React",
        )
        assert result == "合并后的知识：用户喜欢Python和React"
        distiller._call_free_model.assert_awaited_once()

    async def test_merge_failure_returns_new(self):
        """LLM 合并失败时返回 new_content（不合并，直接用新内容）"""
        distiller = self._make_distiller()
        distiller._call_free_model = AsyncMock(return_value=None)
        result = await distiller.merge_knowledge(
            existing="旧知识",
            new_content="新知识",
        )
        assert result == "新知识"

    async def test_merge_empty_existing(self):
        """existing 为空时直接返回 new_content"""
        distiller = self._make_distiller()
        result = await distiller.merge_knowledge(existing="", new_content="新知识")
        assert result == "新知识"
        distiller._call_free_model.assert_not_awaited()


class TestDistillToKnowledge:
    """_distill_to_knowledge: 原始记忆 → 提炼知识"""

    async def test_distill_creates_new_knowledge(self, distill_db):
        """无相似知识时新建 is_raw=0 的提炼知识"""
        db, mgr = distill_db
        scope = Scope()

        # 插入原始记忆
        raw_id = await db.memory.insert_episodic_memory(
            summary="原始记录：用户喜欢Python", scope=scope, is_raw=1
        )

        # mock distiller.distill 返回蒸馏结果
        mgr.distiller.distill = AsyncMock(return_value="用户喜欢Python编程")
        mgr.distiller.merge_knowledge = AsyncMock(return_value="合并知识")
        # mock _find_similar_knowledge 返回 None（无相似）
        mgr._find_similar_knowledge = AsyncMock(return_value=None)

        await mgr._distill_to_knowledge(raw_id, "原始记录：用户喜欢Python", scope, 0.8, "开心")

        # 验证创建了 is_raw=0 的提炼知识
        cursor = await db._conn.execute(
            "SELECT * FROM episodic_memories WHERE is_raw=0 AND user_id=? AND agent_id=?",
            (scope.user_id, scope.agent_id),
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert rows[0]["summary"] == "用户喜欢Python编程"

    async def test_distill_updates_existing_knowledge(self, distill_db):
        """有相似知识时 UPDATE（合并）"""
        db, mgr = distill_db
        scope = Scope()

        # 先插入一条提炼知识
        existing_id = await db.memory.insert_episodic_memory(
            summary="用户喜欢Python", scope=scope, is_raw=0
        )

        # 插入原始记忆
        raw_id = await db.memory.insert_episodic_memory(
            summary="用户也喜欢React", scope=scope, is_raw=1
        )

        # mock 返回相似知识
        existing_mem = await db.memory.get_memory_by_id(existing_id)
        mgr._find_similar_knowledge = AsyncMock(return_value=existing_mem)
        mgr.distiller.merge_knowledge = AsyncMock(return_value="用户喜欢Python和React")
        mgr.distiller.distill = AsyncMock(return_value="用户也喜欢React")

        await mgr._distill_to_knowledge(raw_id, "用户也喜欢React", scope, 0.5, "")

        # 验证提炼知识被 UPDATE（合并）
        cursor = await db._conn.execute(
            "SELECT * FROM episodic_memories WHERE id=?", (existing_id,)
        )
        row = await cursor.fetchone()
        assert row["summary"] == "用户喜欢Python和React"

    async def test_distill_no_result_skips(self, distill_db):
        """蒸馏返回空时跳过（不创建提炼知识）"""
        db, mgr = distill_db
        scope = Scope()

        raw_id = await db.memory.insert_episodic_memory(
            summary="原始记录", scope=scope, is_raw=1
        )

        mgr.distiller.distill = AsyncMock(return_value="")  # 蒸馏失败

        await mgr._distill_to_knowledge(raw_id, "原始记录", scope, 0.5, "")

        # 验证没有创建 is_raw=0 的记录
        cursor = await db._conn.execute(
            "SELECT COUNT(*) as cnt FROM episodic_memories WHERE is_raw=0"
        )
        row = await cursor.fetchone()
        assert row["cnt"] == 0


class TestFindSimilarKnowledge:
    """_find_similar_knowledge: 查找相似提炼知识"""

    async def test_find_similar_exists(self, distill_db):
        """找到相似的 is_raw=0 知识"""
        db, mgr = distill_db
        scope = Scope()

        await db.memory.insert_episodic_memory(
            summary="用户喜欢Python编程语言", scope=scope, is_raw=0
        )

        similar = await mgr._find_similar_knowledge("用户喜欢Python", scope=scope)
        assert similar is not None
        assert "Python" in similar["summary"]

    async def test_find_similar_not_found(self, distill_db):
        """无相似知识返回 None"""
        db, mgr = distill_db
        scope = Scope()

        await db.memory.insert_episodic_memory(
            summary="完全不同的内容关于天气", scope=scope, is_raw=0
        )

        similar = await mgr._find_similar_knowledge("Python编程", scope=scope)
        assert similar is None

    async def test_find_similar_ignores_raw(self, distill_db):
        """只查 is_raw=0，忽略 is_raw=1"""
        db, mgr = distill_db
        scope = Scope()

        await db.memory.insert_episodic_memory(
            summary="原始记录Python", scope=scope, is_raw=1
        )

        similar = await mgr._find_similar_knowledge("原始记录Python", scope=scope)
        assert similar is None  # is_raw=1 不参与
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_distill_merge.py -v`
Expected: FAIL with "MemoryDistiller has no attribute 'merge_knowledge'" or "MemoryManager has no attribute '_distill_to_knowledge'"

- [ ] **Step 3: Write minimal implementation**

在 `memory/memory_distiller.py` 的 `distill_recall` 方法之后（约第 230 行后）新增 `merge_knowledge` 方法：

```python
    async def merge_knowledge(self, existing: str, new_content: str) -> str:
        """合并已有提炼知识和新蒸馏内容（LLM 合并，避免信息丢失）。

        用于 ADD-only 架构的 UPDATE 场景：当发现相似的提炼知识时，
        用 LLM 合并新旧内容，而不是直接覆盖。

        Args:
            existing: 已有提炼知识文本
            new_content: 新蒸馏的内容
        Returns:
            合并后的文本。LLM 失败时返回 new_content（降级：不合并，用新内容）。
        """
        if not existing or not existing.strip():
            return new_content
        if not new_content or not new_content.strip():
            return existing

        merge_prompt = f"""你是知识合并助手。将以下两段知识合并为一段简洁摘要，保留所有关键信息：

已有知识：
{existing[:300]}

新知识：
{new_content[:300]}

输出合并后的摘要（200字以内，不要重复信息）："""

        messages = [{"role": "user", "content": merge_prompt}]

        # 优先使用免费模型
        result = await self._call_free_model(
            messages, temperature=0.4, max_tokens=400,
        )
        if result is None and self.router:
            try:
                result = await self.router.route(
                    task_type="memory_encoding",
                    messages=messages,
                    temperature=0.4,
                    max_tokens=400,
                )
            except Exception as e:
                logger.warning("memory_distiller.merge_router_fallback_failed", error=str(e))
                return new_content

        if not result or not isinstance(result, str):
            # LLM 失败时降级：直接用新内容（不合并）
            return new_content

        merged = result.strip()
        # 去除可能的 <think> 标签内容
        if "<think>" in merged:
            import re
            merged = re.sub(r"<think>.*?</think>", "", merged, flags=re.DOTALL).strip()
        return merged
```

在 `memory/memory_manager.py` 的 `_extract_and_link_entities` 方法之后新增 `_distill_to_knowledge`、`_find_similar_knowledge`、`_update_knowledge` 方法：

```python
    async def _distill_to_knowledge(self, raw_id: int, summary: str,
                                     scope: Any, importance: float = 0.5,
                                     emotion: str = "") -> None:
        """将原始记忆蒸馏为提炼知识（允许 UPDATE/DELETE）。

        mem0 SPEC 优化 ADD-only 架构：
        1. 调用 MemoryDistiller 蒸馏
        2. 检查是否已有相似的提炼知识（is_raw=0, 同 scope）
        3a. 有相似 → UPDATE（合并/增强）
        3b. 无相似 → 新建提炼知识（is_raw=0）

        Args:
            raw_id: 原始记忆 ID
            summary: 原始记忆摘要
            scope: Scope 对象
            importance: 重要性
            emotion: 情感标签
        """
        if not self.distiller:
            return
        try:
            # 1. 蒸馏（调用已有 MemoryDistiller，传入单条记忆）
            distilled = await self.distiller.distill([{"summary": summary, "timestamp": time.time()}])
            if not distilled or not distilled.strip():
                return

            # 2. 检查是否已有相似的提炼知识
            similar = await self._find_similar_knowledge(distilled, scope=scope)

            if similar:
                # 3a. 有相似知识 → UPDATE（合并）
                await self._update_knowledge(similar["id"], distilled, raw_id, scope)
            else:
                # 3b. 无相似知识 → 新建提炼知识（is_raw=0）
                knowledge_id = await self.memory.insert_episodic_memory(
                    summary=distilled,
                    importance=importance,
                    emotion_label=emotion,
                    scope=scope,
                    is_raw=0,
                )
                if self.vec and knowledge_id:
                    try:
                        await self.vec.upsert(knowledge_id, distilled)
                    except Exception as e:
                        logger.debug("memory.distill_vec_upsert_failed", error=str(e))
                logger.info("memory.distilled_new",
                           raw_id=raw_id, knowledge_id=knowledge_id)
        except Exception as e:
            logger.warning("memory.distill_to_knowledge_failed", error=str(e))

    async def _find_similar_knowledge(self, summary: str,
                                       scope: Any) -> dict | None:
        """查找相似的提炼知识（is_raw=0, 同 scope）。

        Args:
            summary: 待查重的摘要
            scope: Scope 对象
        Returns:
            相似的记忆 dict，或 None
        """
        try:
            normalized = _normalize_for_dedupe(summary)
            if len(normalized) < 10:
                return None
            # FTS 搜索 is_raw=0 的提炼知识
            candidates = await self.memory.search_memories_fts_scoped(
                summary, scope=scope, limit=5, is_raw=0
            )
            for c in candidates:
                if _normalize_for_dedupe(c.get("summary", "")) == normalized:
                    return c
            # FTS 无精确匹配，检查最近记忆
            # （这里不做模糊匹配，只在精确归一化匹配时返回）
            return None
        except Exception as e:
            logger.debug("memory.find_similar_knowledge_failed", error=str(e))
            return None

    async def _update_knowledge(self, knowledge_id: int, new_content: str,
                                 raw_id: int, scope: Any) -> None:
        """更新已有提炼知识（合并新信息）。

        Args:
            knowledge_id: 提炼知识 ID
            new_content: 新蒸馏的内容
            raw_id: 原始记忆 ID（用于溯源）
            scope: Scope 对象
        """
        try:
            # 1. 获取已有知识
            existing = await self.memory.get_memory_by_id(knowledge_id)
            if not existing:
                return

            # 2. LLM 合并新旧知识
            merged = await self.distiller.merge_knowledge(
                existing=existing.get("summary", ""),
                new_content=new_content,
            )

            # 3. 更新记录（version+1）
            await self.memory.update_memory_enrichment(
                memory_id=knowledge_id,
                summary=merged,
                metadata_json=__import__("json").dumps({
                    "source_raw_ids": [raw_id],
                    "merged_at": time.time(),
                }),
            )

            # 4. 向量更新
            if self.vec:
                try:
                    await self.vec.upsert(knowledge_id, merged)
                except Exception as e:
                    logger.debug("memory.update_knowledge_vec_failed", error=str(e))

            logger.info("memory.knowledge_updated",
                       knowledge_id=knowledge_id, raw_id=raw_id)
        except Exception as e:
            logger.warning("memory.update_knowledge_failed", error=str(e))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_distill_merge.py -v`
Expected: PASS (9 tests passed)

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add memory/memory_distiller.py memory/memory_manager.py tests/test_distill_merge.py
git commit -m "feat(distill): add merge_knowledge + _distill_to_knowledge + _update_knowledge"
```

---

## Task 8: 检索流程五路 RRF + Entity Boost

**Files:**
- Modify: `memory/memory_manager.py:346-570` (`retrieve_memories_hybrid` 接入第5路 + Entity Boost)
- Modify: `memory/memory_manager.py` (新增 `_entity_recall` 协程 + `_apply_entity_boost` 方法)
- Test: `tests/test_five_path_rrf_entity_boost.py`

**Interfaces:**
- Consumes: Task 2 的 `Scope` + `search_memories_fts_scoped/search_memories_vec_scoped`；Task 4 的 `EntityExtractor.extract`；Task 5 的 `EntityStore.recall_by_entities` + `get_query_entities_boost`
- Produces: `MemoryManager.retrieve_memories_hybrid(query, k, use_reranker, use_kg, scope=None, include_raw=False)`（scope 参数可选）；`MemoryManager._entity_recall(query, scope, recall_limit) -> list[dict]`；`MemoryManager._apply_entity_boost(query, candidates, scope) -> list[dict]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_five_path_rrf_entity_boost.py
"""五路 RRF + Entity Boost 测试：第5路召回 + 精排加分"""
import asyncio
import time
import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.scope import Scope
from memory.entity_extractor import Entity
from memory.entity_store import compute_entity_boost


@pytest.fixture
async def search_db(tmp_path):
    """创建带 v13 schema + 实体链接的测试数据库"""
    from db.database import DatabaseManager
    from memory.memory_manager import MemoryManager
    db_path = tmp_path / "test_five_path.db"
    db = DatabaseManager(db_path)
    await db.init()

    mgr = MemoryManager.__new__(MemoryManager)
    mgr.db = db
    mgr.memory = db.memory
    mgr.vec = None
    mgr.kg = None
    mgr._security_filter = None
    mgr._reranker = None
    mgr._governance = None
    mgr.entity_extractor = None
    mgr.entity_store = None

    yield db, mgr
    await db.close()


class TestEntityRecall:
    """_entity_recall: 第5路召回"""

    async def test_entity_recall_returns_memories(self, search_db):
        """通过实体名反查到关联记忆"""
        db, mgr = search_db
        scope = Scope()

        # 插入带实体链接的记忆
        mem_id = await db.memory.insert_episodic_memory(
            summary="Python编程笔记", scope=scope, is_raw=0
        )
        entity_id = await db.memory.insert_memory_entity(
            name="Python", entity_type="IDENTIFIER", kind="技术"
        )
        await db.memory.insert_entity_memory_link(entity_id, mem_id)

        # mock entity_store
        mgr.entity_store = MagicMock()
        mgr.entity_store.recall_by_entities = AsyncMock(return_value=[
            {"id": mem_id, "summary": "Python编程笔记", "is_raw": 0}
        ])

        results = await mgr._entity_recall("Python", scope, recall_limit=10)
        assert len(results) == 1
        assert results[0]["id"] == mem_id

    async def test_entity_recall_no_store_returns_empty(self, search_db):
        """entity_store 为 None 时返回空列表"""
        db, mgr = search_db
        mgr.entity_store = None
        results = await mgr._entity_recall("Python", Scope(), recall_limit=10)
        assert results == []

    async def test_entity_recall_failure_returns_empty(self, search_db):
        """entity_store 调用失败时返回空列表（降级）"""
        db, mgr = search_db
        mgr.entity_store = MagicMock()
        mgr.entity_store.recall_by_entities = AsyncMock(side_effect=Exception("DB error"))
        results = await mgr._entity_recall("Python", Scope(), recall_limit=10)
        assert results == []


class TestApplyEntityBoost:
    """_apply_entity_boost: 精排阶段 Entity Boost 加分"""

    async def test_boost_increases_score(self, search_db):
        """匹配实体的候选记忆 score 提升"""
        db, mgr = search_db
        scope = Scope()

        # 插入带实体链接的记忆
        mem_id = await db.memory.insert_episodic_memory(
            summary="Python笔记", scope=scope, is_raw=0
        )
        entity_id = await db.memory.insert_memory_entity(
            name="Python", entity_type="IDENTIFIER", kind="技术"
        )
        await db.memory.insert_entity_memory_link(entity_id, mem_id)

        # mock entity_extractor 提取查询实体
        mgr.entity_extractor = MagicMock()
        mgr.entity_extractor.extract = AsyncMock(return_value=[
            Entity(name="Python", entity_type="IDENTIFIER")
        ])

        candidates = [
            {"id": mem_id, "summary": "Python笔记", "rrf_score": 0.5},
            {"id": 999, "summary": "无关记忆", "rrf_score": 0.6},
        ]

        boosted = await mgr._apply_entity_boost("Python", candidates, scope)
        # 带 Python 实体的记忆 score 应该被提升
        python_item = next(c for c in boosted if c["id"] == mem_id)
        other_item = next(c for c in boosted if c["id"] == 999)
        assert python_item["rrf_score"] > 0.5  # 被提升了
        assert other_item["rrf_score"] == 0.6  # 未变

    async def test_boost_no_match_unchanged(self, search_db):
        """无实体匹配时 score 不变"""
        db, mgr = search_db
        scope = Scope()

        mgr.entity_extractor = MagicMock()
        mgr.entity_extractor.extract = AsyncMock(return_value=[
            Entity(name="Java", entity_type="IDENTIFIER")
        ])
        mgr.entity_store = MagicMock()
        mgr.entity_store.get_query_entities_boost = AsyncMock(return_value=0.0)

        candidates = [
            {"id": 1, "summary": "记忆1", "rrf_score": 0.5},
        ]
        boosted = await mgr._apply_entity_boost("Java", candidates, scope)
        assert boosted[0]["rrf_score"] == 0.5  # 未变

    async def test_boost_no_extractor_unchanged(self, search_db):
        """entity_extractor 为 None 时不加 boost"""
        db, mgr = search_db
        mgr.entity_extractor = None
        candidates = [{"id": 1, "rrf_score": 0.5}]
        boosted = await mgr._apply_entity_boost("Python", candidates, Scope())
        assert boosted[0]["rrf_score"] == 0.5


class TestRetrieveMemoriesHybridScoped:
    """retrieve_memories_hybrid + scope 过滤"""

    async def test_retrieve_scoped(self, search_db):
        """scope 过滤：只返回当前 scope 的记忆"""
        db, mgr = search_db
        scope_alice = Scope(user_id="alice", agent_id="xiaoli")
        scope_bob = Scope(user_id="bob", agent_id="xiaoke")

        # alice 和 bob 各有一条记忆
        await db.memory.insert_episodic_memory(
            summary="alice的Python笔记", scope=scope_alice, is_raw=0
        )
        await db.memory.insert_episodic_memory(
            summary="bob的Java笔记", scope=scope_bob, is_raw=0
        )

        # mock 冷启动为 hot（走完整检索）
        mgr.get_memory_tier = AsyncMock(return_value="hot")
        mgr._extract_deterministic_selectors = MagicMock(return_value={})
        mgr._get_candidate_ids_by_selectors = AsyncMock(return_value=None)
        mgr._hybrid_fts_search = AsyncMock(return_value=[])
        mgr._hybrid_vec_search = AsyncMock(return_value=[])
        mgr.invalidate_memory_count_cache = MagicMock()

        # alice scope 检索
        results = await mgr.retrieve_memories_hybrid("Python", k=5, scope=scope_alice)
        # 应只返回 alice 的记忆
        for r in results:
            assert r["user_id"] == "alice"

    async def test_retrieve_include_raw(self, search_db):
        """include_raw=True 时查所有记忆（含 is_raw=1）"""
        db, mgr = search_db
        scope = Scope()

        await db.memory.insert_episodic_memory(
            summary="提炼知识", scope=scope, is_raw=0
        )
        await db.memory.insert_episodic_memory(
            summary="原始记录", scope=scope, is_raw=1
        )

        mgr.get_memory_tier = AsyncMock(return_value="cold")
        mgr._hybrid_fts_search = AsyncMock(return_value=[
            {"id": 1, "summary": "提炼知识", "is_raw": 0, "user_id": "default", "agent_id": "xiaoda"},
            {"id": 2, "summary": "原始记录", "is_raw": 1, "user_id": "default", "agent_id": "xiaoda"},
        ])
        mgr.invalidate_memory_count_cache = MagicMock()

        # include_raw=True: 应返回所有
        results = await mgr.retrieve_memories_hybrid(
            "记忆", k=5, scope=scope, include_raw=True
        )
        assert len(results) >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_five_path_rrf_entity_boost.py -v`
Expected: FAIL with "retrieve_memories_hybrid() got an unexpected keyword argument 'scope'" or "MemoryManager has no attribute '_entity_recall'"

- [ ] **Step 3: Write minimal implementation**

在 `memory/memory_manager.py` 的 `_extract_and_link_entities` 方法之后新增 `_entity_recall` 和 `_apply_entity_boost` 方法：

```python
    async def _entity_recall(self, query: str, scope: Any,
                              recall_limit: int = 50) -> list[dict]:
        """第5路召回：通过实体名反查记忆（mem0 SPEC 优化）。

        流程：
        1. EntityExtractor 提取查询中的实体名（jieba 快抽，<10ms）
        2. EntityStore.recall_by_entities 反查关联记忆

        Args:
            query: 用户查询
            scope: Scope 对象
            recall_limit: 召回上限
        Returns:
            记忆 dict 列表。失败返回空列表（降级）。
        """
        if not self.entity_store or not self.entity_extractor:
            return []
        try:
            # 1. 提取查询实体（jieba 快抽，不触发 LLM）
            entities = self.entity_extractor._rule_based_extract(query)
            if not entities:
                return []
            entity_names = [e.name for e in entities]
            # 2. 反查关联记忆
            results = await self.entity_store.recall_by_entities(
                entity_names, scope=scope, limit=recall_limit, is_raw=0
            )
            for r in results:
                r["entity_recall"] = True
            return results
        except Exception as e:
            logger.debug("memory.entity_recall_failed", error=str(e))
            return []

    async def _apply_entity_boost(self, query: str, candidates: list[dict],
                                   scope: Any) -> list[dict]:
        """精排阶段计算 Entity Boost 并加分（mem0 SPEC 优化）。

        对每个候选记忆，计算其关联实体与查询实体的 boost 值，
        加到 rrf_score 上。

        Args:
            query: 用户查询
            candidates: 候选记忆列表（含 rrf_score）
            scope: Scope 对象
        Returns:
            加分后的候选列表（按 rrf_score 降序）
        """
        if not self.entity_extractor or not self.entity_store:
            return candidates
        if not candidates:
            return candidates
        try:
            # 1. 提取查询实体
            query_entities_list = await self.entity_extractor.extract(query, importance=0.3)
            query_entity_names = {e.name for e in query_entities_list}
            if not query_entity_names:
                return candidates

            # 2. 对每个候选计算 Entity Boost
            now = time.time()
            for candidate in candidates:
                mem_id = candidate.get("id")
                if mem_id is None:
                    continue
                boost = await self.entity_store.get_query_entities_boost(
                    mem_id, query_entity_names, now=now
                )
                if boost > 0:
                    candidate["rrf_score"] = candidate.get("rrf_score", 0.0) + boost
                    candidate["entity_boost"] = boost

            # 3. 按 rrf_score 降序重新排序
            candidates.sort(key=lambda x: x.get("rrf_score", 0.0), reverse=True)
            return candidates
        except Exception as e:
            logger.debug("memory.apply_entity_boost_failed", error=str(e))
            return candidates
```

修改 `memory/memory_manager.py:346` 的 `retrieve_memories_hybrid` 方法签名，添加 scope 和 include_raw 参数：

```python
    async def retrieve_memories_hybrid(self, query: str, k: int = 5,
                                        use_reranker: bool = True,
                                        use_kg: bool = True,
                                        scope: Any | None = None,
                                        include_raw: bool = False) -> list[dict]:
        """FTS + 向量 + KG + 子chunk + 实体 五路 RRF 混合检索 + Reranker 精排

        mem0 SPEC 优化：
        - 新增第5路：EntityStore.recall_by_entities
        - 新增 Entity Boost：精排阶段加分
        - 新增 scope 过滤：user_id + agent_id 隔离
        - 新增 include_raw：是否包含 is_raw=1 的原始记忆

        Args:
            scope: Scope 对象。None 时使用默认 Scope()。
            include_raw: False=只查提炼知识（is_raw=0），True=查所有记忆
            use_reranker: 是否调用 Reranker 精排
            use_kg: 是否启用 KG 第三路召回
        """
        # scope 默认值
        if scope is None:
            from memory.scope import Scope
            scope = Scope()

        _start = time.time()
        is_raw_filter = None if include_raw else 0

        # 候选集大小参数化
        import config as _cfg
        recall_limit = getattr(_cfg, 'RAG_RECALL_LIMIT', 50)
        rerank_limit = getattr(_cfg, 'RAG_RERANK_LIMIT', 50)

        # ── 冷启动路由: 判断用户记忆档位 ──
        tier = await self.get_memory_tier()
        is_cold = tier == "cold"
        is_warm = tier == "warm"

        # 冷用户: 仅 FTS（scope 过滤）
        if is_cold:
            fts_items = await self.memory.search_memories_fts_scoped(
                query, scope=scope, limit=recall_limit * 2, is_raw=is_raw_filter
            )
            if fts_items:
                results = fts_items[:k]
                logger.info("memory.search", event="memory_search",
                            query=query[:100], tier="cold", results=len(results),
                            duration_ms=int((time.time() - _start) * 1000))
                return results
            # FTS 无结果，尝试向量兜底
            vec_items = await self._hybrid_vec_search(query, recall_limit)
            if vec_items:
                logger.info("memory.search", event="memory_search",
                            query=query[:100], tier="cold+vec_fallback", results=len(vec_items),
                            duration_ms=int((time.time() - _start) * 1000))
                return vec_items[:k]
            logger.info("memory.search", event="memory_search",
                        query=query[:100], tier="cold", results=0,
                        duration_ms=int((time.time() - _start) * 1000))
            return []

        # ── 温/热用户: 并行执行五路检索 ──
        selectors = self._extract_deterministic_selectors(query)
        candidate_ids = await self._get_candidate_ids_by_selectors(
            selectors, limit=recall_limit * 6)

        # KG 召回协程
        async def _kg_recall() -> list[dict]:
            if not self.kg or not use_kg:
                return []
            try:
                related_names = await self.kg.recall_by_query(query, limit=recall_limit)
                if not related_names:
                    return []
                return await self.memory.search_memories_by_entities(
                    related_names, limit=recall_limit)
            except Exception as e:
                logger.debug("memory.kg_recall_failed", error=str(e))
                return []

        # 子chunk召回协程
        async def _child_recall() -> list[dict]:
            import config as _child_cfg
            if not getattr(_child_cfg, 'PARENT_CHILD_CHUNK_ENABLED', True):
                return []
            try:
                async def _child_vec_recall() -> list[int]:
                    if not self.vec or not self.vec.enabled:
                        return []
                    query_vec = await self.vec.embed(query)
                    if not query_vec:
                        return []
                    results = await self.vec.search_child(query_vec, top_k=recall_limit)
                    if not results:
                        return []
                    child_ids = [r["id"] for r in results]
                    return await self.memory.get_child_parent_ids(child_ids)

                child_fts_results, child_vec_parent_ids = await asyncio.gather(
                    self.memory.search_child_fts(query, recall_limit),
                    _child_vec_recall(),
                )
                parent_ids: set[int] = set()
                for r in child_fts_results:
                    parent_ids.add(r["parent_id"])
                for pid in child_vec_parent_ids:
                    parent_ids.add(pid)
                if not parent_ids:
                    return []
                parent_mems = await self.memory.get_memories_by_ids(list(parent_ids))
                for pm in parent_mems:
                    pm["child_recall"] = True
                return parent_mems
            except Exception as e:
                logger.debug("memory.child_recall_failed", error=str(e))
                return []

        # 五路并行检索（FTS+向量+KG+子chunk+实体）
        fts_items, vec_items, kg_items, child_items, entity_items = await asyncio.gather(
            self._hybrid_fts_search_scoped(query, recall_limit, scope, is_raw_filter),
            self._hybrid_vec_search(query, recall_limit, candidate_ids=candidate_ids),
            _kg_recall(),
            _child_recall(),
            self._entity_recall(query, scope, recall_limit),
        )

        # 空通道自动剔除
        if not any([fts_items, vec_items, kg_items, child_items, entity_items]):
            logger.info("memory.search", event="memory_search",
                        query=query[:100], tier=tier, results=0,
                        duration_ms=int((time.time() - _start) * 1000))
            return []

        # ── 加权 RRF 融合（五路）──
        try:
            warm_vec_weight = getattr(_cfg, "MEMORY_WARM_VEC_WEIGHT", 0.2)
        except (ImportError, AttributeError):
            warm_vec_weight = 0.2
        if is_warm:
            fts_weight, vec_weight = 1.0, warm_vec_weight
        else:
            fts_weight, vec_weight = 1.0, 1.0

        oversample_k = rerank_limit
        fts_ids = [str(item["id"]) for item in fts_items]
        vec_ids = [str(item["id"]) for item in vec_items]
        ranked_lists = [fts_ids, vec_ids]
        weights = [fts_weight, vec_weight]
        if kg_items:
            for _kitem in kg_items:
                _kitem["kg_recall"] = True
            ranked_lists.append([str(item["id"]) for item in kg_items])
            weights.append(0.8)
        if child_items:
            ranked_lists.append([str(item["id"]) for item in child_items])
            weights.append(0.9)
        if entity_items:
            ranked_lists.append([str(item["id"]) for item in entity_items])
            weights.append(0.7)  # 实体召回权重

        fused = reciprocal_rank_fusion(
            ranked_lists, limit=oversample_k, weights=weights,
        )

        # 按 RRF 排序获取完整记录
        all_items = {str(item["id"]): item for item in
                     fts_items + vec_items + kg_items + child_items + entity_items}

        # ── mem0 SPEC: Entity Boost 精排加分 ──
        candidates = []
        for item_id, rrf_score in fused:
            if item_id in all_items:
                item = all_items[item_id]
                item["rrf_score"] = rrf_score
                candidates.append(item)
        candidates = await self._apply_entity_boost(query, candidates, scope)

        # Reranker 精排
        if use_reranker and self._reranker and self._reranker.available and len(candidates) > k:
            reranked = await self._hybrid_rerank(query, fused, all_items, k)
            if reranked:
                # 对 reranked 也应用 entity boost
                reranked = await self._apply_entity_boost(query, reranked, scope)
                results = reranked[:k]
                logger.info("memory.search", event="memory_search",
                            query=query[:100], tier=tier, results=len(results),
                            duration_ms=int((time.time() - _start) * 1000))
                return results

        final = candidates[:k]
        logger.info("memory.search", event="memory_search",
                    query=query[:100], tier=tier, results=len(final),
                    duration_ms=int((time.time() - _start) * 1000))
        return final

    async def _hybrid_fts_search_scoped(self, query: str, k: int,
                                         scope: Any, is_raw: int | None) -> list[dict]:
        """FTS 检索 + scope 过滤（mem0 SPEC 优化）"""
        if not self.memory:
            return []
        try:
            return await self.memory.search_memories_fts_scoped(
                query, scope=scope, limit=k * 2, is_raw=is_raw
            )
        except Exception as e:
            logger.warning("memory.fts_scoped_search_failed", error=str(e))
            return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_five_path_rrf_entity_boost.py -v`
Expected: PASS (7 tests passed)

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add memory/memory_manager.py tests/test_five_path_rrf_entity_boost.py
git commit -m "feat(search): five-path RRF + Entity Boost + scope filter in retrieve_memories_hybrid"
```

---

## Task 9: 时间感知增强

**Files:**
- Modify: `memory/memory_manager.py` (`_try_temporal_search` 加入 scope 过滤)
- Modify: `memory/entity_store.py` (link_entities 更新 last_seen — 已在 Task 5 实现)
- Test: `tests/test_temporal_scope_enhancement.py`

**Interfaces:**
- Consumes: Task 2 的 `Scope` + `search_memories_by_time_scoped`；Task 5 的 `EntityStore.link_entities`（已更新 last_seen）
- Produces: `MemoryManager._try_temporal_search(query, scope, ...) -> list[dict]`（加入 scope 过滤）

- [ ] **Step 1: Write the failing test**

```python
# tests/test_temporal_scope_enhancement.py
"""时间感知增强测试：_try_temporal_search 加入 scope 过滤 + EntityStore last_seen 更新"""
import asyncio
import time
import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.scope import Scope
from memory.entity_extractor import Entity
from memory.entity_store import EntityStore, compute_entity_boost


@pytest.fixture
async def temporal_db(tmp_path):
    """创建带 v13 schema 的测试数据库"""
    from db.database import DatabaseManager
    db_path = tmp_path / "test_temporal.db"
    db = DatabaseManager(db_path)
    await db.init()
    yield db
    await db.close()


class TestTemporalSearchScoped:
    """_try_temporal_search 加入 scope 过滤"""

    async def test_temporal_search_scope_filtered(self, temporal_db):
        """时间检索只返回当前 scope 的记忆"""
        db = temporal_db
        scope_alice = Scope(user_id="alice", agent_id="xiaoli")
        scope_bob = Scope(user_id="bob", agent_id="xiaoke")
        now = time.time()

        # alice 和 bob 各有一条昨天的记忆
        yesterday = now - 86400
        await db.memory.insert_episodic_memory(
            summary="alice昨天的记忆", scope=scope_alice, is_raw=0
        )
        # 手动更新时间戳为昨天
        cursor = await db._conn.execute(
            "UPDATE episodic_memories SET timestamp=? WHERE user_id='alice'",
            (yesterday,),
        )
        await db._conn.commit()

        await db.memory.insert_episodic_memory(
            summary="bob昨天的记忆", scope=scope_bob, is_raw=0
        )
        cursor = await db._conn.execute(
            "UPDATE episodic_memories SET timestamp=? WHERE user_id='bob'",
            (yesterday,),
        )
        await db._conn.commit()

        # alice scope 查询昨天的记忆
        from memory.memory_manager import MemoryManager
        mgr = MemoryManager.__new__(MemoryManager)
        mgr.memory = db.memory

        results = await mgr._try_temporal_search("昨天发生了什么", scope=scope_alice)
        # 应只返回 alice 的记忆
        for r in results:
            assert r["user_id"] == "alice"

    async def test_temporal_search_no_time_word(self, temporal_db):
        """无时间词返回空"""
        from memory.memory_manager import MemoryManager
        db = temporal_db
        mgr = MemoryManager.__new__(MemoryManager)
        mgr.memory = db.memory
        results = await mgr._try_temporal_search("Python编程", scope=Scope())
        assert results == []

    async def test_temporal_search_is_raw_filter(self, temporal_db):
        """时间检索默认只查 is_raw=0"""
        db = temporal_db
        scope = Scope()
        yesterday = time.time() - 86400

        # 插入一条 is_raw=0 和一条 is_raw=1
        mem_raw = await db.memory.insert_episodic_memory(
            summary="原始记录昨天", scope=scope, is_raw=1
        )
        mem_refined = await db.memory.insert_episodic_memory(
            summary="提炼知识昨天", scope=scope, is_raw=0
        )
        # 手动更新时间戳
        await db._conn.execute(
            "UPDATE episodic_memories SET timestamp=? WHERE id IN (?, ?)",
            (yesterday, mem_raw, mem_refined),
        )
        await db._conn.commit()

        from memory.memory_manager import MemoryManager
        mgr = MemoryManager.__new__(MemoryManager)
        mgr.memory = db.memory

        # 默认 include_raw=False
        results = await mgr._try_temporal_search("昨天", scope=scope)
        # 应只返回 is_raw=0
        for r in results:
            assert r["is_raw"] == 0


class TestEntityLastSeenUpdate:
    """EntityStore 链接时更新 last_seen（时间感知）"""

    async def test_link_updates_last_seen(self, temporal_db):
        """链接实体时 last_seen 被更新为当前时间"""
        db = temporal_db
        store = EntityStore(db.memory)

        # 先创建实体（设置旧的 last_seen）
        entity_id = await db.memory.insert_memory_entity(
            name="OldTopic", entity_type="TOPIC", kind="概念"
        )
        old_ts = time.time() - 86400 * 7  # 7天前
        await db.memory.update_entity_last_seen(entity_id, old_ts)

        # 链接到新记忆
        mem_id = await db.memory.insert_episodic_memory(summary="新记忆")
        await store.link_entities(
            mem_id, [Entity(name="OldTopic", entity_type="TOPIC")], scope=Scope()
        )

        entity = await db.memory.find_memory_entity_by_id(entity_id)
        assert entity["last_seen"] > old_ts

    async def test_boost_recency_decay_integration(self, temporal_db):
        """Entity Boost 时间衰减集成测试"""
        db = temporal_db
        now = time.time()

        # 最近实体（今天）
        entity_recent = {"name": "Python", "memory_count": 5, "last_seen": now}
        # 旧实体（30天前）
        entity_old = {"name": "Python", "memory_count": 5, "last_seen": now - 86400 * 30}

        query_entities = {"Python"}
        boost_recent = compute_entity_boost(entity_recent, query_entities, now=now)
        boost_old = compute_entity_boost(entity_old, query_entities, now=now)

        # 最近的 boost 应该明显高于旧的
        assert boost_recent > boost_old
        assert boost_recent > 0
        assert boost_old > 0  # 旧的不为0，只是衰减
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_temporal_scope_enhancement.py -v`
Expected: FAIL with "_try_temporal_search() got an unexpected keyword argument 'scope'"

- [ ] **Step 3: Write minimal implementation**

在 `memory/memory_manager.py` 中找到 `_try_temporal_search` 方法，修改为加入 scope 过滤。在 `_apply_entity_boost` 方法之后新增：

```python
    async def _try_temporal_search(self, query: str, scope: Any | None = None,
                                    limit: int = 10,
                                    include_raw: bool = False) -> list[dict]:
        """按时间范围检索记忆（加入 scope 过滤，mem0 SPEC 优化）。

        解析查询中的时间词（昨天/前天/上周等），按时间范围检索。
        加入 scope 过滤：只返回当前 user_id + agent_id 的记忆。

        Args:
            query: 用户查询
            scope: Scope 对象。None 时使用默认 Scope()。
            limit: 返回条数上限
            include_raw: False=只查 is_raw=0，True=查所有
        Returns:
            记忆 dict 列表。无时间词返回空。
        """
        if scope is None:
            from memory.scope import Scope
            scope = Scope()

        time_range = _parse_temporal_query(query)
        if not time_range:
            return []

        is_raw_filter = None if include_raw else 0
        try:
            return await self.memory.search_memories_by_time_scoped(
                time_range[0], time_range[1], scope=scope,
                limit=limit, is_raw=is_raw_filter,
            )
        except Exception as e:
            logger.debug("memory.temporal_search_scoped_failed", error=str(e))
            return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_temporal_scope_enhancement.py -v`
Expected: PASS (5 tests passed)

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add memory/memory_manager.py tests/test_temporal_scope_enhancement.py
git commit -m "feat(temporal): add scope filter to _try_temporal_search + verify last_seen update"
```

---

## Task 10: 端到端集成测试

**Files:**
- Create: `tests/test_mem0_optimization_e2e.py`
- Test: `tests/test_mem0_optimization_e2e.py`

**Interfaces:**
- Consumes: Task 1-9 所有组件
- Produces: 端到端测试验证完整流程：编码→提取→链接→蒸馏→检索→Boost

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mem0_optimization_e2e.py
"""端到端集成测试：验证 mem0 SPEC 优化完整流程

流程：编码 → 提取实体 → 蒸馏 → 检索 → Entity Boost
"""
import asyncio
import time
import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.scope import Scope
from memory.entity_extractor import EntityExtractor, Entity
from memory.entity_store import EntityStore
from memory.memory_distiller import MemoryDistiller


@pytest.fixture
async def e2e_db(tmp_path):
    """创建带 v13 schema 的完整测试环境"""
    from db.database import DatabaseManager
    from memory.memory_manager import MemoryManager
    db_path = tmp_path / "test_e2e.db"
    db = DatabaseManager(db_path)
    await db.init()

    mgr = MemoryManager.__new__(MemoryManager)
    mgr.db = db
    mgr.memory = db.memory
    mgr.vec = None
    mgr.kg = None
    mgr._security_filter = None
    mgr._reranker = None
    mgr._governance = None
    mgr._last_encode_time = 0
    mgr._pending_encode = False
    mgr._last_message_time = time.time()
    mgr.entity_extractor = EntityExtractor(router=None)
    mgr.entity_store = EntityStore(db.memory)
    mgr.distiller = MemoryDistiller(router=None)

    yield db, mgr
    await db.close()


class TestEndToEndMemoryFlow:
    """端到端：编码 → 提取 → 链接 → 蒸馏 → 检索 → Boost"""

    async def test_full_flow_with_entity_boost(self, e2e_db):
        """完整流程：用户说喜欢Python → 编码 → 提取实体 → 检索时 Boost 生效"""
        db, mgr = e2e_db
        scope = Scope(user_id="alice", agent_id="xiaoli")

        # mock 辅助方法
        mgr._generate_summary = MagicMock(return_value="用户说: 我喜欢Python编程语言")
        mgr._estimate_importance = MagicMock(return_value=0.8)
        mgr._save_state_json = MagicMock()
        mgr.invalidate_memory_count_cache = MagicMock()
        mgr._enrich_memory_async = AsyncMock()

        # 1. 编码记忆
        context = {
            "exchanges": [
                {"role": "user", "content": "我喜欢Python编程语言"},
                {"role": "assistant", "content": "好的，记下了"},
            ],
            "emotion": {"primary": "开心"},
        }
        await mgr.encode_memory(context, scope=scope)

        # 等待异步任务完成
        await asyncio.sleep(0.2)

        # 2. 验证原始记忆写入（is_raw=1）
        cursor = await db._conn.execute(
            "SELECT * FROM episodic_memories WHERE is_raw=1 AND user_id='alice'"
        )
        raw_rows = await cursor.fetchall()
        assert len(raw_rows) >= 1

        # 3. 验证实体提取（Python → IDENTIFIER）
        cursor = await db._conn.execute(
            "SELECT * FROM memory_entities WHERE name='Python'"
        )
        entity_rows = await cursor.fetchall()
        assert len(entity_rows) >= 1

        # 4. 验证实体链接（entity_memory_links 有记录）
        cursor = await db._conn.execute(
            "SELECT * FROM entity_memory_links WHERE entity_id=?",
            (entity_rows[0]["id"],),
        )
        link_rows = await cursor.fetchall()
        assert len(link_rows) >= 1

        # 5. 检索 "Python" 相关记忆（第5路召回应生效）
        mgr.get_memory_tier = AsyncMock(return_value="cold")
        results = await mgr.retrieve_memories_hybrid("Python", k=5, scope=scope)
        assert len(results) >= 1

    async def test_scope_isolation_e2e(self, e2e_db):
        """scope 隔离：不同用户的记忆互不串"""
        db, mgr = e2e_db
        scope_alice = Scope(user_id="alice", agent_id="xiaoli")
        scope_bob = Scope(user_id="bob", agent_id="xiaoke")

        mgr._generate_summary = MagicMock(side_effect=[
            "alice说: 我喜欢Python",
            "bob说: 我喜欢Java",
        ])
        mgr._estimate_importance = MagicMock(return_value=0.5)
        mgr._save_state_json = MagicMock()
        mgr.invalidate_memory_count_cache = MagicMock()
        mgr._enrich_memory_async = AsyncMock()

        # alice 编码 Python 记忆
        await mgr.encode_memory({
            "exchanges": [
                {"role": "user", "content": "我喜欢Python"},
                {"role": "assistant", "content": "好的"},
            ],
        }, scope=scope_alice)

        # bob 编码 Java 记忆
        await mgr.encode_memory({
            "exchanges": [
                {"role": "user", "content": "我喜欢Java"},
                {"role": "assistant", "content": "好的"},
            ],
        }, scope=scope_bob)

        await asyncio.sleep(0.2)

        # alice 检索：不应看到 bob 的记忆
        mgr.get_memory_tier = AsyncMock(return_value="cold")
        results_alice = await mgr.retrieve_memories_hybrid("编程", k=5, scope=scope_alice)
        for r in results_alice:
            assert r["user_id"] == "alice"
            assert r["agent_id"] == "xiaoli"

        # bob 检索：不应看到 alice 的记忆
        results_bob = await mgr.retrieve_memories_hybrid("编程", k=5, scope=scope_bob)
        for r in results_bob:
            assert r["user_id"] == "bob"
            assert r["agent_id"] == "xiaoke"

    async def test_add_only_no_dedup_e2e(self, e2e_db):
        """ADD-only：连续编码相同内容都写入（不去重）"""
        db, mgr = e2e_db
        scope = Scope()

        mgr._generate_summary = MagicMock(return_value="重复的内容：我喜欢Python")
        mgr._estimate_importance = MagicMock(return_value=0.5)
        mgr._save_state_json = MagicMock()
        mgr.invalidate_memory_count_cache = MagicMock()
        mgr._enrich_memory_async = AsyncMock()

        context = {
            "exchanges": [
                {"role": "user", "content": "我喜欢Python"},
                {"role": "assistant", "content": "好的"},
            ],
        }

        # 连续三次编码相同内容
        await mgr.encode_memory(context, scope=scope)
        await mgr.encode_memory(context, scope=scope)
        await mgr.encode_memory(context, scope=scope)

        # 验证三条 is_raw=1 记录都存在
        cursor = await db._conn.execute(
            "SELECT COUNT(*) as cnt FROM episodic_memories WHERE is_raw=1 AND summary='重复的内容：我喜欢Python'"
        )
        row = await cursor.fetchone()
        assert row["cnt"] == 3

    async def test_entity_recall_fifth_path_e2e(self, e2e_db):
        """第5路召回：通过实体反查到记忆"""
        db, mgr = e2e_db
        scope = Scope()

        # 手动插入提炼知识 + 实体链接
        mem_id = await db.memory.insert_episodic_memory(
            summary="Python编程技巧", scope=scope, is_raw=0
        )
        entity_id = await db.memory.insert_memory_entity(
            name="Python", entity_type="IDENTIFIER", kind="技术"
        )
        await db.memory.insert_entity_memory_link(entity_id, mem_id)

        # mock hot 档位走五路检索
        mgr.get_memory_tier = AsyncMock(return_value="hot")
        mgr._extract_deterministic_selectors = MagicMock(return_value={})
        mgr._get_candidate_ids_by_selectors = AsyncMock(return_value=None)
        mgr._hybrid_fts_search_scoped = AsyncMock(return_value=[])
        mgr._hybrid_vec_search = AsyncMock(return_value=[])
        mgr.invalidate_memory_count_cache = MagicMock()

        # 检索 "Python"
        results = await mgr.retrieve_memories_hybrid("Python", k=5, scope=scope)

        # 第5路应召回 Python 相关记忆
        assert len(results) >= 1
        assert any(r["id"] == mem_id for r in results)
        # 验证 entity_recall 标记
        assert any(r.get("entity_recall") for r in results)

    async def test_distillation_creates_refined_knowledge_e2e(self, e2e_db):
        """蒸馏：原始记忆 → 提炼知识"""
        db, mgr = e2e_db
        scope = Scope()

        mgr._generate_summary = MagicMock(return_value="原始：用户喜欢Python编程")
        mgr._estimate_importance = MagicMock(return_value=0.8)
        mgr._save_state_json = MagicMock()
        mgr.invalidate_memory_count_cache = MagicMock()
        mgr._enrich_memory_async = AsyncMock()

        # mock distiller 返回蒸馏结果
        mgr.distiller.distill = AsyncMock(return_value="用户喜欢Python编程（蒸馏）")
        mgr._find_similar_knowledge = AsyncMock(return_value=None)

        await mgr.encode_memory({
            "exchanges": [
                {"role": "user", "content": "我喜欢Python"},
                {"role": "assistant", "content": "好的"},
            ],
        }, scope=scope)

        # 等待异步蒸馏完成
        await asyncio.sleep(0.3)

        # 验证有 is_raw=0 的提炼知识
        cursor = await db._conn.execute(
            "SELECT * FROM episodic_memories WHERE is_raw=0 AND user_id='default'"
        )
        refined_rows = await cursor.fetchall()
        assert len(refined_rows) >= 1
        assert "蒸馏" in refined_rows[0]["summary"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_mem0_optimization_e2e.py -v`
Expected: FAIL（依赖前序任务未全部实现时部分测试失败）

- [ ] **Step 3: Run full test suite to verify no regression**

在前序 Task 1-9 全部实现完成后，端到端测试应自动通过（无需额外实现代码）。

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_mem0_optimization_e2e.py -v`
Expected: PASS (5 tests passed)

- [ ] **Step 4: Run full regression test suite**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/ -v --tb=short 2>&1 | tail -30`
Expected: 全量测试通过（现有 1311 + 新增 ~50 = ~1361 passed），无回归

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add tests/test_mem0_optimization_e2e.py
git commit -m "test(e2e): add end-to-end integration tests for mem0 SPEC optimization"
```

---

## Self-Review

### 1. Spec 覆盖检查

| 设计文档章节 | 对应任务 | 状态 |
|------------|---------|------|
| §3 数据库 Schema 设计（3字段+3表+索引） | Task 1 | ✅ |
| §3.9 数据迁移策略（回填默认值） | Task 1 Step 3 | ✅ |
| §6 Scope 三级隔离 | Task 2 | ✅ |
| §6.3 检索时的 Scope 过滤逻辑 | Task 2 + Task 8 | ✅ |
| §3.3-3.5 memory_entities + entity_memory_links | Task 1 + Task 3 | ✅ |
| §4.1 EntityExtractor 混合实体提取 | Task 4 | ✅ |
| §4.2 EntityStore 实体存储与链接 | Task 5 | ✅ |
| §4.3 Entity Boost 计算公式 | Task 5 | ✅ |
| §4.4 检索流程集成（五路 RRF + Boost） | Task 8 | ✅ |
| §5 ADD-only 混合架构 | Task 6 | ✅ |
| §5.3 蒸馏流程变更 | Task 7 | ✅ |
| §5.4 提炼知识的 UPDATE 策略 | Task 7 | ✅ |
| §5.5 检索时的 is_raw 过滤 | Task 8 | ✅ |
| §5.6 _has_duplicate 改为只对 is_raw=0 生效 | Task 6 | ✅ |
| §7 时间感知增强 | Task 9 | ✅ |
| §8 错误处理和降级策略 | 所有任务（try-except + logger.debug） | ✅ |
| §9 测试策略（单元+集成+回归） | Task 1-10 | ✅ |

### 2. Placeholder 扫描

- ✅ 无 TBD / TODO / "implement later"
- ✅ 无 "Add appropriate error handling"（所有错误处理都有具体代码）
- ✅ 无 "Similar to Task N"（每个任务代码完整）
- ✅ 无 "Write tests for the above"（每个测试都有完整代码）

### 3. 类型一致性检查

| 接口 | 定义任务 | 使用任务 | 一致性 |
|------|---------|---------|--------|
| `Scope` dataclass | Task 2 | Task 5,6,7,8,9,10 | ✅ user_id/session_id/agent_id |
| `Entity` dataclass | Task 4 | Task 5,6,10 | ✅ name/entity_type/kind/confidence |
| `EntityExtractor.extract(text, importance)` | Task 4 | Task 6,8 | ✅ |
| `EntityStore.link_entities(memory_id, entities, scope)` | Task 5 | Task 6,10 | ✅ |
| `EntityStore.recall_by_entities(names, scope, limit)` | Task 5 | Task 8 | ✅ |
| `EntityStore.get_query_entities_boost(mem_id, names, now)` | Task 5 | Task 8 | ✅ |
| `compute_entity_boost(entity, query_entities, now)` | Task 5 | Task 8,9 | ✅ |
| `MemoryDistiller.merge_knowledge(existing, new_content)` | Task 7 | Task 7 | ✅ |
| `MemoryManager.encode_memory(context, scope)` | Task 6 | Task 10 | ✅ |
| `MemoryManager.retrieve_memories_hybrid(query, k, scope, include_raw)` | Task 8 | Task 10 | ✅ |
| `MemoryManager._has_duplicate(summary, scope)` | Task 6 | Task 7 | ✅ |
| `MemoryDB.insert_episodic_memory(summary, scope, is_raw)` | Task 2 | Task 6,7,8,10 | ✅ |
| `MemoryDB.search_memories_fts_scoped(query, scope, limit, is_raw)` | Task 2 | Task 6,7,8 | ✅ |
| `MemoryDB.search_memories_by_time_scoped(start, end, scope, limit, is_raw)` | Task 2 | Task 9 | ✅ |
| `MemoryDB.get_memories_by_entity_names_scoped(names, scope, limit, is_raw)` | Task 3 | Task 5 | ✅ |

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-07-11-mem0-spec-optimization.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
