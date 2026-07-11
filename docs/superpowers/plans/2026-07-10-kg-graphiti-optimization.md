# KG Graphiti Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a graphiti-style temporal knowledge graph (KG v2) for xiaoda-agent, adding fact supersession, entity evolution, episode provenance, hybrid retrieval, and community detection — all alongside the existing KG v1 with a feature flag.

**Architecture:** Parallel v2 tables (kg_entities_v2, kg_relations_v2, kg_episodes, kg_communities, kg_edge_episode_refs) coexist with legacy v1 tables. A new KnowledgeDBV2 handles CRUD, KnowledgeGraphV2 handles episode ingestion + fact supersession + entity evolution + community detection, and KGSearchEngine fuses semantic + fulltext + graph search via RRF. A KG_V2_ENABLED feature flag controls whether the v2 path is active, with automatic fallback to v1.

**Tech Stack:** Python 3.11, asyncio, aiosqlite, SQLite (FTS5 + sqlite-vec), pytest + pytest-asyncio, BAAI/bge-m3 embeddings (1024-dim).

## Global Constraints

- Python 3.11 + asyncio + aiosqlite; all DB operations are async.
- Database migrations use the `_migrate_vN()` pattern in `db/database.py`; current schema version is 13, target is 14.
- All `CREATE TABLE` uses `IF NOT EXISTS`; all data migration uses `INSERT OR IGNORE` / `WHERE NOT EXISTS` for idempotency.
- Legacy v1 tables (`knowledge_entities`, `knowledge_relations`) and classes (`KnowledgeGraph`, `KnowledgeDB`) are never modified destructively.
- FTS5 triggers are disabled on vfat/exfat filesystems (existing pattern in `_setup_fts5_triggers`).
- sqlite-vec uses integer `rowid` as the key; `kg_entities_v2`/`kg_relations_v2` have implicit rowids (not `WITHOUT ROWID`).
- Tests use `pytest.mark.asyncio` + `tmp_path` fixture; follow patterns from `tests/test_bitemporal_memory.py`.
- Existing tests must not regress.
- LLM calls reuse the free-model pattern (`_call_free_model`) from `KnowledgeGraph`; retrieval hot path has zero LLM calls.

## File Structure

**New files:**
| File | Responsibility |
|------|---------------|
| `db/db_kg_v2.py` | KnowledgeDBV2 — CRUD for kg_entities_v2, kg_relations_v2, kg_episodes, kg_communities, kg_edge_episode_refs |
| `memory/knowledge_graph_v2.py` | KnowledgeGraphV2 — episode ingestion, fact supersession, entity evolution, community detection (inherits KnowledgeGraph) |
| `memory/kg_search.py` | KGSearchEngine — hybrid retrieval: semantic + fulltext + graph + RRF fusion |
| `tests/test_kg_v2_schema.py` | Schema v14 migration tests (idempotency, table creation, data migration) |
| `tests/test_kg_v2_crud.py` | KnowledgeDBV2 CRUD unit tests |
| `tests/test_kg_v2_invalidation.py` | Fact supersession + entity evolution tests |
| `tests/test_kg_v2_search.py` | Hybrid search engine + RRF fusion tests |
| `tests/test_kg_v2_community.py` | Community detection (label propagation) tests |
| `tests/test_kg_v2_integration.py` | Feature flag + end-to-end integration tests |

**Modified files:**
| File | Change |
|------|--------|
| `db/database.py` | `CURRENT_SCHEMA_VERSION` → 14, add `_migrate_v14()`, append to migrations list, init KnowledgeDBV2, extend `_setup_fts5_triggers` for v2 FTS tables |
| `memory/vector_store.py` | `init()` creates `kg_entities_vec`/`kg_relations_vec` vec0 tables; new methods `upsert_kg_entity`, `upsert_kg_relation`, `search_kg_entities`, `search_kg_relations` |
| `memory/knowledge_graph.py` | `auto_extract_and_merge()` adds v2 branch with feature flag; new `set_kg_v2()` method |
| `memory/memory_manager.py` | Search path adds KG v2 hybrid retrieval; new `_kg_v2_recall` coroutine |
| `config.py` | New `KG_V2_ENABLED` flag |

---

### Task 1: DB Schema v14 Migration

**Files:**
- Modify: `db/database.py` (lines 25, 199-213, 515-608, 1032-1060)
- Test: `tests/test_kg_v2_schema.py`

**Interfaces:**
- Consumes: Existing `_migrate_v13()` pattern, `schema_version` table, `_setup_fts5_triggers()` pattern.
- Produces:
  - `DatabaseManager._migrate_v14()` — creates v2 tables, FTS5 virtual tables, indexes, migrates data from v1.
  - `CURRENT_SCHEMA_VERSION = 14`
  - Tables: `kg_episodes`, `kg_entities_v2`, `kg_relations_v2`, `kg_communities`, `kg_edge_episode_refs`, `kg_entities_v2_fts`, `kg_relations_v2_fts`.
  - Extended `_setup_fts5_triggers()` with v2 FTS5 triggers.

- [ ] **Step 1: Write the failing test for v14 schema migration**

Create `tests/test_kg_v2_schema.py`:

```python
"""KG v2 Schema v14 迁移测试 — 表创建、数据迁移、幂等性。"""
import sqlite3
import pytest

from db.database import CURRENT_SCHEMA_VERSION, DatabaseManager

V2_TABLES = {
    "kg_episodes",
    "kg_entities_v2",
    "kg_relations_v2",
    "kg_communities",
    "kg_edge_episode_refs",
    "kg_entities_v2_fts",
    "kg_relations_v2_fts",
}


async def _schema_version(manager: DatabaseManager) -> int:
    row = await manager.fetch_one("SELECT MAX(version) AS version FROM schema_version")
    return row["version"]


async def _table_names(manager: DatabaseManager) -> set[str]:
    rows = await manager.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")
    return {row["name"] for row in rows}


@pytest.mark.asyncio
async def test_fresh_database_migrates_to_v14(tmp_path):
    db_path = tmp_path / "fresh_v14.db"
    manager = DatabaseManager(db_path)
    await manager.init()
    assert CURRENT_SCHEMA_VERSION == 14
    assert await _schema_version(manager) == 14
    assert V2_TABLES <= await _table_names(manager)
    await manager.close()


@pytest.mark.asyncio
async def test_v14_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "idempotent_v14.db"
    manager = DatabaseManager(db_path)
    await manager.init()
    # Re-run v14 migration directly
    await manager._migrate_v14()
    await manager.commit()
    # Re-init to ensure no duplicate schema_version entries
    await manager.init()
    versions = await manager.fetch_all(
        "SELECT version, COUNT(*) AS count FROM schema_version GROUP BY version HAVING version = 14"
    )
    assert len(versions) == 1
    assert versions[0]["count"] == 1
    await manager.close()


@pytest.mark.asyncio
async def test_v14_migrates_existing_v1_data(tmp_path):
    db_path = tmp_path / "migrate_v1_to_v14.db"
    manager = DatabaseManager(db_path)
    await manager.init()
    # Insert v1 data
    await manager.execute(
        "INSERT INTO knowledge_entities (id, name, kind, observations, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("ENT-old1", "篮球", "概念", '["团队运动"]', 1000.0),
    )
    await manager.execute(
        "INSERT INTO knowledge_relations (id, from_entity, relation_type, to_entity, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("REL-old1", "用户", "喜欢", "篮球", 1000.0, 1000.0),
    )
    await manager.close()

    # Manually drop v2 tables and reset schema to v13 to simulate upgrade
    with sqlite3.connect(db_path) as conn:
        for t in V2_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.execute("DELETE FROM schema_version WHERE version = 14")
        conn.commit()

    # Upgrade
    upgraded = DatabaseManager(db_path)
    await upgraded.init()
    assert await _schema_version(upgraded) == 14

    # Verify entity migrated
    entity = await upgraded.fetch_one("SELECT * FROM kg_entities_v2 WHERE name = ?", ("篮球",))
    assert entity is not None
    assert entity["kind"] == "概念"
    assert entity["summary"] == '["团队运动"]'  # observations → summary compatibility
    assert entity["summary_version"] == 0

    # Verify relation migrated
    rel = await upgraded.fetch_one("SELECT * FROM kg_relations_v2 WHERE id = ?", ("REL-old1",))
    assert rel is not None
    assert rel["from_entity"] == "用户"
    assert rel["relation_type"] == "喜欢"
    assert rel["to_entity"] == "篮球"
    assert rel["fact"] == "用户 喜欢 篮球"
    assert rel["is_current"] == 1
    assert rel["valid_at"] == 1000.0

    # Verify v1 tables still exist (not deleted)
    tables = await _table_names(upgraded)
    assert "knowledge_entities" in tables
    assert "knowledge_relations" in tables
    await upgraded.close()


@pytest.mark.asyncio
async def test_v14_fts5_triggers_sync_on_insert(tmp_path):
    """Verify FTS5 triggers auto-populate v2 FTS index on insert."""
    db_path = tmp_path / "fts_v14.db"
    manager = DatabaseManager(db_path)
    await manager.init()
    await manager.execute(
        "INSERT INTO kg_entities_v2 (id, name, kind, observations, summary, summary_version, updated_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("ENT-fts1", "测试实体", "概念", '[]', '这是一个测试摘要', 0, 1000.0, 1000.0),
    )
    # FTS5 should have the entry via trigger
    row = await manager.fetch_one(
        "SELECT id FROM kg_entities_v2_fts WHERE name_summary MATCH ?", ('"测试实体"',)
    )
    assert row is not None
    assert row["id"] == "ENT-fts1"
    await manager.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_kg_v2_schema.py -v`
Expected: FAIL — `CURRENT_SCHEMA_VERSION` is 13, not 14; `_migrate_v14` does not exist; v2 tables not created.

- [ ] **Step 3: Implement `_migrate_v14` and update `database.py`**

In `db/database.py`, change line 25:

```python
CURRENT_SCHEMA_VERSION = 14
```

Append to the `migrations` list (after line 213, the v13 entry):

```python
            (14, "kg_v2_tables", self._migrate_v14),
```

Add the `_migrate_v14` method after `_migrate_v13` (after line 608):

```python
    async def _migrate_v14(self) -> None:
        """v14: 知识图谱 v2 — 时序事实、实体演化、Episode溯源、社区发现。"""
        # 1. 创建 v2 表
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS kg_episodes (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source_type TEXT DEFAULT 'summary',
                source_description TEXT DEFAULT '',
                valid_at REAL NOT NULL,
                created_at REAL NOT NULL,
                group_id TEXT DEFAULT 'default'
            );
            CREATE INDEX IF NOT EXISTS idx_kg_episode_valid_at ON kg_episodes(valid_at);

            CREATE TABLE IF NOT EXISTS kg_entities_v2 (
                id TEXT PRIMARY KEY,
                name TEXT UNIQUE,
                kind TEXT DEFAULT '',
                observations TEXT DEFAULT '[]',
                summary TEXT DEFAULT '',
                summary_version INTEGER DEFAULT 0,
                name_embedding TEXT DEFAULT NULL,
                updated_at REAL NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_kg_entity_v2_name ON kg_entities_v2(name);

            CREATE TABLE IF NOT EXISTS kg_relations_v2 (
                id TEXT PRIMARY KEY,
                from_entity TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                to_entity TEXT NOT NULL,
                fact TEXT DEFAULT '',
                fact_embedding TEXT DEFAULT NULL,
                episode_ids TEXT DEFAULT '[]',
                valid_at REAL DEFAULT NULL,
                invalid_at REAL DEFAULT NULL,
                expired_at REAL DEFAULT NULL,
                is_current INTEGER DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_kg_rel_v2_from ON kg_relations_v2(from_entity);
            CREATE INDEX IF NOT EXISTS idx_kg_rel_v2_to ON kg_relations_v2(to_entity);
            CREATE INDEX IF NOT EXISTS idx_kg_rel_v2_current ON kg_relations_v2(is_current);
            CREATE INDEX IF NOT EXISTS idx_kg_rel_v2_valid_at ON kg_relations_v2(valid_at);
            CREATE INDEX IF NOT EXISTS idx_kg_rel_v2_invalid_at ON kg_relations_v2(invalid_at);

            CREATE TABLE IF NOT EXISTS kg_communities (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                summary TEXT DEFAULT '',
                member_entities TEXT DEFAULT '[]',
                name_embedding TEXT DEFAULT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kg_edge_episode_refs (
                edge_id TEXT NOT NULL,
                episode_id TEXT NOT NULL,
                PRIMARY KEY (edge_id, episode_id)
            );
            CREATE INDEX IF NOT EXISTS idx_kg_eer_episode ON kg_edge_episode_refs(episode_id);
            CREATE INDEX IF NOT EXISTS idx_kg_eer_edge ON kg_edge_episode_refs(edge_id);

            CREATE VIRTUAL TABLE IF NOT EXISTS kg_entities_v2_fts USING fts5(
                id UNINDEXED,
                name_summary
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS kg_relations_v2_fts USING fts5(
                id UNINDEXED,
                fact
            );
        """)

        # 2. 迁移 entities: knowledge_entities → kg_entities_v2
        await self._conn.execute("""
            INSERT OR IGNORE INTO kg_entities_v2 (id, name, kind, observations, summary, summary_version, updated_at, created_at)
            SELECT id, name, kind, observations,
                   observations AS summary,
                   0,
                   updated_at,
                   updated_at
            FROM knowledge_entities
            WHERE name NOT IN (SELECT name FROM kg_entities_v2)
        """)

        # 3. 迁移 relations: knowledge_relations → kg_relations_v2
        await self._conn.execute("""
            INSERT OR IGNORE INTO kg_relations_v2 (id, from_entity, relation_type, to_entity, fact, episode_ids, valid_at, invalid_at, expired_at, is_current, created_at, updated_at)
            SELECT id, from_entity, relation_type, to_entity,
                   from_entity || ' ' || relation_type || ' ' || to_entity AS fact,
                   '[]',
                   created_at AS valid_at,
                   NULL,
                   NULL,
                   1,
                   created_at,
                   updated_at
            FROM knowledge_relations
            WHERE id NOT IN (SELECT id FROM kg_relations_v2)
        """)

        # 4. 回填 FTS5 索引 (triggers 尚未创建, 手动插入)
        await self._conn.execute("""
            INSERT OR IGNORE INTO kg_entities_v2_fts (id, name_summary)
            SELECT id, name || ' ' || summary FROM kg_entities_v2
            WHERE id NOT IN (SELECT id FROM kg_entities_v2_fts)
        """)
        await self._conn.execute("""
            INSERT OR IGNORE INTO kg_relations_v2_fts (id, fact)
            SELECT id, fact FROM kg_relations_v2
            WHERE id NOT IN (SELECT id FROM kg_relations_v2_fts)
        """)
```

- [ ] **Step 4: Extend `_setup_fts5_triggers` for v2 FTS tables**

In `db/database.py`, modify `_setup_fts5_triggers` (around line 1043). After the existing v1 trigger block, add the v2 triggers inside the same `try` block:

```python
    async def _setup_fts5_triggers(self) -> None:
        """Phase 5: FTS5 触发器管理。vfat/exfat 上禁用（delete 命令不工作）。"""
        if getattr(self, "_is_fat_fs", False):
            for trig in ["knowledge_entities_fts_ai", "knowledge_entities_fts_ad", "knowledge_entities_fts_au",
                         "kg_entities_v2_fts_ai", "kg_entities_v2_fts_ad", "kg_entities_v2_fts_au",
                         "kg_relations_v2_fts_ai", "kg_relations_v2_fts_ad", "kg_relations_v2_fts_au"]:
                try:
                    await self._conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
                except (OSError, RuntimeError):
                    logger.debug("database.fts5_trigger_drop_error: {}", exc_info=True)
            logger.info("database.fts5_triggers_disabled (vfat)")
            return
        try:
            await self._conn.executescript("""
                CREATE TRIGGER IF NOT EXISTS knowledge_entities_fts_ai AFTER INSERT ON knowledge_entities BEGIN
                    INSERT INTO knowledge_entities_fts(id, name_index) VALUES (new.id, new.name);
                END;
                CREATE TRIGGER IF NOT EXISTS knowledge_entities_fts_ad AFTER DELETE ON knowledge_entities BEGIN
                    INSERT INTO knowledge_entities_fts(knowledge_entities_fts, id, name_index)
                    VALUES ('delete', old.id, old.name);
                END;
                CREATE TRIGGER IF NOT EXISTS knowledge_entities_fts_au AFTER UPDATE ON knowledge_entities BEGIN
                    INSERT INTO knowledge_entities_fts(knowledge_entities_fts, id, name_index)
                    VALUES ('delete', old.id, old.name);
                    INSERT INTO knowledge_entities_fts(id, name_index) VALUES (new.id, new.name);
                END;

                CREATE TRIGGER IF NOT EXISTS kg_entities_v2_fts_ai AFTER INSERT ON kg_entities_v2 BEGIN
                    INSERT INTO kg_entities_v2_fts(id, name_summary) VALUES (new.id, new.name || ' ' || new.summary);
                END;
                CREATE TRIGGER IF NOT EXISTS kg_entities_v2_fts_ad AFTER DELETE ON kg_entities_v2 BEGIN
                    INSERT INTO kg_entities_v2_fts(kg_entities_v2_fts, id, name_summary)
                    VALUES ('delete', old.id, old.name || ' ' || old.summary);
                END;
                CREATE TRIGGER IF NOT EXISTS kg_entities_v2_fts_au AFTER UPDATE ON kg_entities_v2 BEGIN
                    INSERT INTO kg_entities_v2_fts(kg_entities_v2_fts, id, name_summary)
                    VALUES ('delete', old.id, old.name || ' ' || old.summary);
                    INSERT INTO kg_entities_v2_fts(id, name_summary) VALUES (new.id, new.name || ' ' || new.summary);
                END;

                CREATE TRIGGER IF NOT EXISTS kg_relations_v2_fts_ai AFTER INSERT ON kg_relations_v2 BEGIN
                    INSERT INTO kg_relations_v2_fts(id, fact) VALUES (new.id, new.fact);
                END;
                CREATE TRIGGER IF NOT EXISTS kg_relations_v2_fts_ad AFTER DELETE ON kg_relations_v2 BEGIN
                    INSERT INTO kg_relations_v2_fts(kg_relations_v2_fts, id, fact)
                    VALUES ('delete', old.id, old.fact);
                END;
                CREATE TRIGGER IF NOT EXISTS kg_relations_v2_fts_au AFTER UPDATE ON kg_relations_v2 BEGIN
                    INSERT INTO kg_relations_v2_fts(kg_relations_v2_fts, id, fact)
                    VALUES ('delete', old.id, old.fact);
                    INSERT INTO kg_relations_v2_fts(id, fact) VALUES (new.id, new.fact);
                END;
            """)
        except (OSError, RuntimeError) as e:
            logger.warning(f"database.fts5_trigger_failed: {e} — FTS搜索将降级为LIKE查询")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_kg_v2_schema.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 6: Run existing bitemporal tests for regression**

Run: `python -m pytest tests/test_bitemporal_memory.py -v`
Expected: All existing tests still PASS (the `CURRENT_SCHEMA_VERSION == 13` assertion in `test_fresh_database_migrates_to_v13_idempotently` will FAIL — update it to 14).

Fix `tests/test_bitemporal_memory.py` line 34:
```python
    assert CURRENT_SCHEMA_VERSION == 14
```

Re-run: `python -m pytest tests/test_bitemporal_memory.py tests/test_kg_v2_schema.py -v`
Expected: All PASS.

- [ ] **Step 7: Commit**

```bash
git add db/database.py tests/test_kg_v2_schema.py tests/test_bitemporal_memory.py
git commit -m "feat(kg-v2): add v14 schema migration with KG v2 tables, FTS5, data migration"
```

---

### Task 2: KnowledgeDBV2 CRUD

**Files:**
- Create: `db/db_kg_v2.py`
- Test: `tests/test_kg_v2_crud.py`

**Interfaces:**
- Consumes: `aiosqlite.Connection` (from `DatabaseManager._conn`), v2 tables from Task 1.
- Produces:
  - `KnowledgeDBV2(conn: aiosqlite.Connection)` — constructor.
  - `async insert_episode(episode_id, content, source_type, valid_at, created_at, source_description="", group_id="default", auto_commit=True) -> None`
  - `async get_episode(episode_id) -> dict | None`
  - `async insert_entity_v2(entity_id, name, kind, observations, summary, auto_commit=True) -> int` — returns rowid.
  - `async get_entity_v2(name) -> dict | None`
  - `async update_entity_summary_v2(name, summary, summary_version, auto_commit=True) -> int` — returns rowid.
  - `async insert_relation_v2(rel_id, from_entity, relation_type, to_entity, fact, episode_id, valid_at, auto_commit=True) -> int` — returns rowid.
  - `async get_active_relations_between(from_entity, to_entity) -> list[dict]`
  - `async invalidate_relation(rel_id, invalid_at, expired_at, auto_commit=True) -> None`
  - `async append_episode_ref(edge_id, episode_id, auto_commit=True) -> None`
  - `async insert_community(community_id, name, summary, member_entities, auto_commit=True) -> None`
  - `async get_entity_community(entity_name) -> str | None` — returns community_id.
  - `async add_entity_to_community(entity_name, community_id, auto_commit=True) -> None`
  - `async get_facts_from_episode(episode_id) -> list[dict]`
  - `async get_episodes_for_fact(edge_id) -> list[dict]`

- [ ] **Step 1: Write the failing test for KnowledgeDBV2 CRUD**

Create `tests/test_kg_v2_crud.py`:

```python
"""KnowledgeDBV2 CRUD 单元测试。"""
import json
import time

import pytest

from db.database import DatabaseManager
from db.db_kg_v2 import KnowledgeDBV2


@pytest.mark.asyncio
async def test_episode_crud(tmp_path):
    manager = DatabaseManager(tmp_path / "crud.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    now = time.time()
    await db.insert_episode("EP-test1", "用户讨论了篮球", "summary", 1000.0, now)
    ep = await db.get_episode("EP-test1")
    assert ep is not None
    assert ep["content"] == "用户讨论了篮球"
    assert ep["source_type"] == "summary"
    assert ep["valid_at"] == 1000.0
    assert await db.get_episode("EP-nonexistent") is None
    await manager.close()


@pytest.mark.asyncio
async def test_entity_v2_crud(tmp_path):
    manager = DatabaseManager(tmp_path / "entity.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    rowid = await db.insert_entity_v2("ENT-1", "篮球", "概念", ["团队运动"], "团队运动")
    assert rowid > 0
    ent = await db.get_entity_v2("篮球")
    assert ent is not None
    assert ent["kind"] == "概念"
    assert ent["summary"] == "团队运动"
    assert ent["summary_version"] == 0

    rowid2 = await db.update_entity_summary_v2("篮球", "用户喜欢的团队运动", 1)
    assert rowid2 == rowid
    ent2 = await db.get_entity_v2("篮球")
    assert ent2["summary"] == "用户喜欢的团队运动"
    assert ent2["summary_version"] == 1
    await manager.close()


@pytest.mark.asyncio
async def test_relation_v2_crud_and_invalidation(tmp_path):
    manager = DatabaseManager(tmp_path / "rel.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    await db.insert_entity_v2("ENT-a", "用户", "人物", [], "")
    await db.insert_entity_v2("ENT-b", "篮球", "概念", [], "")

    rowid = await db.insert_relation_v2(
        "REL-1", "用户", "喜欢", "篮球", "用户喜欢篮球", "EP-1", 1000.0
    )
    assert rowid > 0

    active = await db.get_active_relations_between("用户", "篮球")
    assert len(active) == 1
    assert active[0]["fact"] == "用户喜欢篮球"
    assert active[0]["is_current"] == 1

    await db.invalidate_relation("REL-1", invalid_at=2000.0, expired_at=2001.0)
    active2 = await db.get_active_relations_between("用户", "篮球")
    assert len(active2) == 0
    await manager.close()


@pytest.mark.asyncio
async def test_append_episode_ref_and_bidirectional_query(tmp_path):
    manager = DatabaseManager(tmp_path / "eer.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    await db.insert_episode("EP-1", "内容1", "summary", 1000.0, time.time())
    await db.insert_episode("EP-2", "内容2", "summary", 2000.0, time.time())
    await db.insert_relation_v2("REL-1", "用户", "喜欢", "篮球", "用户喜欢篮球", "EP-1", 1000.0)
    await db.append_episode_ref("REL-1", "EP-2")

    facts = await db.get_facts_from_episode("EP-1")
    assert len(facts) == 1
    assert facts[0]["id"] == "REL-1"

    facts2 = await db.get_facts_from_episode("EP-2")
    assert len(facts2) == 1
    assert facts2[0]["id"] == "REL-1"

    episodes = await db.get_episodes_for_fact("REL-1")
    assert len(episodes) == 2
    ep_ids = {e["id"] for e in episodes}
    assert ep_ids == {"EP-1", "EP-2"}
    await manager.close()


@pytest.mark.asyncio
async def test_community_crud(tmp_path):
    manager = DatabaseManager(tmp_path / "comm.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    await db.insert_entity_v2("ENT-a", "用户", "人物", [], "")
    await db.insert_entity_v2("ENT-b", "篮球", "概念", [], "")
    await db.insert_community("COM-1", "运动社区", "关于运动的社区", ["用户", "篮球"])

    comm_id = await db.get_entity_community("篮球")
    assert comm_id == "COM-1"

    await db.insert_entity_v2("ENT-c", "足球", "概念", [], "")
    await db.add_entity_to_community("足球", "COM-1")
    comm_id2 = await db.get_entity_community("足球")
    assert comm_id2 == "COM-1"
    await manager.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_kg_v2_crud.py -v`
Expected: FAIL — `db.db_kg_v2` module does not exist.

- [ ] **Step 3: Implement KnowledgeDBV2**

Create `db/db_kg_v2.py`:

```python
"""KnowledgeDBV2 — KG v2 表的 CRUD 操作。

覆盖: kg_episodes, kg_entities_v2, kg_relations_v2, kg_communities, kg_edge_episode_refs。
所有写方法默认 auto_commit=True，返回 rowid 的方法用于向量表同步。
"""
import json
import time

import aiosqlite


class KnowledgeDBV2:
    """KG v2 表的持久化操作。"""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        conn.row_factory = aiosqlite.Row

    # ── Episode ──────────────────────────────────────────────

    async def insert_episode(
        self,
        episode_id: str,
        content: str,
        source_type: str,
        valid_at: float,
        created_at: float,
        source_description: str = "",
        group_id: str = "default",
        auto_commit: bool = True,
    ) -> None:
        await self._conn.execute(
            """INSERT OR REPLACE INTO kg_episodes
               (id, content, source_type, source_description, valid_at, created_at, group_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (episode_id, content, source_type, source_description, valid_at, created_at, group_id),
        )
        if auto_commit:
            await self._conn.commit()

    async def get_episode(self, episode_id: str) -> dict | None:
        cursor = await self._conn.execute(
            "SELECT * FROM kg_episodes WHERE id=?", (episode_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # ── Entity v2 ────────────────────────────────────────────

    async def insert_entity_v2(
        self,
        entity_id: str,
        name: str,
        kind: str,
        observations: list,
        summary: str,
        auto_commit: bool = True,
    ) -> int:
        """插入实体，返回 rowid（用于向量表同步）。"""
        obs_json = json.dumps(observations or [], ensure_ascii=False)
        now = time.time()
        cursor = await self._conn.execute(
            """INSERT OR IGNORE INTO kg_entities_v2
               (id, name, kind, observations, summary, summary_version, updated_at, created_at)
               VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
            (entity_id, name, kind, obs_json, summary, now, now),
        )
        if auto_commit:
            await self._conn.commit()
        rowid = cursor.lastrowid
        if rowid == 0:
            cur = await self._conn.execute(
                "SELECT rowid FROM kg_entities_v2 WHERE name=?", (name,)
            )
            row = await cur.fetchone()
            rowid = row[0] if row else 0
        return rowid

    async def get_entity_v2(self, name: str) -> dict | None:
        cursor = await self._conn.execute(
            "SELECT * FROM kg_entities_v2 WHERE name=?", (name,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_entity_summary_v2(
        self,
        name: str,
        summary: str,
        summary_version: int,
        auto_commit: bool = True,
    ) -> int:
        """更新实体摘要，返回 rowid。"""
        await self._conn.execute(
            "UPDATE kg_entities_v2 SET summary=?, summary_version=?, updated_at=? WHERE name=?",
            (summary, summary_version, time.time(), name),
        )
        if auto_commit:
            await self._conn.commit()
        cur = await self._conn.execute(
            "SELECT rowid FROM kg_entities_v2 WHERE name=?", (name,)
        )
        row = await cur.fetchone()
        return row[0] if row else 0

    # ── Relation v2 ──────────────────────────────────────────

    async def insert_relation_v2(
        self,
        rel_id: str,
        from_entity: str,
        relation_type: str,
        to_entity: str,
        fact: str,
        episode_id: str,
        valid_at: float,
        auto_commit: bool = True,
    ) -> int:
        """插入关系，返回 rowid。同时写入 episode_ref。"""
        now = time.time()
        episode_ids = json.dumps([episode_id], ensure_ascii=False)
        cursor = await self._conn.execute(
            """INSERT OR IGNORE INTO kg_relations_v2
               (id, from_entity, relation_type, to_entity, fact, fact_embedding,
                episode_ids, valid_at, invalid_at, expired_at, is_current,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, NULL, ?, ?, NULL, NULL, 1, ?, ?)""",
            (rel_id, from_entity, relation_type, to_entity, fact,
             episode_ids, valid_at, now, now),
        )
        # 写入 episode ref
        await self._conn.execute(
            "INSERT OR IGNORE INTO kg_edge_episode_refs (edge_id, episode_id) VALUES (?, ?)",
            (rel_id, episode_id),
        )
        if auto_commit:
            await self._conn.commit()
        rowid = cursor.lastrowid
        if rowid == 0:
            cur = await self._conn.execute(
                "SELECT rowid FROM kg_relations_v2 WHERE id=?", (rel_id,)
            )
            row = await cur.fetchone()
            rowid = row[0] if row else 0
        return rowid

    async def get_active_relations_between(
        self, from_entity: str, to_entity: str
    ) -> list[dict]:
        """获取两个实体之间当前有效的关系（双向）。"""
        cursor = await self._conn.execute(
            """SELECT * FROM kg_relations_v2
               WHERE ((from_entity=? AND to_entity=?) OR (from_entity=? AND to_entity=?))
               AND is_current=1""",
            (from_entity, to_entity, to_entity, from_entity),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def invalidate_relation(
        self,
        rel_id: str,
        invalid_at: float,
        expired_at: float,
        auto_commit: bool = True,
    ) -> None:
        """标记关系失效。"""
        await self._conn.execute(
            """UPDATE kg_relations_v2
               SET invalid_at=?, expired_at=?, is_current=0, updated_at=?
               WHERE id=?""",
            (invalid_at, expired_at, time.time(), rel_id),
        )
        if auto_commit:
            await self._conn.commit()

    async def append_episode_ref(
        self, edge_id: str, episode_id: str, auto_commit: bool = True
    ) -> None:
        """追加 episode 引用（去重）。同时更新关系的 episode_ids JSON。"""
        await self._conn.execute(
            "INSERT OR IGNORE INTO kg_edge_episode_refs (edge_id, episode_id) VALUES (?, ?)",
            (edge_id, episode_id),
        )
        # 同步更新 episode_ids JSON 列
        cursor = await self._conn.execute(
            "SELECT episode_ids FROM kg_relations_v2 WHERE id=?", (edge_id,)
        )
        row = await cursor.fetchone()
        if row:
            try:
                ids = json.loads(row["episode_ids"]) if row["episode_ids"] else []
            except (json.JSONDecodeError, TypeError):
                ids = []
            if episode_id not in ids:
                ids.append(episode_id)
                await self._conn.execute(
                    "UPDATE kg_relations_v2 SET episode_ids=?, updated_at=? WHERE id=?",
                    (json.dumps(ids, ensure_ascii=False), time.time(), edge_id),
                )
        if auto_commit:
            await self._conn.commit()

    # ── Community ────────────────────────────────────────────

    async def insert_community(
        self,
        community_id: str,
        name: str,
        summary: str,
        member_entities: list,
        auto_commit: bool = True,
    ) -> None:
        members_json = json.dumps(member_entities, ensure_ascii=False)
        now = time.time()
        await self._conn.execute(
            """INSERT OR REPLACE INTO kg_communities
               (id, name, summary, member_entities, name_embedding, created_at, updated_at)
               VALUES (?, ?, ?, ?, NULL, ?, ?)""",
            (community_id, name, summary, members_json, now, now),
        )
        # 更新成员实体的社区归属
        for entity_name in member_entities:
            await self._conn.execute(
                "UPDATE kg_entities_v2 SET name_embedding=? WHERE name=?",
                (community_id, entity_name),
            )
        if auto_commit:
            await self._conn.commit()

    async def get_entity_community(self, entity_name: str) -> str | None:
        """查询实体所属社区 ID。"""
        cursor = await self._conn.execute(
            "SELECT name_embedding FROM kg_entities_v2 WHERE name=?", (entity_name,)
        )
        row = await cursor.fetchone()
        if row and row["name_embedding"]:
            return row["name_embedding"]
        return None

    async def add_entity_to_community(
        self, entity_name: str, community_id: str, auto_commit: bool = True
    ) -> None:
        """将实体加入社区（设置 name_embedding 为 community_id）。"""
        await self._conn.execute(
            "UPDATE kg_entities_v2 SET name_embedding=?, updated_at=? WHERE name=?",
            (community_id, time.time(), entity_name),
        )
        # 同步更新社区的 member_entities 列表
        cursor = await self._conn.execute(
            "SELECT member_entities FROM kg_communities WHERE id=?", (community_id,)
        )
        row = await cursor.fetchone()
        if row:
            try:
                members = json.loads(row["member_entities"]) if row["member_entities"] else []
            except (json.JSONDecodeError, TypeError):
                members = []
            if entity_name not in members:
                members.append(entity_name)
                await self._conn.execute(
                    "UPDATE kg_communities SET member_entities=?, updated_at=? WHERE id=?",
                    (json.dumps(members, ensure_ascii=False), time.time(), community_id),
                )
        if auto_commit:
            await self._conn.commit()

    # ── 双向溯源查询 ─────────────────────────────────────────

    async def get_facts_from_episode(self, episode_id: str) -> list[dict]:
        """前向查询: episode → facts (relations)。"""
        cursor = await self._conn.execute(
            """SELECT r.* FROM kg_relations_v2 r
               JOIN kg_edge_episode_refs ref ON ref.edge_id = r.id
               WHERE ref.episode_id=?""",
            (episode_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_episodes_for_fact(self, edge_id: str) -> list[dict]:
        """反向查询: fact → episodes。"""
        cursor = await self._conn.execute(
            """SELECT e.* FROM kg_episodes e
               JOIN kg_edge_episode_refs ref ON ref.episode_id = e.id
               WHERE ref.edge_id=?""",
            (edge_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_kg_v2_crud.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add db/db_kg_v2.py tests/test_kg_v2_crud.py
git commit -m "feat(kg-v2): add KnowledgeDBV2 CRUD for episodes, entities, relations, communities"
```

---

### Task 3: VectorStore Extension

**Files:**
- Modify: `memory/vector_store.py` (init method around line 154, new methods at end of class)
- Test: `tests/test_kg_v2_search.py` (vector portion only — full search tests in Task 5)

**Interfaces:**
- Consumes: `sqlite_vec` extension, existing `VectorStore.init()` / `embed()` / `upsert()` pattern.
- Produces:
  - `VectorStore.init()` now creates `kg_entities_vec` and `kg_relations_vec` vec0 tables.
  - `async upsert_kg_entity(row_id: int, text: str) -> bool`
  - `async upsert_kg_relation(row_id: int, text: str) -> bool`
  - `async search_kg_entities(query_text: str, top_k: int = 5) -> list[tuple[int, float]]`
  - `async search_kg_relations(query_text: str, top_k: int = 5) -> list[tuple[int, float]]`

- [ ] **Step 1: Write the failing test for KG vector operations**

Create `tests/test_kg_v2_search.py` with the vector portion:

```python
"""KG v2 向量存储 + 混合检索测试。"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from db.database import DatabaseManager
from db.db_kg_v2 import KnowledgeDBV2


@pytest.fixture
def mock_vec_store():
    """创建带 mock embed 的 VectorStore。"""
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite_vec not available")
    from memory.vector_store import VectorStore
    import tempfile
    import os

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = VectorStore(path, embed_api_key="fake-key")
    return store, path


@pytest.mark.asyncio
async def test_kg_vec_tables_created(mock_vec_store):
    store, path = mock_vec_store
    try:
        await store.init()
        # Verify tables exist
        import sqlite3
        conn = sqlite3.connect(path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "kg_entities_vec" in tables
        assert "kg_relations_vec" in tables
    finally:
        await store.close()
        import os
        os.unlink(path)


@pytest.mark.asyncio
async def test_upsert_and_search_kg_entity(mock_vec_store):
    store, path = mock_vec_store
    try:
        await store.init()
        # Mock embed to return deterministic 1024-dim vector
        store._cache.clear() if hasattr(store._cache, 'clear') else None
        store.embed = AsyncMock(return_value=[0.1] * 1024)

        ok = await store.upsert_kg_entity(1, "篮球: 团队运动")
        assert ok is True

        results = await store.search_kg_entities("团队运动", top_k=5)
        assert len(results) >= 1
        assert results[0][0] == 1  # rowid
    finally:
        await store.close()
        import os
        os.unlink(path)


@pytest.mark.asyncio
async def test_upsert_and_search_kg_relation(mock_vec_store):
    store, path = mock_vec_store
    try:
        await store.init()
        store.embed = AsyncMock(return_value=[0.2] * 1024)

        ok = await store.upsert_kg_relation(1, "用户喜欢打篮球")
        assert ok is True

        results = await store.search_kg_relations("篮球", top_k=5)
        assert len(results) >= 1
        assert results[0][0] == 1  # rowid
    finally:
        await store.close()
        import os
        os.unlink(path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_kg_v2_search.py::test_kg_vec_tables_created -v`
Expected: FAIL — `kg_entities_vec` table not created in `init()`.

- [ ] **Step 3: Add vec0 table creation to `VectorStore.init()`**

In `memory/vector_store.py`, modify the `_init_db` inner function (around line 154). After the `memories_child_vec` creation, add:

```python
                conn.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS kg_entities_vec
                    USING vec0(embedding float[{dims}])
                """)
                conn.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS kg_relations_vec
                    USING vec0(embedding float[{dims}])
                """)
```

The full block should look like:

```python
                conn.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec
                    USING vec0(embedding float[{dims}])
                """)
                conn.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS memories_child_vec
                    USING vec0(embedding float[{dims}])
                """)
                conn.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS kg_entities_vec
                    USING vec0(embedding float[{dims}])
                """)
                conn.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS kg_relations_vec
                    USING vec0(embedding float[{dims}])
                """)
                conn.commit()
                return conn, is_fat
```

- [ ] **Step 4: Add KG vector methods to VectorStore**

At the end of the `VectorStore` class (after `search_with_hyde`), add:

```python
    async def upsert_kg_entity(self, row_id: int, text: str) -> bool:
        """写入或更新 KG 实体向量（先删后插）。"""
        if not self._initialized or not self._vec_conn:
            return False
        vec = await self.embed(text)
        if not vec:
            return False
        vec_json = json.dumps(vec)

        def _do_upsert() -> bool:
            with self._lock:
                if self._closed:
                    return False
                try:
                    self._vec_conn.execute("BEGIN TRANSACTION")
                    try:
                        self._vec_conn.execute(
                            "DELETE FROM kg_entities_vec WHERE rowid=?", [row_id]
                        )
                    except Exception:
                        pass
                    self._vec_conn.execute(
                        "INSERT INTO kg_entities_vec(rowid, embedding) VALUES (?, vec_f32(?))",
                        [row_id, vec_json],
                    )
                    self._vec_conn.commit()
                    return True
                except Exception as e:
                    try:
                        self._vec_conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    logger.warning("vector_store.upsert_kg_entity_failed", row_id=row_id, error=str(e))
                    return False

        return await asyncio.to_thread(_do_upsert)

    async def upsert_kg_relation(self, row_id: int, text: str) -> bool:
        """写入或更新 KG 关系向量（先删后插）。"""
        if not self._initialized or not self._vec_conn:
            return False
        vec = await self.embed(text)
        if not vec:
            return False
        vec_json = json.dumps(vec)

        def _do_upsert() -> bool:
            with self._lock:
                if self._closed:
                    return False
                try:
                    self._vec_conn.execute("BEGIN TRANSACTION")
                    try:
                        self._vec_conn.execute(
                            "DELETE FROM kg_relations_vec WHERE rowid=?", [row_id]
                        )
                    except Exception:
                        pass
                    self._vec_conn.execute(
                        "INSERT INTO kg_relations_vec(rowid, embedding) VALUES (?, vec_f32(?))",
                        [row_id, vec_json],
                    )
                    self._vec_conn.commit()
                    return True
                except Exception as e:
                    try:
                        self._vec_conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    logger.warning("vector_store.upsert_kg_relation_failed", row_id=row_id, error=str(e))
                    return False

        return await asyncio.to_thread(_do_upsert)

    async def search_kg_entities(self, query_text: str, top_k: int = 5) -> list[tuple[int, float]]:
        """搜索 KG 实体向量，返回 [(rowid, distance), ...]。"""
        if not self._initialized or not self._vec_conn:
            return []
        vec = await self.embed(query_text)
        if not vec:
            return []
        vec_json = json.dumps(vec)
        fetch_k = top_k * 2

        def _do_search() -> list[tuple[int, float]]:
            with self._lock:
                if self._closed:
                    return []
                rows = self._vec_conn.execute(
                    "SELECT rowid, distance FROM kg_entities_vec "
                    "WHERE embedding MATCH vec_f32(?) AND k=? "
                    "ORDER BY distance",
                    [vec_json, fetch_k],
                ).fetchall()
                results = [(row[0], row[1]) for row in rows]
                results.sort(key=lambda r: (r[1], r[0]))
                return results[:top_k]

        try:
            return await asyncio.to_thread(_do_search)
        except Exception as e:
            logger.warning("vector_store.search_kg_entities_failed", error=str(e))
            return []

    async def search_kg_relations(self, query_text: str, top_k: int = 5) -> list[tuple[int, float]]:
        """搜索 KG 关系向量，返回 [(rowid, distance), ...]。"""
        if not self._initialized or not self._vec_conn:
            return []
        vec = await self.embed(query_text)
        if not vec:
            return []
        vec_json = json.dumps(vec)
        fetch_k = top_k * 2

        def _do_search() -> list[tuple[int, float]]:
            with self._lock:
                if self._closed:
                    return []
                rows = self._vec_conn.execute(
                    "SELECT rowid, distance FROM kg_relations_vec "
                    "WHERE embedding MATCH vec_f32(?) AND k=? "
                    "ORDER BY distance",
                    [vec_json, fetch_k],
                ).fetchall()
                results = [(row[0], row[1]) for row in rows]
                results.sort(key=lambda r: (r[1], r[0]))
                return results[:top_k]

        try:
            return await asyncio.to_thread(_do_search)
        except Exception as e:
            logger.warning("vector_store.search_kg_relations_failed", error=str(e))
            return []
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_kg_v2_search.py -v -k "kg_vec or upsert_and_search"`
Expected: All 3 vector tests PASS.

- [ ] **Step 6: Commit**

```bash
git add memory/vector_store.py tests/test_kg_v2_search.py
git commit -m "feat(kg-v2): extend VectorStore with kg_entities_vec/kg_relations_vec tables and methods"
```

---

### Task 4: KnowledgeGraphV2 Core Logic

**Files:**
- Create: `memory/knowledge_graph_v2.py`
- Test: `tests/test_kg_v2_invalidation.py`

**Interfaces:**
- Consumes:
  - `KnowledgeDBV2` from Task 2 (all CRUD methods).
  - `VectorStore` from Task 3 (`upsert_kg_entity`, `upsert_kg_relation`).
  - `KnowledgeGraph` parent class (`_call_free_model`, `extract_from_summary`, `set_free_model_client`).
- Produces:
  - `KnowledgeGraphV2(db_v2: KnowledgeDBV2, vector_store: Any = None, router: Any = None)` — inherits `KnowledgeGraph`.
  - `async add_facts_from_episode(episode_content: str, episode_time: float, source_type: str = "summary") -> dict` — returns `{"episode_id": str, "new_facts": int, "invalidated": int}`.
  - `async merge_entities_v2(entities: list[dict], episode_content: str, episode_time: float) -> None`
  - `async merge_relation_v2(relation: dict, episode_id: str, episode_time: float) -> tuple[bool, list[dict]]`
  - `async _detect_contradictions(new_fact: str, existing_facts: list[str]) -> list[int]`
  - `_resolve_contradiction(old_relation: dict, new_valid_at: float) -> bool`
  - Module-level `ENTITY_EXTRACT_PROMPT_V2` with `fact` field.

- [ ] **Step 1: Write the failing test for fact supersession and entity evolution**

Create `tests/test_kg_v2_invalidation.py`:

```python
"""KG v2 事实超驰 + 实体演化测试。"""
import time
from unittest.mock import AsyncMock, patch

import pytest

from db.database import DatabaseManager
from db.db_kg_v2 import KnowledgeDBV2
from memory.knowledge_graph_v2 import KnowledgeGraphV2


@pytest.mark.asyncio
async def test_resolve_contradiction_marks_old_as_invalid(tmp_path):
    """旧事实生效更早 → 标记失效。"""
    manager = DatabaseManager(tmp_path / "sup.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraphV2(db_v2=db, vector_store=None)
    old_relation = {"valid_at": 1000.0, "invalid_at": None, "is_current": 1}
    result = kg._resolve_contradiction(old_relation, new_valid_at=2000.0)
    assert result is True
    assert old_relation["invalid_at"] == 2000.0
    assert old_relation["is_current"] == 0
    await manager.close()


@pytest.mark.asyncio
async def test_resolve_contradiction_skips_already_invalid(tmp_path):
    """旧事实已失效 → 不冲突。"""
    manager = DatabaseManager(tmp_path / "sup2.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraphV2(db_v2=db, vector_store=None)
    old_relation = {"valid_at": 1000.0, "invalid_at": 1500.0, "is_current": 0}
    result = kg._resolve_contradiction(old_relation, new_valid_at=2000.0)
    assert result is False
    await manager.close()


@pytest.mark.asyncio
async def test_detect_contradictions_via_llm(tmp_path):
    """LLM 矛盾检测返回被矛盾的索引列表。"""
    manager = DatabaseManager(tmp_path / "detect.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraphV2(db_v2=db, vector_store=None)
    # Mock LLM to return contradiction at index 0
    kg._call_free_model = AsyncMock(return_value='{"contradicted_indices": [0]}')
    result = await kg._detect_contradictions(
        new_fact="用户改打网球了",
        existing_facts=["用户喜欢打篮球"],
    )
    assert result == [0]
    await manager.close()


@pytest.mark.asyncio
async def test_merge_relation_v2_supersedes_old_fact(tmp_path):
    """新事实超驰旧事实：旧关系 is_current→0，新关系插入。"""
    manager = DatabaseManager(tmp_path / "merge.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraphV2(db_v2=db, vector_store=None)
    # Mock LLM contradiction detection
    kg._call_free_model = AsyncMock(return_value='{"contradicted_indices": [0]}')
    kg.extract_from_summary = AsyncMock(return_value={
        "entities": [{"name": "用户", "kind": "人物", "observations": []}],
        "relations": [],
    })

    # Insert old fact
    await db.insert_entity_v2("ENT-u", "用户", "人物", [], "")
    await db.insert_entity_v2("ENT-b", "篮球", "概念", [], "")
    await db.insert_episode("EP-old", "旧对话", "summary", 1000.0, time.time())
    await db.insert_relation_v2("REL-old", "用户", "喜欢", "篮球", "用户喜欢篮球", "EP-old", 1000.0)

    # Merge new contradictory fact
    new_rel = {
        "from_entity": "用户",
        "relation_type": "喜欢",
        "to_entity": "网球",
        "fact": "用户改打网球了",
    }
    is_new, invalidated = await kg.merge_relation_v2(new_rel, "EP-new", 2000.0)
    assert is_new is True
    assert len(invalidated) == 1
    assert invalidated[0]["id"] == "REL-old"

    # Verify old relation is invalidated
    active = await db.get_active_relations_between("用户", "篮球")
    assert len(active) == 0
    await manager.close()


@pytest.mark.asyncio
async def test_merge_relation_v2_deduplicates_identical_fact(tmp_path):
    """相同事实不重复插入，仅追加 episode 引用。"""
    manager = DatabaseManager(tmp_path / "dedup.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraphV2(db_v2=db, vector_store=None)

    await db.insert_entity_v2("ENT-u", "用户", "人物", [], "")
    await db.insert_entity_v2("ENT-b", "篮球", "概念", [], "")
    await db.insert_episode("EP-1", "对话1", "summary", 1000.0, time.time())
    await db.insert_relation_v2("REL-1", "用户", "喜欢", "篮球", "用户喜欢篮球", "EP-1", 1000.0)

    rel = {
        "from_entity": "用户",
        "relation_type": "喜欢",
        "to_entity": "篮球",
        "fact": "用户喜欢篮球",
    }
    is_new, invalidated = await kg.merge_relation_v2(rel, "EP-2", 2000.0)
    assert is_new is False
    assert len(invalidated) == 0

    # Verify episode ref was appended
    episodes = await db.get_episodes_for_fact("REL-1")
    ep_ids = {e["id"] for e in episodes}
    assert ep_ids == {"EP-1", "EP-2"}
    await manager.close()


@pytest.mark.asyncio
async def test_merge_entities_v2_increments_summary_version(tmp_path):
    """实体演化：summary 重写，version 递增。"""
    manager = DatabaseManager(tmp_path / "evol.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraphV2(db_v2=db, vector_store=None)
    # Mock LLM summary rewrite
    kg._call_free_model = AsyncMock(return_value="用户喜欢篮球，是篮球爱好者")

    # First insert
    await db.insert_entity_v2("ENT-u", "用户", "人物", ["喜欢篮球"], "喜欢篮球")
    # Merge with new observations
    await kg.merge_entities_v2(
        [{"name": "用户", "kind": "人物", "observations": ["每周打篮球"]}],
        episode_content="用户每周都打篮球",
        episode_time=2000.0,
    )
    ent = await db.get_entity_v2("用户")
    assert ent["summary"] == "用户喜欢篮球，是篮球爱好者"
    assert ent["summary_version"] == 1
    await manager.close()


@pytest.mark.asyncio
async def test_add_facts_from_episode_end_to_end(tmp_path):
    """完整 episode 摄入流程。"""
    manager = DatabaseManager(tmp_path / "e2e.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraphV2(db_v2=db, vector_store=None)
    # Mock LLM extraction
    kg.extract_from_summary = AsyncMock(return_value={
        "entities": [
            {"name": "用户", "kind": "人物", "observations": ["喜欢篮球"]},
            {"name": "篮球", "kind": "概念", "observations": ["团队运动"]},
        ],
        "relations": [
            {"from_entity": "用户", "relation_type": "喜欢", "to_entity": "篮球",
             "fact": "用户喜欢篮球"},
        ],
    })

    result = await kg.add_facts_from_episode("用户说喜欢打篮球", 1000.0)
    assert result["new_facts"] == 1
    assert result["invalidated"] == 0
    assert result["episode_id"].startswith("EP-")

    # Verify entity and relation were created
    ent = await db.get_entity_v2("用户")
    assert ent is not None
    rel = await db.get_active_relations_between("用户", "篮球")
    assert len(rel) == 1
    await manager.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_kg_v2_invalidation.py -v`
Expected: FAIL — `memory.knowledge_graph_v2` module does not exist.

- [ ] **Step 3: Implement KnowledgeGraphV2**

Create `memory/knowledge_graph_v2.py`:

```python
"""KnowledgeGraphV2 — 基于 graphiti 核心机制的时序知识图谱。

功能:
- Episode 摄入 + LLM 提取
- 事实超驰 (矛盾检测 + 时间窗口冲突解析)
- 实体演化 (替换式 summary 重写)
- 社区发现 (Task 6 扩展)
"""
import json
import time
import uuid
from typing import Any

from loguru import logger

from memory.knowledge_graph import KnowledgeGraph, _clean_json_response, _repair_json, _normalize_json_keys


ENTITY_EXTRACT_PROMPT_V2 = """从以下对话摘要中提取关键实体和关系，只提取最显著的3-5个。

严格输出JSON，不要添加任何其他文字。格式如下：
{{"entities": [{{"name": "实体名", "kind": "人物/游戏/地点/概念/物品", "observations": ["观察1"]}}], "relations": [{{"from_entity": "实体A", "relation_type": "关系类型", "to_entity": "实体B", "fact": "自然语言事实陈述"}}]}}

规则：
1. 只提取明确提及的实体，不要推测
2. observations 是关于实体的具体描述
3. relation_type 使用简洁的动词短语，如"喜欢"、"属于"、"住在"
4. fact 是对关系的自然语言完整陈述，如"用户喜欢打篮球"
5. 如果没有明确的实体和关系，返回 {{"entities": [], "relations": []}}

对话摘要：
{summary}"""


CONTRADICTION_PROMPT = """判断新事实是否与已有事实矛盾。

新事实: {new_fact}
已有事实: {existing_facts_list}

规则:
1. 如果新事实与已有事实表达相同含义，不算矛盾
2. 如果新事实使已有事实不再成立，算矛盾
3. 输出JSON: {{"contradicted_indices": [索引列表]}}

输出JSON:"""


SUMMARY_REWRITE_PROMPT = """你是知识压缩助手。将旧摘要和新信息融合为一条精简摘要。

旧摘要: {old_summary}
新信息: {new_observations}
实体名: {entity_name}

要求:
1. 保留所有关键事实
2. 去除冗余和重复
3. 不超过200字
4. 直接输出摘要文本，不要加任何标记"""


class KnowledgeGraphV2(KnowledgeGraph):
    """KG v2: 时序事实、实体演化、Episode溯源、社区发现。

    继承 KnowledgeGraph 以复用 _call_free_model / set_free_model_client。
    """

    def __init__(
        self,
        db_v2: Any,
        vector_store: Any = None,
        router: Any = None,
    ) -> None:
        super().__init__(db=None, knowledge_db=None, router=router)
        self._db_v2 = db_v2
        self._vector_store = vector_store

    @property
    def _conn(self) -> Any:
        return self._db_v2._conn

    async def extract_from_summary(self, summary: str) -> dict:
        """使用 V2 prompt 提取实体和关系（含 fact 字段）。"""
        if not summary:
            return {"entities": [], "relations": []}
        try:
            prompt = ENTITY_EXTRACT_PROMPT_V2.format(summary=summary[:500])
            messages = [
                {"role": "system", "content": "你是一个知识提取助手，只输出纯JSON，不要输出任何其他内容，不要用markdown代码块包裹。"},
                {"role": "user", "content": prompt},
            ]
            result = await self._call_free_model(messages, temperature=0.1, max_tokens=800)
            if result is None and self._router:
                result = await self._router.route(
                    "memory_encoding", messages, temperature=0.1,
                    user_openid="system", session_id="kg_v2_extract",
                )
            if isinstance(result, str):
                cleaned = _clean_json_response(result)
                try:
                    parsed = json.loads(cleaned)
                except json.JSONDecodeError:
                    repaired = _repair_json(cleaned)
                    parsed = json.loads(repaired)
                if isinstance(parsed, list) and len(parsed) > 0:
                    parsed = parsed[0] if isinstance(parsed[0], dict) else {}
                if not isinstance(parsed, dict):
                    return {"entities": [], "relations": []}
                parsed = _normalize_json_keys(parsed)
                entities = parsed.get("entities", [])
                relations = parsed.get("relations", [])
                if not isinstance(entities, list):
                    entities = []
                if not isinstance(relations, list):
                    relations = []
                return {"entities": entities[:5], "relations": relations[:5]}
        except Exception as e:
            logger.warning("kg_v2.extract_failed", error=str(e))
        return {"entities": [], "relations": []}

    async def add_facts_from_episode(
        self,
        episode_content: str,
        episode_time: float,
        source_type: str = "summary",
    ) -> dict:
        """从 Episode 提取并合并事实。"""
        episode_id = f"EP-{uuid.uuid4().hex[:12]}"
        now = time.time()
        await self._db_v2.insert_episode(
            episode_id, episode_content, source_type, episode_time, now
        )

        extracted = await self.extract_from_summary(episode_content)
        if not extracted.get("entities") and not extracted.get("relations"):
            return {"episode_id": episode_id, "new_facts": 0, "invalidated": 0}

        await self.merge_entities_v2(extracted["entities"], episode_content, episode_time)

        invalidated_count = 0
        new_facts_count = 0
        for rel in extracted.get("relations", []):
            is_new, invalidated = await self.merge_relation_v2(rel, episode_id, episode_time)
            new_facts_count += int(is_new)
            invalidated_count += len(invalidated)

        return {
            "episode_id": episode_id,
            "new_facts": new_facts_count,
            "invalidated": invalidated_count,
        }

    async def merge_entities_v2(
        self,
        entities: list[dict],
        episode_content: str,
        episode_time: float,
    ) -> None:
        """实体演化: summary 替换式重写, version 递增。"""
        for ent in entities[:5]:
            try:
                name = ent.get("name", "")
                if not name:
                    continue
                kind = ent.get("kind", "")
                new_obs = ent.get("observations", [])
                existing = await self._db_v2.get_entity_v2(name)

                if existing:
                    old_summary = existing.get("summary", "")
                    if old_summary and len(old_summary) + len(str(new_obs)) < 200:
                        new_summary = f"{old_summary}; {'; '.join(new_obs)}" if new_obs else old_summary
                    elif old_summary:
                        new_summary = await self._rewrite_summary(old_summary, new_obs, name)
                    else:
                        new_summary = "; ".join(new_obs) if new_obs else ""

                    rowid = await self._db_v2.update_entity_summary_v2(
                        name, new_summary,
                        summary_version=existing.get("summary_version", 0) + 1,
                    )
                    # 同步向量
                    if self._vector_store and rowid:
                        await self._vector_store.upsert_kg_entity(
                            rowid, f"{name}: {new_summary}"
                        )
                else:
                    entity_id = f"ENT-{uuid.uuid4().hex[:12]}"
                    summary = "; ".join(new_obs) if new_obs else ""
                    rowid = await self._db_v2.insert_entity_v2(
                        entity_id, name, kind, new_obs, summary
                    )
                    if self._vector_store and rowid:
                        await self._vector_store.upsert_kg_entity(
                            rowid, f"{name}: {summary}"
                        )
            except Exception as e:
                logger.warning("kg_v2.merge_entity_failed", name=ent.get("name", ""), error=str(e))

    async def _rewrite_summary(
        self, old_summary: str, new_observations: list, entity_name: str
    ) -> str:
        """LLM 重写 summary。"""
        prompt = SUMMARY_REWRITE_PROMPT.format(
            old_summary=old_summary,
            new_observations=", ".join(new_observations),
            entity_name=entity_name,
        )
        messages = [{"role": "user", "content": prompt}]
        result = await self._call_free_model(messages, temperature=0.3, max_tokens=300)
        if result and isinstance(result, str):
            return result.strip()
        return f"{old_summary}; {'; '.join(new_observations)}"

    async def merge_relation_v2(
        self,
        relation: dict,
        episode_id: str,
        episode_time: float,
    ) -> tuple[bool, list[dict]]:
        """合并新关系，自动处理超驰。Returns: (is_new, invalidated_relations)。"""
        from_entity = relation.get("from_entity", "")
        relation_type = relation.get("relation_type", "")
        to_entity = relation.get("to_entity", "")
        fact = relation.get("fact", f"{from_entity} {relation_type} {to_entity}")

        if not from_entity or not relation_type or not to_entity:
            return False, []

        existing = await self._db_v2.get_active_relations_between(from_entity, to_entity)
        conflict_candidates = [
            r for r in existing
            if r["relation_type"] == relation_type and r.get("is_current", 1) == 1
        ]

        invalidated: list[dict] = []
        is_duplicate = False

        if conflict_candidates:
            # 精确匹配检查
            for candidate in conflict_candidates:
                if candidate.get("fact", "") == fact:
                    is_duplicate = True
                    await self._db_v2.append_episode_ref(candidate["id"], episode_id)
                    break

            # LLM 矛盾检测
            if not is_duplicate:
                contradictions = await self._detect_contradictions(
                    new_fact=fact,
                    existing_facts=[r.get("fact", "") for r in conflict_candidates],
                )
                for idx in contradictions:
                    if idx < len(conflict_candidates):
                        candidate = conflict_candidates[idx]
                        if self._resolve_contradiction(candidate, episode_time):
                            await self._db_v2.invalidate_relation(
                                candidate["id"],
                                invalid_at=candidate["invalid_at"],
                                expired_at=candidate.get("expired_at", time.time()),
                            )
                            invalidated.append(candidate)

        # 插入新关系
        if not is_duplicate:
            rel_id = f"REL-{uuid.uuid4().hex[:12]}"
            rowid = await self._db_v2.insert_relation_v2(
                rel_id, from_entity, relation_type, to_entity, fact,
                episode_id, episode_time,
            )
            # 同步事实向量
            if self._vector_store and rowid:
                await self._vector_store.upsert_kg_relation(rowid, fact)

        return not is_duplicate, invalidated

    async def _detect_contradictions(
        self, new_fact: str, existing_facts: list[str]
    ) -> list[int]:
        """LLM 矛盾检测，返回被矛盾的已有事实索引列表。"""
        if not existing_facts:
            return []
        try:
            facts_list = "\n".join(
                f"{i}. {f}" for i, f in enumerate(existing_facts)
            )
            prompt = CONTRADICTION_PROMPT.format(
                new_fact=new_fact, existing_facts_list=facts_list
            )
            messages = [{"role": "user", "content": prompt}]
            result = await self._call_free_model(messages, temperature=0.0, max_tokens=200)
            if result and isinstance(result, str):
                cleaned = _clean_json_response(result)
                parsed = json.loads(cleaned)
                indices = parsed.get("contradicted_indices", [])
                if isinstance(indices, list):
                    return [int(i) for i in indices if 0 <= i < len(existing_facts)]
        except Exception as e:
            logger.debug("kg_v2.detect_contradictions_failed", error=str(e))
        return []

    def _resolve_contradiction(
        self, old_relation: dict, new_valid_at: float
    ) -> bool:
        """时间窗口冲突解析。返回是否标记了旧关系失效。"""
        old_valid_at = old_relation.get("valid_at") or 0
        old_invalid_at = old_relation.get("invalid_at")

        if old_invalid_at and old_invalid_at <= new_valid_at:
            return False

        if old_valid_at < new_valid_at:
            old_relation["invalid_at"] = new_valid_at
            old_relation["expired_at"] = time.time()
            old_relation["is_current"] = 0
            return True

        return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_kg_v2_invalidation.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add memory/knowledge_graph_v2.py tests/test_kg_v2_invalidation.py
git commit -m "feat(kg-v2): add KnowledgeGraphV2 with fact supersession and entity evolution"
```

---

### Task 5: Hybrid Search Engine

**Files:**
- Create: `memory/kg_search.py`
- Test: `tests/test_kg_v2_search.py` (append search engine tests)

**Interfaces:**
- Consumes:
  - `KnowledgeDBV2` from Task 2 (for entity/relation lookups by rowid).
  - `VectorStore` from Task 3 (`search_kg_entities`, `search_kg_relations`).
  - `aiosqlite.Connection` for FTS5 and graph traversal queries.
  - `KnowledgeGraph.get_query_entities()` (inherited by KnowledgeGraphV2) for graph search seed extraction.
- Produces:
  - `KGSearchEngine(db: KnowledgeDBV2, vector_store: Any, conn: aiosqlite.Connection)` — constructor.
  - `async search(query: str, top_k: int = 10, as_of: float | None = None) -> list[dict]` — returns fused results with `rrf_score`.
  - `async _semantic_search(query: str, k: int) -> list[dict]`
  - `async _fulltext_search(query: str, k: int) -> list[dict]`
  - `async _graph_search(query: str, k: int) -> list[dict]`
  - `_rrf_fuse(ranked_lists: list[list[dict]], k: int = 60) -> list[dict]`

- [ ] **Step 1: Write the failing test for RRF fusion and search**

Append to `tests/test_kg_v2_search.py`:

```python
# ── KGSearchEngine tests ──────────────────────────────────────

import asyncio
from memory.kg_search import KGSearchEngine


def test_rrf_fuse_combines_ranked_lists():
    """RRF 融合: 多路结果按 1/(k+rank) 求和排序。"""
    engine = KGSearchEngine.__new__(KGSearchEngine)
    list_a = [{"type": "entity", "id": "E1"}, {"type": "entity", "id": "E2"}]
    list_b = [{"type": "entity", "id": "E2"}, {"type": "entity", "id": "E3"}]
    fused = engine._rrf_fuse([list_a, list_b], k=60)
    # E2 appears in both lists → highest score
    assert fused[0]["id"] == "E2"
    assert "rrf_score" in fused[0]
    # E1 and E3 appear in one list each
    ids = [f["id"] for f in fused]
    assert "E1" in ids
    assert "E3" in ids


def test_rrf_fuse_empty_lists():
    engine = KGSearchEngine.__new__(KGSearchEngine)
    assert engine._rrf_fuse([], k=60) == []
    assert engine._rrf_fuse([[], []], k=60) == []


@pytest.mark.asyncio
async def test_search_returns_current_facts_only_by_default(tmp_path):
    """默认只返回当前有效事实 (is_current=1)。"""
    manager = DatabaseManager(tmp_path / "search_curr.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    # Insert data
    await db.insert_entity_v2("ENT-u", "用户", "人物", [], "用户是人物")
    await db.insert_entity_v2("ENT-b", "篮球", "概念", [], "篮球是运动")
    await db.insert_episode("EP-1", "用户喜欢篮球", "summary", 1000.0, time.time())
    await db.insert_relation_v2("REL-1", "用户", "喜欢", "篮球", "用户喜欢篮球", "EP-1", 1000.0)
    # Insert invalidated relation
    await db.insert_episode("EP-2", "用户改打网球", "summary", 2000.0, time.time())
    await db.insert_entity_v2("ENT-t", "网球", "概念", [], "网球是运动")
    await db.insert_relation_v2("REL-2", "用户", "喜欢", "网球", "用户喜欢网球", "EP-2", 2000.0)
    await db.invalidate_relation("REL-1", invalid_at=2000.0, expired_at=2001.0)

    engine = KGSearchEngine(db=db, vector_store=None, conn=manager._conn)
    results = await engine.search("篮球", top_k=10)
    # REL-1 is invalidated → should not appear
    rel_ids = [r["id"] for r in results if r.get("type") == "relation"]
    assert "REL-1" not in rel_ids
    await manager.close()


@pytest.mark.asyncio
async def test_search_with_as_of_returns_historical_snapshot(tmp_path):
    """as_of 时间戳返回历史快照。"""
    manager = DatabaseManager(tmp_path / "search_asof.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    await db.insert_entity_v2("ENT-u", "用户", "人物", [], "")
    await db.insert_entity_v2("ENT-b", "篮球", "概念", [], "")
    await db.insert_episode("EP-1", "用户喜欢篮球", "summary", 1000.0, time.time())
    await db.insert_relation_v2("REL-1", "用户", "喜欢", "篮球", "用户喜欢篮球", "EP-1", 1000.0)
    await db.invalidate_relation("REL-1", invalid_at=2000.0, expired_at=2001.0)

    engine = KGSearchEngine(db=db, vector_store=None, conn=manager._conn)
    # as_of=1500 → REL-1 was still valid
    results = await engine.search("篮球", top_k=10, as_of=1500.0)
    rel_ids = [r["id"] for r in results if r.get("type") == "relation"]
    assert "REL-1" in rel_ids
    await manager.close()


@pytest.mark.asyncio
async def test_fulltext_search_finds_entities_by_summary(tmp_path):
    """FTS5 全文搜索实体。"""
    manager = DatabaseManager(tmp_path / "fts_search.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    await db.insert_entity_v2("ENT-1", "篮球", "概念", [], "团队运动，需要五个球员")
    await db.insert_entity_v2("ENT-2", "足球", "概念", [], "另一种运动")

    engine = KGSearchEngine(db=db, vector_store=None, conn=manager._conn)
    results = await engine._fulltext_search("篮球", k=5)
    ids = [r["id"] for r in results]
    assert "ENT-1" in ids
    await manager.close()


@pytest.mark.asyncio
async def test_graph_search_finds_neighbors(tmp_path):
    """图遍历搜索找到邻居实体。"""
    manager = DatabaseManager(tmp_path / "graph.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    await db.insert_entity_v2("ENT-u", "用户", "人物", [], "")
    await db.insert_entity_v2("ENT-b", "篮球", "概念", [], "")
    await db.insert_entity_v2("ENT-t", "网球", "概念", [], "")
    await db.insert_episode("EP-1", "", "summary", 1000.0, time.time())
    await db.insert_relation_v2("REL-1", "用户", "喜欢", "篮球", "用户喜欢篮球", "EP-1", 1000.0)
    await db.insert_episode("EP-2", "", "summary", 2000.0, time.time())
    await db.insert_relation_v2("REL-2", "用户", "喜欢", "网球", "用户喜欢网球", "EP-2", 2000.0)

    engine = KGSearchEngine(db=db, vector_store=None, conn=manager._conn)
    # Mock entity extraction from query
    engine._extract_query_entities = AsyncMock(return_value={"用户"})
    results = await engine._graph_search("用户", k=5)
    names = [r["id"] for r in results]
    assert "篮球" in names or "网球" in names
    await manager.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_kg_v2_search.py::test_rrf_fuse_combines_ranked_lists -v`
Expected: FAIL — `memory.kg_search` module does not exist.

- [ ] **Step 3: Implement KGSearchEngine**

Create `memory/kg_search.py`:

```python
"""KGSearchEngine — 混合检索引擎: 语义 + 全文 + 图遍历, RRF 融合。"""
import asyncio
import json
from typing import Any

import aiosqlite
from loguru import logger

from db.db_kg_v2 import KnowledgeDBV2
from db.fts_utils import _build_fts_query


class KGSearchEngine:
    """混合检索引擎，融合语义、全文、图三路搜索结果。"""

    def __init__(
        self,
        db: KnowledgeDBV2,
        vector_store: Any,
        conn: aiosqlite.Connection,
    ) -> None:
        self._db = db
        self._vector_store = vector_store
        self._conn = conn

    async def search(
        self,
        query: str,
        top_k: int = 10,
        as_of: float | None = None,
    ) -> list[dict]:
        """混合检索: 语义 + 全文 + 图遍历, RRF 融合。

        Args:
            query: 查询文本
            top_k: 返回条数
            as_of: None=只返回当前有效; 时间戳=历史快照
        """
        results = await asyncio.gather(
            self._semantic_search(query, top_k * 2),
            self._fulltext_search(query, top_k * 2),
            self._graph_search(query, top_k * 2),
        )
        fused = self._rrf_fuse(results, k=60)

        # 时序过滤
        if as_of is None:
            fused = [r for r in fused if r.get("is_current", 1) == 1]
        else:
            filtered = []
            for r in fused:
                valid_at = r.get("valid_at") or 0
                invalid_at = r.get("invalid_at")
                if valid_at <= as_of and (invalid_at is None or invalid_at > as_of):
                    filtered.append(r)
            fused = filtered

        return fused[:top_k]

    async def _semantic_search(self, query: str, k: int) -> list[dict]:
        """语义搜索: sqlite-vec KNN。"""
        if not self._vector_store:
            return []
        try:
            entity_hits = await self._vector_store.search_kg_entities(query, top_k=k)
            relation_hits = await self._vector_store.search_kg_relations(query, top_k=k)
        except Exception as e:
            logger.debug("kg_search.semantic_failed", error=str(e))
            return []

        results = []
        # 实体命中
        for rowid, distance in entity_hits:
            cursor = await self._conn.execute(
                "SELECT id, name, kind, summary FROM kg_entities_v2 WHERE rowid=?", (rowid,)
            )
            row = await cursor.fetchone()
            if row:
                results.append({
                    "type": "entity",
                    "id": row["id"],
                    "name": row["name"],
                    "kind": row["kind"],
                    "summary": row["summary"],
                    "distance": distance,
                })
        # 关系命中
        for rowid, distance in relation_hits:
            cursor = await self._conn.execute(
                "SELECT id, from_entity, relation_type, to_entity, fact, valid_at, invalid_at, is_current "
                "FROM kg_relations_v2 WHERE rowid=?", (rowid,)
            )
            row = await cursor.fetchone()
            if row:
                results.append({
                    "type": "relation",
                    "id": row["id"],
                    "from_entity": row["from_entity"],
                    "relation_type": row["relation_type"],
                    "to_entity": row["to_entity"],
                    "fact": row["fact"],
                    "valid_at": row["valid_at"],
                    "invalid_at": row["invalid_at"],
                    "is_current": row["is_current"],
                    "distance": distance,
                })
        return results

    async def _fulltext_search(self, query: str, k: int) -> list[dict]:
        """FTS5 BM25 全文搜索。"""
        fts_query = _build_fts_query(query)
        if not fts_query:
            return []
        results = []
        try:
            # 实体: name + summary
            cursor = await self._conn.execute(
                """SELECT e.id, e.name, e.kind, e.summary
                   FROM kg_entities_v2_fts fts
                   JOIN kg_entities_v2 e ON e.id = fts.id
                   WHERE fts MATCH ?
                   ORDER BY rank LIMIT ?""",
                (fts_query, k),
            )
            for row in await cursor.fetchall():
                results.append({
                    "type": "entity",
                    "id": row["id"],
                    "name": row["name"],
                    "kind": row["kind"],
                    "summary": row["summary"],
                })
            # 关系: fact
            cursor = await self._conn.execute(
                """SELECT r.id, r.from_entity, r.relation_type, r.to_entity, r.fact,
                          r.valid_at, r.invalid_at, r.is_current
                   FROM kg_relations_v2_fts fts
                   JOIN kg_relations_v2 r ON r.id = fts.id
                   WHERE fts MATCH ?
                   ORDER BY rank LIMIT ?""",
                (fts_query, k),
            )
            for row in await cursor.fetchall():
                results.append({
                    "type": "relation",
                    "id": row["id"],
                    "from_entity": row["from_entity"],
                    "relation_type": row["relation_type"],
                    "to_entity": row["to_entity"],
                    "fact": row["fact"],
                    "valid_at": row["valid_at"],
                    "invalid_at": row["invalid_at"],
                    "is_current": row["is_current"],
                })
        except Exception as e:
            logger.debug("kg_search.fulltext_failed", error=str(e))
        return results

    async def _graph_search(self, query: str, k: int) -> list[dict]:
        """图遍历搜索: 递归 CTE BFS。"""
        entities = await self._extract_query_entities(query)
        if not entities:
            return []
        results = []
        for seed in list(entities)[:3]:
            try:
                cursor = await self._conn.execute(
                    """WITH RECURSIVE bfs(entity, depth) AS (
                        SELECT ?, 0
                        UNION ALL
                        SELECT CASE WHEN r.from_entity = b.entity THEN r.to_entity
                                    ELSE r.from_entity END, b.depth + 1
                        FROM kg_relations_v2 r JOIN bfs b
                          ON (r.from_entity = b.entity OR r.to_entity = b.entity)
                        WHERE b.depth < 2 AND r.is_current = 1
                    )
                    SELECT DISTINCT entity, MIN(depth) as min_depth FROM bfs
                    GROUP BY entity ORDER BY min_depth LIMIT ?""",
                    (seed, k),
                )
                rows = await cursor.fetchall()
                for r in rows:
                    results.append({
                        "type": "entity",
                        "id": r[0],
                        "name": r[0],
                        "graph_distance": r[1],
                    })
            except Exception as e:
                logger.debug("kg_search.graph_failed", seed=seed, error=str(e))
        return results

    async def _extract_query_entities(self, query: str) -> set[str]:
        """从查询中提取实体名 (简单分词, 无 LLM 调用)。"""
        # 简单实现: 按空格和标点分词, 取长度>=2的词
        # 生产环境可注入 KnowledgeGraph.get_query_entities
        import re
        tokens = re.split(r'[\s,，。.!！?？、的了吗呢吧]', query)
        return {t.strip() for t in tokens if len(t.strip()) >= 2}

    def _rrf_fuse(self, ranked_lists: list[list[dict]], k: int = 60) -> list[dict]:
        """Reciprocal Rank Fusion: score = Σ 1/(k + rank)。"""
        scores: dict[str, float] = {}
        items: dict[str, dict] = {}
        for ranked in ranked_lists:
            for rank, item in enumerate(ranked):
                key = f"{item.get('type', '')}:{item.get('id', '')}"
                scores[key] = scores.get(key, 0) + 1.0 / (k + rank)
                if key not in items:
                    items[key] = item
        sorted_keys = sorted(scores.keys(), key=lambda x: -scores[x])
        return [{**items[key], "rrf_score": scores[key]} for key in sorted_keys]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_kg_v2_search.py -v`
Expected: All tests PASS (vector + search engine).

- [ ] **Step 5: Commit**

```bash
git add memory/kg_search.py tests/test_kg_v2_search.py
git commit -m "feat(kg-v2): add KGSearchEngine with semantic+fulltext+graph RRF fusion"
```

---

### Task 6: Community Detection

**Files:**
- Modify: `memory/knowledge_graph_v2.py` (append community detection methods)
- Test: `tests/test_kg_v2_community.py`

**Interfaces:**
- Consumes: `KnowledgeGraphV2` from Task 4, `KnowledgeDBV2` from Task 2, `aiosqlite.Connection`.
- Produces:
  - `async detect_communities() -> list[list[str]]` — returns list of entity-name clusters.
  - `_label_propagation(adjacency: dict[str, list[tuple[str, int]]], max_iter: int = 10) -> list[list[str]]`
  - `async _build_community_summary(member_names: list[str]) -> None`
  - `async update_community_for_entity(entity_name: str) -> None`

- [ ] **Step 1: Write the failing test for community detection**

Create `tests/test_kg_v2_community.py`:

```python
"""KG v2 社区发现测试 — 标签传播 + 社区摘要。"""
import time
from unittest.mock import AsyncMock

import pytest

from db.database import DatabaseManager
from db.db_kg_v2 import KnowledgeDBV2
from memory.knowledge_graph_v2 import KnowledgeGraphV2


def test_label_propagation_clusters_connected_nodes():
    """标签传播: 相连实体聚类到同一社区。"""
    kg = KnowledgeGraphV2.__new__(KnowledgeGraphV2)
    adjacency = {
        "A": [("B", 2), ("C", 1)],
        "B": [("A", 2), ("C", 1)],
        "C": [("A", 1), ("B", 1)],
        "D": [("E", 2)],
        "E": [("D", 2)],
    }
    clusters = kg._label_propagation(adjacency, max_iter=10)
    assert len(clusters) >= 2
    # A, B, C should be in the same cluster
    for cluster in clusters:
        if "A" in cluster:
            assert "B" in cluster
            assert "C" in cluster
        if "D" in cluster:
            assert "E" in cluster


def test_label_propagation_empty_adjacency():
    kg = KnowledgeGraphV2.__new__(KnowledgeGraphV2)
    assert kg._label_propagation({}, max_iter=10) == []


def test_label_propagation_single_node():
    kg = KnowledgeGraphV2.__new__(KnowledgeGraphV2)
    adjacency = {"X": []}
    clusters = kg._label_propagation(adjacency, max_iter=10)
    assert len(clusters) == 1
    assert clusters[0] == ["X"]


@pytest.mark.asyncio
async def test_detect_communities_creates_community_records(tmp_path):
    """社区发现: 检测社区并写入 kg_communities 表。"""
    manager = DatabaseManager(tmp_path / "comm.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraphV2(db_v2=db, vector_store=None)
    # Mock LLM for community naming
    kg._call_free_model = AsyncMock(return_value="运动社区")

    # Build a graph: A-B-C cluster, D-E cluster
    await db.insert_entity_v2("ENT-a", "A", "概念", [], "A的摘要")
    await db.insert_entity_v2("ENT-b", "B", "概念", [], "B的摘要")
    await db.insert_entity_v2("ENT-c", "C", "概念", [], "C的摘要")
    await db.insert_entity_v2("ENT-d", "D", "概念", [], "D的摘要")
    await db.insert_entity_v2("ENT-e", "E", "概念", [], "E的摘要")
    await db.insert_episode("EP-1", "", "summary", 1000.0, time.time())
    await db.insert_relation_v2("R1", "A", "connected", "B", "A连接B", "EP-1", 1000.0)
    await db.insert_relation_v2("R2", "B", "connected", "C", "B连接C", "EP-1", 1000.0)
    await db.insert_relation_v2("R3", "D", "connected", "E", "D连接E", "EP-1", 1000.0)

    clusters = await kg.detect_communities()
    assert len(clusters) >= 2

    # Verify communities were created in DB
    cursor = await manager._conn.execute("SELECT COUNT(*) as cnt FROM kg_communities")
    row = await cursor.fetchone()
    assert row["cnt"] >= 1
    await manager.close()


@pytest.mark.asyncio
async def test_update_community_for_entity(tmp_path):
    """增量更新: 新实体加入邻居社区。"""
    manager = DatabaseManager(tmp_path / "incr.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraphV2(db_v2=db, vector_store=None)

    # Create existing community with members
    await db.insert_entity_v2("ENT-a", "A", "概念", [], "")
    await db.insert_entity_v2("ENT-b", "B", "概念", [], "")
    await db.insert_community("COM-1", "测试社区", "摘要", ["A", "B"])

    # Add new entity connected to A
    await db.insert_entity_v2("ENT-c", "C", "概念", [], "")
    await db.insert_episode("EP-1", "", "summary", 1000.0, time.time())
    await db.insert_relation_v2("R1", "C", "connected", "A", "C连接A", "EP-1", 1000.0)

    await kg.update_community_for_entity("C")
    comm_id = await db.get_entity_community("C")
    assert comm_id == "COM-1"
    await manager.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_kg_v2_community.py -v`
Expected: FAIL — `detect_communities`, `_label_propagation` methods do not exist on KnowledgeGraphV2.

- [ ] **Step 3: Implement community detection methods in KnowledgeGraphV2**

Append to `memory/knowledge_graph_v2.py` (inside the `KnowledgeGraphV2` class, after `_resolve_contradiction`):

```python
    # ── 社区发现 ──────────────────────────────────────────────

    async def detect_communities(self) -> list[list[str]]:
        """社区发现: 加载图投影 → 标签传播 → 生成社区摘要。"""
        cursor = await self._conn.execute("""
            SELECT from_entity, to_entity, COUNT(*) as edge_count
            FROM kg_relations_v2
            WHERE is_current = 1
            GROUP BY from_entity, to_entity
        """)
        rows = await cursor.fetchall()

        adjacency: dict[str, list[tuple[str, int]]] = {}
        for row in rows:
            f, t, cnt = row[0], row[1], row[2]
            adjacency.setdefault(f, []).append((t, cnt))
            adjacency.setdefault(t, []).append((f, cnt))

        if not adjacency:
            return []

        clusters = self._label_propagation(adjacency, max_iter=10)

        for cluster in clusters:
            if len(cluster) > 1:
                await self._build_community_summary(cluster)

        return clusters

    def _label_propagation(
        self,
        adjacency: dict[str, list[tuple[str, int]]],
        max_iter: int = 10,
    ) -> list[list[str]]:
        """标签传播算法: 纯 Python 内存计算。"""
        if not adjacency:
            return []
        labels = {node: i for i, node in enumerate(adjacency)}

        for _ in range(max_iter):
            no_change = True
            for node in adjacency:
                neighbor_labels: dict[int, int] = {}
                for neighbor, edge_count in adjacency[node]:
                    lbl = labels[neighbor]
                    neighbor_labels[lbl] = neighbor_labels.get(lbl, 0) + edge_count

                if not neighbor_labels:
                    continue

                best_label = max(neighbor_labels, key=neighbor_labels.get)
                if neighbor_labels[best_label] > 1 and labels[node] != best_label:
                    labels[node] = best_label
                    no_change = False

            if no_change:
                break

        communities: dict[int, list[str]] = {}
        for node, lbl in labels.items():
            communities.setdefault(lbl, []).append(node)
        return list(communities.values())

    async def _build_community_summary(self, member_names: list[str]) -> None:
        """为社区生成摘要并写入 kg_communities 表。"""
        placeholders = ",".join("?" * len(member_names))
        cursor = await self._conn.execute(
            f"SELECT name, summary FROM kg_entities_v2 WHERE name IN ({placeholders}) AND summary != ''",
            member_names,
        )
        rows = await cursor.fetchall()

        if not rows:
            return

        summaries = [r[1] for r in rows]
        if len(summaries) <= 4:
            combined = "; ".join(summaries)
        else:
            # 简单截断, 避免过多 LLM 调用
            combined = "; ".join(summaries[:4])

        community_id = f"COM-{uuid.uuid4().hex[:12]}"
        name = await self._generate_community_name(combined)
        await self._db_v2.insert_community(community_id, name, combined, member_names)

    async def _generate_community_name(self, combined_summary: str) -> str:
        """LLM 生成社区名称。"""
        prompt = f"根据以下信息生成一个简短的社区名称（不超过10个字）:\n{combined_summary[:200]}\n\n直接输出名称:"
        messages = [{"role": "user", "content": prompt}]
        result = await self._call_free_model(messages, temperature=0.3, max_tokens=50)
        if result and isinstance(result, str):
            return result.strip()[:20]
        return "未命名社区"

    async def update_community_for_entity(self, entity_name: str) -> None:
        """增量更新: 新增实体后, 查邻居社区归属, 取众数归入。"""
        cursor = await self._conn.execute(
            """SELECT r.from_entity, r.to_entity FROM kg_relations_v2 r
               WHERE r.is_current = 1 AND (r.from_entity = ? OR r.to_entity = ?)""",
            [entity_name, entity_name],
        )
        rows = await cursor.fetchall()

        neighbor_names = set()
        for row in rows:
            neighbor_names.add(row[0] if row[1] == entity_name else row[1])

        community_votes: dict[str, int] = {}
        for neighbor in neighbor_names:
            comm = await self._db_v2.get_entity_community(neighbor)
            if comm:
                community_votes[comm] = community_votes.get(comm, 0) + 1

        if community_votes:
            best = max(community_votes, key=community_votes.get)
            await self._db_v2.add_entity_to_community(entity_name, best)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_kg_v2_community.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add memory/knowledge_graph_v2.py tests/test_kg_v2_community.py
git commit -m "feat(kg-v2): add community detection with label propagation and summary generation"
```

---

### Task 7: Integration and Feature Flag

**Files:**
- Modify: `config.py` (add `KG_V2_ENABLED`)
- Modify: `db/database.py` (init KnowledgeDBV2 in `init()`)
- Modify: `memory/knowledge_graph.py` (add v2 branch in `auto_extract_and_merge`)
- Modify: `memory/memory_manager.py` (add v2 search path)
- Create: `tests/test_kg_v2_integration.py`

**Interfaces:**
- Consumes: All previous tasks (KnowledgeDBV2, KnowledgeGraphV2, KGSearchEngine, VectorStore extension).
- Produces:
  - `config.KG_V2_ENABLED` — boolean feature flag, default `True`.
  - `DatabaseManager.kg_v2` — KnowledgeDBV2 instance.
  - `KnowledgeGraph.set_kg_v2(kg_v2)` — inject v2 instance.
  - `KnowledgeGraph.auto_extract_and_merge()` — delegates to v2 when flag is on.
  - `MemoryManager` — KG v2 hybrid retrieval in search path.

- [ ] **Step 1: Write the failing integration test**

Create `tests/test_kg_v2_integration.py`:

```python
"""KG v2 集成测试 — 功能开关 + 端到端流程。"""
import os
import time
from unittest.mock import AsyncMock, patch

import pytest

from db.database import DatabaseManager
from db.db_kg_v2 import KnowledgeDBV2
from memory.knowledge_graph import KnowledgeGraph
from memory.knowledge_graph_v2 import KnowledgeGraphV2


@pytest.mark.asyncio
async def test_kg_v2_enabled_flag_defaults_true():
    """KG_V2_ENABLED 默认为 True。"""
    # Remove env var to test default
    os.environ.pop("KG_V2_ENABLED", None)
    import importlib
    import config
    importlib.reload(config)
    assert config.KG_V2_ENABLED is True


@pytest.mark.asyncio
async def test_kg_v2_flag_can_be_disabled():
    """KG_V2_ENABLED=false 关闭 v2。"""
    os.environ["KG_V2_ENABLED"] = "false"
    import importlib
    import config
    importlib.reload(config)
    assert config.KG_V2_ENABLED is False
    os.environ.pop("KG_V2_ENABLED", None)
    importlib.reload(config)


@pytest.mark.asyncio
async def test_database_manager_has_kg_v2_instance(tmp_path):
    """DatabaseManager.init() 创建 KnowledgeDBV2 实例。"""
    manager = DatabaseManager(tmp_path / "mgr.db")
    await manager.init()
    assert manager.kg_v2 is not None
    assert isinstance(manager.kg_v2, KnowledgeDBV2)
    await manager.close()


@pytest.mark.asyncio
async def test_auto_extract_uses_v2_when_enabled(tmp_path):
    """功能开关开启时, auto_extract_and_merge 调用 v2。"""
    manager = DatabaseManager(tmp_path / "v2_on.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraph(knowledge_db=manager.knowledge)
    kg_v2 = KnowledgeGraphV2(db_v2=db, vector_store=None)
    kg.set_kg_v2(kg_v2)

    # Mock v2 method to track call
    kg_v2.add_facts_from_episode = AsyncMock(return_value={
        "episode_id": "EP-mock", "new_facts": 0, "invalidated": 0
    })

    with patch("config.KG_V2_ENABLED", True):
        await kg.auto_extract_and_merge("用户说喜欢篮球")

    kg_v2.add_facts_from_episode.assert_called_once()
    await manager.close()


@pytest.mark.asyncio
async def test_auto_extract_falls_back_to_v1_when_disabled(tmp_path):
    """功能开关关闭时, auto_extract_and_merge 走 v1 逻辑。"""
    manager = DatabaseManager(tmp_path / "v1_fallback.db")
    await manager.init()
    kg = KnowledgeGraph(knowledge_db=manager.knowledge)
    db = KnowledgeDBV2(manager._conn)
    kg_v2 = KnowledgeGraphV2(db_v2=db, vector_store=None)
    kg.set_kg_v2(kg_v2)

    # Mock both v1 and v2 methods
    kg_v2.add_facts_from_episode = AsyncMock(return_value={
        "episode_id": "EP-mock", "new_facts": 0, "invalidated": 0
    })
    kg.extract_from_summary = AsyncMock(return_value={"entities": [], "relations": []})

    with patch("config.KG_V2_ENABLED", False):
        await kg.auto_extract_and_merge("用户说喜欢篮球")

    # v2 should NOT be called
    kg_v2.add_facts_from_episode.assert_not_called()
    # v1 extract_from_summary SHOULD be called
    kg.extract_from_summary.assert_called_once()
    await manager.close()


@pytest.mark.asyncio
async def test_end_to_end_episode_to_search(tmp_path):
    """端到端: Episode 摄入 → 混合检索 → 验证结果。"""
    from memory.kg_search import KGSearchEngine

    manager = DatabaseManager(tmp_path / "e2e_int.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraphV2(db_v2=db, vector_store=None)

    # Mock LLM extraction
    kg.extract_from_summary = AsyncMock(return_value={
        "entities": [
            {"name": "用户", "kind": "人物", "observations": ["喜欢篮球"]},
            {"name": "篮球", "kind": "概念", "observations": ["团队运动"]},
        ],
        "relations": [
            {"from_entity": "用户", "relation_type": "喜欢", "to_entity": "篮球",
             "fact": "用户喜欢篮球"},
        ],
    })

    # Step 1: Episode ingestion
    result = await kg.add_facts_from_episode("用户说喜欢打篮球", time.time())
    assert result["new_facts"] == 1

    # Step 2: Search (without vector store, only fulltext + graph)
    engine = KGSearchEngine(db=db, vector_store=None, conn=manager._conn)
    results = await engine.search("篮球", top_k=10)
    # Should find the relation "用户喜欢篮球" via FTS5
    rel_results = [r for r in results if r.get("type") == "relation"]
    assert len(rel_results) >= 1
    assert any(r["fact"] == "用户喜欢篮球" for r in rel_results)

    await manager.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_kg_v2_integration.py -v`
Expected: FAIL — `config.KG_V2_ENABLED` does not exist; `manager.kg_v2` is None; `kg.set_kg_v2` does not exist.

- [ ] **Step 3: Add `KG_V2_ENABLED` to `config.py`**

In `config.py`, after the `PARENT_CHILD_CHUNK_ENABLED` line (around line 810), add:

```python
# ── KG v2 知识图谱优化 ──
KG_V2_ENABLED = os.getenv("KG_V2_ENABLED", "true").lower() in ("1", "true", "yes")
```

- [ ] **Step 4: Add KnowledgeDBV2 initialization to `DatabaseManager.init()`**

In `db/database.py`, first add the import at the top (after line 12):

```python
from .db_kg_v2 import KnowledgeDBV2
```

Then in `DatabaseManager.__init__` (around line 70), add:

```python
        self.kg_v2: KnowledgeDBV2 | None = None
```

Then in `DatabaseManager.init()` (after line 130 `self.temporal = TemporalMemoryDB(self._conn)`), add:

```python
        self.kg_v2 = KnowledgeDBV2(self._conn)
```

The full init block should look like:

```python
        self.notebook = NotebookDB(self._conn)
        self.learning = LearningDB(self._conn)
        self.knowledge = KnowledgeDB(self._conn)
        self.analytics = AnalyticsDB(self._conn)
        self.temporal = TemporalMemoryDB(self._conn)
        self.kg_v2 = KnowledgeDBV2(self._conn)
        logger.info("database.ready", path=str(self.db_path))
```

- [ ] **Step 5: Add v2 branch to `KnowledgeGraph.auto_extract_and_merge`**

In `memory/knowledge_graph.py`, modify `auto_extract_and_merge` (line 379). Add `set_kg_v2` method and v2 branch:

```python
    def set_kg_v2(self, kg_v2: Any) -> None:
        """注入 KnowledgeGraphV2 实例。"""
        self._kg_v2 = kg_v2

    async def auto_extract_and_merge(self, summary: str) -> None:
        if not summary:
            return

        # KG v2 分支: 功能开关开启时走 v2 路径
        try:
            import config as _cfg
            if getattr(_cfg, 'KG_V2_ENABLED', False) and getattr(self, '_kg_v2', None):
                try:
                    await self._kg_v2.add_facts_from_episode(summary, time.time())
                    return
                except Exception as e:
                    logger.warning("kg.v2_extract_failed_fallback_to_v1", error=str(e))
        except Exception:
            pass

        # v1 逻辑 (原有代码)
        entity_count = await self.get_entity_count()
        if entity_count > self.MAX_ENTITIES:
            await self.cleanup_stale()

        try:
            from memory.ontology_complexity import should_extract
            import config as _cfg
            _threshold = float(getattr(_cfg, "ONTOLOGY_SKIP_THRESHOLD", 0.75))
            _do_extract, _score = should_extract(summary, skip_threshold=_threshold)
            if not _do_extract:
                logger.debug("kg.skip_complex_summary",
                             total=round(_score.total, 3),
                             detail=_score.detail)
                return
        except Exception as e:
            logger.debug("kg.complexity_check_failed", error=str(e))

        extracted = await self.extract_from_summary(summary)
        if extracted.get("entities"):
            await self.merge_entities(extracted["entities"])
        if extracted.get("relations"):
            await self.merge_relations(extracted["relations"])
```

Add `import time` at the top of `knowledge_graph.py` if not already present (it is not in the current imports — add it after line 2).

- [ ] **Step 6: Add KG v2 search to `MemoryManager`**

In `memory/memory_manager.py`, add a `_kg_v2_search` method and integrate it into the search path. In the `__init__` method (around line 224), add:

```python
        self._kg_v2_engine: Any = None
```

Add a setter method after `set_knowledge_graph` (around line 258):

```python
    def set_kg_v2_engine(self, engine: Any) -> None:
        """注入 KGSearchEngine 实例 (KG v2 混合检索)。"""
        self._kg_v2_engine = engine
```

In the search method, add a v2 KG recall coroutine alongside the existing `_kg_recall`. Find the `_kg_recall` definition (around line 408) and add after it:

```python
        # KG v2 混合检索协程
        async def _kg_v2_recall() -> list[dict]:
            """KG v2: 直接返回 KG 事实/实体作为上下文候选。"""
            import config as _cfg
            if not getattr(_cfg, 'KG_V2_ENABLED', False) or not self._kg_v2_engine:
                return []
            try:
                results = await self._kg_v2_engine.search(query, top_k=recall_limit)
                if not results:
                    return []
                # 将 KG 事实格式化为 dict 供上下文使用
                formatted = []
                for r in results:
                    if r.get("type") == "relation":
                        formatted.append({
                            "summary": r.get("fact", ""),
                            "source": "kg_v2",
                            "rrf_score": r.get("rrf_score", 0),
                        })
                    elif r.get("type") == "entity":
                        summary_text = f"{r.get('name', '')}({r.get('kind', '')}): {r.get('summary', '')}"
                        formatted.append({
                            "summary": summary_text,
                            "source": "kg_v2",
                            "rrf_score": r.get("rrf_score", 0),
                        })
                return formatted
            except Exception as e:
                logger.debug("memory.kg_v2_recall_failed", error=str(e))
                return []
```

Then add `_kg_v2_recall()` to the `asyncio.gather` call that runs the parallel recall (find the existing gather call and add it):

```python
        # 将 v2 recall 结果加入 all_items
        kg_v2_items = await _kg_v2_recall()
        if kg_v2_items:
            all_items.extend(kg_v2_items)
```

Add this after the existing `all_items` aggregation (before the dedup/sorting step).

- [ ] **Step 7: Run integration tests to verify they pass**

Run: `python -m pytest tests/test_kg_v2_integration.py -v`
Expected: All 6 tests PASS.

- [ ] **Step 8: Run full KG v2 test suite**

Run: `python -m pytest tests/test_kg_v2_schema.py tests/test_kg_v2_crud.py tests/test_kg_v2_invalidation.py tests/test_kg_v2_search.py tests/test_kg_v2_community.py tests/test_kg_v2_integration.py -v`
Expected: All tests PASS.

- [ ] **Step 9: Run regression on existing KG tests**

Run: `python -m pytest tests/test_i6_kg_rag.py tests/test_bitemporal_memory.py -v`
Expected: All existing tests still PASS.

- [ ] **Step 10: Commit**

```bash
git add config.py db/database.py memory/knowledge_graph.py memory/memory_manager.py tests/test_kg_v2_integration.py
git commit -m "feat(kg-v2): integrate KG v2 with feature flag, memory manager, and end-to-end flow"
```

---

## Self-Review

### 1. Spec Coverage

| Spec Section | Task | Status |
|---|---|---|
| §4 Schema Extension (kg_episodes, kg_entities_v2, kg_relations_v2, kg_communities, kg_edge_episode_refs, FTS5) | Task 1 | ✅ |
| §5 Fact Supersession (merge_relation_v2, _detect_contradictions, _resolve_contradiction) | Task 4 | ✅ |
| §6 Entity Evolution (merge_entities_v2, _rewrite_summary, summary_version) | Task 4 | ✅ |
| §7 Hybrid Retrieval (semantic + fulltext + graph + RRF) | Task 5 | ✅ |
| §8 Community Detection (label propagation, community summary, incremental update) | Task 6 | ✅ |
| §9 Episode Provenance (kg_edge_episode_refs, get_facts_from_episode, get_episodes_for_fact) | Task 2 | ✅ |
| §10 Integration & Feature Flag (KG_V2_ENABLED, auto_extract_and_merge v2 branch) | Task 7 | ✅ |
| §11 Data Migration (v1→v2, idempotent, old tables preserved) | Task 1 | ✅ |
| §4.6 Vector Tables (kg_entities_vec, kg_relations_vec) | Task 3 | ✅ |
| Vector CRUD (upsert_kg_entity, upsert_kg_relation, search_kg_entities, search_kg_relations) | Task 3 | ✅ |

### 2. Placeholder Scan

No placeholders found. All code blocks contain complete implementations. No "TODO", "TBD", "implement later", or "similar to Task N" references.

### 3. Type Consistency

| Method | Defined In | Used In | Consistent |
|---|---|---|---|
| `KnowledgeDBV2.insert_entity_v2(...) -> int` | Task 2 | Task 4 (`merge_entities_v2`) | ✅ |
| `KnowledgeDBV2.insert_relation_v2(...) -> int` | Task 2 | Task 4 (`merge_relation_v2`) | ✅ |
| `KnowledgeDBV2.update_entity_summary_v2(...) -> int` | Task 2 | Task 4 (`merge_entities_v2`) | ✅ |
| `KnowledgeDBV2.get_active_relations_between(...) -> list[dict]` | Task 2 | Task 4 (`merge_relation_v2`) | ✅ |
| `KnowledgeDBV2.invalidate_relation(...)` | Task 2 | Task 4 (`merge_relation_v2`) | ✅ |
| `KnowledgeDBV2.append_episode_ref(...)` | Task 2 | Task 4 (`merge_relation_v2`) | ✅ |
| `KnowledgeDBV2.insert_community(...)` | Task 2 | Task 6 (`_build_community_summary`) | ✅ |
| `KnowledgeDBV2.get_entity_community(...)` | Task 2 | Task 6 (`update_community_for_entity`) | ✅ |
| `KnowledgeDBV2.add_entity_to_community(...)` | Task 2 | Task 6 (`update_community_for_entity`) | ✅ |
| `VectorStore.upsert_kg_entity(row_id: int, text: str) -> bool` | Task 3 | Task 4 (`merge_entities_v2`) | ✅ |
| `VectorStore.upsert_kg_relation(row_id: int, text: str) -> bool` | Task 3 | Task 4 (`merge_relation_v2`) | ✅ |
| `VectorStore.search_kg_entities(...) -> list[tuple[int, float]]` | Task 3 | Task 5 (`_semantic_search`) | ✅ |
| `VectorStore.search_kg_relations(...) -> list[tuple[int, float]]` | Task 3 | Task 5 (`_semantic_search`) | ✅ |
| `KnowledgeGraphV2.add_facts_from_episode(...) -> dict` | Task 4 | Task 7 (`auto_extract_and_merge`) | ✅ |
| `KGSearchEngine.search(...) -> list[dict]` | Task 5 | Task 7 (`_kg_v2_recall`) | ✅ |
| `KGSearchEngine._rrf_fuse(...) -> list[dict]` | Task 5 | Task 5 (`search`) | ✅ |
| `KnowledgeGraph.set_kg_v2(kg_v2)` | Task 7 | Task 7 (integration) | ✅ |
| `DatabaseManager.kg_v2` | Task 7 | Task 7 (tests) | ✅ |
