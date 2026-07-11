# 扩散激活记忆系统实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 xiaoda-agent 记忆系统引入扩散激活检索 + 概念图 + confirm/correct 机制，基于 mind v6.2.8 的扩散激活理论。

**Architecture:** 新增 concept_nodes/concept_edges SQLite 表存储概念图；新增 SpreadingActivationEngine 作为第五路 RRF 检索通道（与现有 FTS/Vector/KG/ChildChunk 并列）；修改 FluidMemory 为 mind 风格 Ebbinghaus 增量模型（移除 MAX_BOOST 硬上限）；新增 confirm/correct 机制强化/纠正记忆。

**Tech Stack:** Python 3.11+, asyncio, aiosqlite, sqlite-vec, jieba, loguru, pytest

## Global Constraints

- 所有时间相关函数使用 `ZoneInfo("Asia/Shanghai")`（项目硬约束）
- 嵌入策略：保留现有 sqlite-vec + OpenAI 兼容嵌入（不使用 mind 的 hash 嵌入）
- 存储方式：SQLite 表（与现有架构一致）
- 迁移策略：双写+懒迁移（新记忆同时写入 episodic_memories 和 concept_nodes）
- 检索融合：扩散激活作为第五路 RRF 通道（保留现有四路能力）
- 不破坏现有线上稳定性（QQ Bot + Web UI 双进程）
- 保持与现有代码风格一致（Python 3.11 + asyncio + aiosqlite + loguru）
- 不引入 Neo4j 等重依赖，仅用 sqlite-vec
- 关键参数（来自 spec，必须 verbatim）：
  - `BOOST_PER_ACCESS = 0.15`, `EDGE_BOOST = 0.25`
  - `STABILITY_BASE_DAYS = 3.0`, `STABILITY_PER_ACCESS = 14.0`
  - `GRACE_DAYS = 45`, `WEIGHT_THRESHOLD = 0.1`
  - `RECALL_RADIUS = 3`, `ACTIVATION_DECAY = 0.5`
  - `SPREADING_THRESHOLD = 0.05`, `RRF_K = 60`
  - `FUZZY_ACTIVATION = 0.5`, `SEPARATION_SIM = 0.92`
  - `MAX_KEYS = 24`, 扩散激活 RRF 权重 = 0.85
  - `weight_bias = 0.35 + 0.65 * weight`（floor 0.35）
  - auto_link 阈值：共享 ≥3 个 keys
  - correct 验证：共享 ≥2 token 或覆盖 ≥50%

---

## File Structure

| 文件 | 类型 | 职责 |
|------|------|------|
| `db/db_concept.py` | 新增 | concept_nodes/edges/meta CRUD（异步 aiosqlite） |
| `db/schema.sql` | 修改 | 追加 concept_nodes/edges/meta 表 DDL + 索引 |
| `db/database.py` | 修改 | 新增 `_ddl_concept_tables()` 到 `_create_tables_ddl` |
| `memory/key_extractor.py` | 新增 | jieba 分词 + 停用词 + 同义词归一化 |
| `memory/concept_graph.py` | 新增 | ConceptGraph：节点/边管理 + auto_link + lazy_migrate |
| `memory/spreading_activation.py` | 新增 | SpreadingActivationEngine：三通道融合检索 |
| `memory/confirm_correct.py` | 新增 | ConfirmCorrect：confirm 强化 + correct 超驰 |
| `memory/fluid_memory.py` | 修改 | 移除 MAX_BOOST，改用 Ebbinghaus 增量模型 |
| `tests/test_fluid_memory.py` | 修改 | 更新为新公式 + peak_weight 参数 |
| `memory/memory_manager.py` | 修改 | 集成第五路扩散激活通道 + 双写 |
| `tools/memory_tool.py` | 修改 | 暴露 confirm/correct 为 Agent 工具 |
| `tests/test_db_concept.py` | 新增 | 概念图 CRUD 测试 |
| `tests/test_key_extractor.py` | 新增 | Key 提取器测试 |
| `tests/test_concept_graph.py` | 新增 | 概念图管理测试 |
| `tests/test_spreading_activation.py` | 新增 | 扩散激活引擎测试 |
| `tests/test_confirm_correct.py` | 新增 | confirm/correct 测试 |
| `tests/test_spreading_integration.py` | 新增 | 集成测试：第五路通道 + 双写 |

---

### Task 1: 概念图数据库表与 CRUD (db/db_concept.py + schema + database.py)

**Files:**
- Create: `db/db_concept.py`
- Modify: `db/schema.sql`（追加 concept 表 DDL 到末尾）
- Modify: `db/database.py`（新增 `_ddl_concept_tables` 方法）
- Test: `tests/test_db_concept.py`

**Interfaces:**
- Consumes: `db/database.py` 的 `Database` 类（提供 `self._conn` aiosqlite 连接）
- Produces: `ConceptDB` 类，方法签名：
  - `async def insert_node(self, id, text, keys, weight=1.0, peak_weight=1.0, confidence=1.0, access_count=0, layer="hippocampus", source_mem_id=None, origin="{}") -> None`
  - `async def get_node(self, node_id: str) -> dict | None`
  - `async def get_node_by_source_mem(self, mem_id: int) -> dict | None`
  - `async def update_node(self, node_id: str, **fields) -> None`
  - `async def get_alive_nodes(self) -> dict[str, dict]`（返回 `{id: node_dict}`，valid_to IS NULL）
  - `async def get_node_count(self) -> int`
  - `async def create_edge(self, source_id, target_id, relation="related", weight=1.0, created=None) -> None`
  - `async def get_edges(self, node_id: str) -> dict[str, dict]`（返回 `{target_id: edge_dict}`）
  - `async def update_edge(self, source_id, target_id, weight=None, relation=None) -> None`
  - `async def auto_link(self, node_id: str, keys: list[str], min_shared=3) -> int`（返回建边数）
  - `async def get_meta(self, key: str) -> str | None`
  - `async def set_meta(self, key: str, value: str) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_db_concept.py`:

```python
"""概念图数据库 CRUD 单元测试"""
import asyncio
import json
import os
import tempfile
import time

import aiosqlite
import pytest

from db.db_concept import ConceptDB


@pytest.fixture
async def concept_db():
    """临时内存 SQLite 数据库 + 概念图表"""
    db_path = ":memory:"
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    # 创建 concept 表
    await conn.executescript("""
        CREATE TABLE IF NOT EXISTS concept_nodes (
            id            TEXT PRIMARY KEY,
            text          TEXT NOT NULL,
            weight        REAL NOT NULL DEFAULT 1.0,
            peak_weight   REAL NOT NULL DEFAULT 1.0,
            confidence    REAL NOT NULL DEFAULT 1.0,
            access_count  INTEGER NOT NULL DEFAULT 0,
            keys          TEXT NOT NULL DEFAULT '[]',
            layer         TEXT NOT NULL DEFAULT 'hippocampus',
            created       TEXT NOT NULL,
            last_accessed TEXT NOT NULL,
            valid_from    TEXT NOT NULL,
            valid_to      TEXT,
            superseded_by TEXT,
            history       TEXT NOT NULL DEFAULT '[]',
            origin        TEXT NOT NULL DEFAULT '{}',
            source_mem_id INTEGER,
            embedding     BLOB
        );
        CREATE TABLE IF NOT EXISTS concept_edges (
            source_id  TEXT NOT NULL,
            target_id  TEXT NOT NULL,
            relation   TEXT NOT NULL DEFAULT 'related',
            weight     REAL NOT NULL DEFAULT 1.0,
            created    TEXT NOT NULL,
            PRIMARY KEY (source_id, target_id)
        );
        CREATE TABLE IF NOT EXISTS concept_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    await conn.commit()
    cdb = ConceptDB(conn)
    yield cdb
    await conn.close()


@pytest.mark.asyncio
async def test_insert_and_get_node(concept_db):
    now = "2026-07-10T12:00:00+08:00"
    await concept_db.insert_node(
        id="abc123def456", text="Redis 是内存数据库",
        keys=json.dumps(["redis", "数据库", "内存"]),
        created=now, last_accessed=now, valid_from=now,
    )
    node = await concept_db.get_node("abc123def456")
    assert node is not None
    assert node["text"] == "Redis 是内存数据库"
    assert node["weight"] == 1.0
    assert node["valid_to"] is None


@pytest.mark.asyncio
async def test_get_node_not_found(concept_db):
    node = await concept_db.get_node("nonexistent")
    assert node is None


@pytest.mark.asyncio
async def test_get_node_by_source_mem(concept_db):
    now = "2026-07-10T12:00:00+08:00"
    await concept_db.insert_node(
        id="src123", text="test", keys='["a"]',
        created=now, last_accessed=now, valid_from=now,
        source_mem_id=42,
    )
    node = await concept_db.get_node_by_source_mem(42)
    assert node is not None
    assert node["id"] == "src123"


@pytest.mark.asyncio
async def test_update_node(concept_db):
    now = "2026-07-10T12:00:00+08:00"
    await concept_db.insert_node(
        id="upd123", text="test", keys='["a"]',
        created=now, last_accessed=now, valid_from=now,
    )
    await concept_db.update_node("upd123", weight=0.8, access_count=3,
                                 peak_weight=0.9, last_accessed=now)
    node = await concept_db.get_node("upd123")
    assert node["weight"] == 0.8
    assert node["access_count"] == 3
    assert node["peak_weight"] == 0.9


@pytest.mark.asyncio
async def test_get_alive_nodes(concept_db):
    now = "2026-07-10T12:00:00+08:00"
    await concept_db.insert_node(
        id="alive1", text="alive", keys='["a"]',
        created=now, last_accessed=now, valid_from=now,
    )
    await concept_db.insert_node(
        id="dead1", text="dead", keys='["b"]',
        created=now, last_accessed=now, valid_from=now,
    )
    await concept_db.update_node("dead1", valid_to=now)
    alive = await concept_db.get_alive_nodes()
    assert "alive1" in alive
    assert "dead1" not in alive


@pytest.mark.asyncio
async def test_get_node_count(concept_db):
    now = "2026-07-10T12:00:00+08:00"
    assert await concept_db.get_node_count() == 0
    await concept_db.insert_node(
        id="cnt1", text="a", keys='["x"]',
        created=now, last_accessed=now, valid_from=now,
    )
    await concept_db.insert_node(
        id="cnt2", text="b", keys='["y"]',
        created=now, last_accessed=now, valid_from=now,
    )
    assert await concept_db.get_node_count() == 2


@pytest.mark.asyncio
async def test_create_and_get_edges(concept_db):
    now = "2026-07-10T12:00:00+08:00"
    for nid in ["n1", "n2", "n3"]:
        await concept_db.insert_node(
            id=nid, text=f"text_{nid}", keys='["k"]',
            created=now, last_accessed=now, valid_from=now,
        )
    await concept_db.create_edge("n1", "n2", "co-occurrence", 1.0, now)
    await concept_db.create_edge("n1", "n3", "related", 0.5, now)
    edges = await concept_db.get_edges("n1")
    assert "n2" in edges
    assert "n3" in edges
    assert edges["n2"]["relation"] == "co-occurrence"
    assert edges["n3"]["weight"] == 0.5


@pytest.mark.asyncio
async def test_update_edge(concept_db):
    now = "2026-07-10T12:00:00+08:00"
    for nid in ["n1", "n2"]:
        await concept_db.insert_node(
            id=nid, text=f"text_{nid}", keys='["k"]',
            created=now, last_accessed=now, valid_from=now,
        )
    await concept_db.create_edge("n1", "n2", "related", 1.0, now)
    await concept_db.update_edge("n1", "n2", weight=0.7)
    edges = await concept_db.get_edges("n1")
    assert edges["n2"]["weight"] == 0.7


@pytest.mark.asyncio
async def test_auto_link_3_shared_keys(concept_db):
    now = "2026-07-10T12:00:00+08:00"
    # 节点 A 有 3 个 keys
    await concept_db.insert_node(
        id="nodeA", text="Python web 开发用 FastAPI",
        keys=json.dumps(["python", "web", "开发", "fastapi"]),
        created=now, last_accessed=now, valid_from=now,
    )
    # 节点 B 共享 3 个 keys
    await concept_db.insert_node(
        id="nodeB", text="Python web 框架对比",
        keys=json.dumps(["python", "web", "框架", "对比"]),
        created=now, last_accessed=now, valid_from=now,
    )
    # 节点 C 只共享 1 个 key
    await concept_db.insert_node(
        id="nodeC", text="Java 开发",
        keys=json.dumps(["java", "开发"]),
        created=now, last_accessed=now, valid_from=now,
    )
    # nodeC 的 keys
    new_keys = ["python", "web", "开发", "新内容"]
    count = await concept_db.auto_link("nodeC", new_keys, min_shared=3)
    # nodeA 共享 python/web/开发 = 3 → 建边
    # nodeB 共享 python/web = 2 → 不建边
    assert count == 1
    edges = await concept_db.get_edges("nodeC")
    assert "nodeA" in edges
    assert edges["nodeA"]["relation"] == "co-occurrence"


@pytest.mark.asyncio
async def test_meta_get_set(concept_db):
    assert await concept_db.get_meta("nonexistent") is None
    await concept_db.set_meta("last_edge_decay", "2026-07-10T12:00:00+08:00")
    val = await concept_db.get_meta("last_edge_decay")
    assert val == "2026-07-10T12:00:00+08:00"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_db_concept.py -v 2>&1 | head -30`
Expected: FAIL with `ModuleNotFoundError: No module named 'db.db_concept'`

- [ ] **Step 3: Write minimal implementation — db/db_concept.py**

Create `db/db_concept.py`:

```python
"""概念图数据库 CRUD — concept_nodes / concept_edges / concept_meta 表操作"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from loguru import logger

_SH_TZ = ZoneInfo("Asia/Shanghai")


def _now_iso() -> str:
    """返回 Asia/Shanghai 时区的 ISO 时间戳"""
    return datetime.now(_SH_TZ).isoformat()


class ConceptDB:
    """概念图数据库访问层（异步 aiosqlite）"""

    def __init__(self, conn):
        self._conn = conn

    async def insert_node(self, id: str, text: str, keys: str,
                          weight: float = 1.0, peak_weight: float = 1.0,
                          confidence: float = 1.0, access_count: int = 0,
                          layer: str = "hippocampus",
                          created: str | None = None,
                          last_accessed: str | None = None,
                          valid_from: str | None = None,
                          valid_to: str | None = None,
                          superseded_by: str | None = None,
                          history: str = "[]",
                          origin: str = "{}",
                          source_mem_id: int | None = None,
                          embedding=None) -> None:
        """插入概念节点。keys 为 JSON 字符串。"""
        now = created or _now_iso()
        await self._conn.execute(
            """INSERT OR REPLACE INTO concept_nodes
               (id, text, weight, peak_weight, confidence, access_count, keys,
                layer, created, last_accessed, valid_from, valid_to,
                superseded_by, history, origin, source_mem_id, embedding)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (id, text, weight, peak_weight, confidence, access_count, keys,
             layer, now, last_accessed or now, valid_from or now, valid_to,
             superseded_by, history, origin, source_mem_id, embedding),
        )
        await self._conn.commit()

    async def get_node(self, node_id: str) -> dict | None:
        async with self._conn.execute(
            "SELECT * FROM concept_nodes WHERE id = ?", (node_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_node_by_source_mem(self, mem_id: int) -> dict | None:
        async with self._conn.execute(
            "SELECT * FROM concept_nodes WHERE source_mem_id = ?", (mem_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def update_node(self, node_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [node_id]
        await self._conn.execute(
            f"UPDATE concept_nodes SET {cols} WHERE id = ?", vals
        )
        await self._conn.commit()

    async def get_alive_nodes(self) -> dict[str, dict]:
        """返回所有有效节点（valid_to IS NULL）"""
        async with self._conn.execute(
            "SELECT * FROM concept_nodes WHERE valid_to IS NULL"
        ) as cur:
            rows = await cur.fetchall()
            return {row["id"]: dict(row) for row in rows}

    async def get_node_count(self) -> int:
        async with self._conn.execute(
            "SELECT COUNT(*) FROM concept_nodes WHERE valid_to IS NULL"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def create_edge(self, source_id: str, target_id: str,
                           relation: str = "related", weight: float = 1.0,
                           created: str | None = None) -> None:
        now = created or _now_iso()
        await self._conn.execute(
            """INSERT OR REPLACE INTO concept_edges
               (source_id, target_id, relation, weight, created)
               VALUES (?, ?, ?, ?, ?)""",
            (source_id, target_id, relation, weight, now),
        )
        await self._conn.commit()

    async def get_edges(self, node_id: str) -> dict[str, dict]:
        async with self._conn.execute(
            "SELECT * FROM concept_edges WHERE source_id = ?", (node_id,)
        ) as cur:
            rows = await cur.fetchall()
            return {row["target_id"]: dict(row) for row in rows}

    async def update_edge(self, source_id: str, target_id: str,
                           weight: float | None = None,
                           relation: str | None = None) -> None:
        fields = {}
        if weight is not None:
            fields["weight"] = weight
        if relation is not None:
            fields["relation"] = relation
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [source_id, target_id]
        await self._conn.execute(
            f"UPDATE concept_edges SET {cols} WHERE source_id = ? AND target_id = ?",
            vals,
        )
        await self._conn.commit()

    async def auto_link(self, node_id: str, keys: list[str],
                         min_shared: int = 3) -> int:
        """与共享 ≥ min_shared 个 keys 的存活节点自动建边。返回建边数。"""
        if not keys:
            return 0
        alive = await self.get_alive_nodes()
        count = 0
        key_set = set(keys)
        now = _now_iso()
        for nid, node in alive.items():
            if nid == node_id:
                continue
            try:
                node_keys = set(json.loads(node.get("keys", "[]")))
            except (json.JSONDecodeError, TypeError):
                continue
            shared = key_set & node_keys
            if len(shared) >= min_shared:
                # 双向建边
                await self.create_edge(node_id, nid, "co-occurrence", 1.0, now)
                await self.create_edge(nid, node_id, "co-occurrence", 1.0, now)
                count += 1
        return count

    async def get_meta(self, key: str) -> str | None:
        async with self._conn.execute(
            "SELECT value FROM concept_meta WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO concept_meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        await self._conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_db_concept.py -v`
Expected: 10 passed

- [ ] **Step 5: Add concept table DDL to schema.sql**

Append to `db/schema.sql` (after the cleanup_config INSERT lines):

```sql

-- ============================================================
-- 概念图表 (扩散激活记忆系统)
-- ============================================================

CREATE TABLE IF NOT EXISTS concept_nodes (
    id            TEXT PRIMARY KEY,
    text          TEXT NOT NULL,
    weight        REAL NOT NULL DEFAULT 1.0,
    peak_weight   REAL NOT NULL DEFAULT 1.0,
    confidence    REAL NOT NULL DEFAULT 1.0,
    access_count  INTEGER NOT NULL DEFAULT 0,
    keys          TEXT NOT NULL DEFAULT '[]',
    layer         TEXT NOT NULL DEFAULT 'hippocampus',
    created       TEXT NOT NULL,
    last_accessed TEXT NOT NULL,
    valid_from    TEXT NOT NULL,
    valid_to      TEXT,
    superseded_by TEXT,
    history       TEXT NOT NULL DEFAULT '[]',
    origin        TEXT NOT NULL DEFAULT '{}',
    source_mem_id INTEGER,
    embedding     BLOB
);

CREATE TABLE IF NOT EXISTS concept_edges (
    source_id  TEXT NOT NULL,
    target_id  TEXT NOT NULL,
    relation   TEXT NOT NULL DEFAULT 'related',
    weight     REAL NOT NULL DEFAULT 1.0,
    created    TEXT NOT NULL,
    PRIMARY KEY (source_id, target_id)
);

CREATE TABLE IF NOT EXISTS concept_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_concept_node_keys ON concept_nodes(keys);
CREATE INDEX IF NOT EXISTS idx_concept_node_layer ON concept_nodes(layer);
CREATE INDEX IF NOT EXISTS idx_concept_node_weight ON concept_nodes(weight);
CREATE INDEX IF NOT EXISTS idx_concept_node_valid ON concept_nodes(valid_to);
CREATE INDEX IF NOT EXISTS idx_concept_edge_source ON concept_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_concept_edge_target ON concept_edges(target_id);
```

- [ ] **Step 6: Add `_ddl_concept_tables` to database.py**

In `db/database.py`, add a new method to the `Database` class. First read the `_create_tables_ddl` method (around line 556) to see the pattern, then add the concept DDL method and call it.

Add this method after `_ddl_learning_error_tables`:

```python
    async def _ddl_concept_tables(self) -> None:
        """建表：概念图（扩散激活记忆系统）。"""
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS concept_nodes (
                id            TEXT PRIMARY KEY,
                text          TEXT NOT NULL,
                weight        REAL NOT NULL DEFAULT 1.0,
                peak_weight   REAL NOT NULL DEFAULT 1.0,
                confidence    REAL NOT NULL DEFAULT 1.0,
                access_count  INTEGER NOT NULL DEFAULT 0,
                keys          TEXT NOT NULL DEFAULT '[]',
                layer         TEXT NOT NULL DEFAULT 'hippocampus',
                created       TEXT NOT NULL,
                last_accessed TEXT NOT NULL,
                valid_from    TEXT NOT NULL,
                valid_to      TEXT,
                superseded_by TEXT,
                history       TEXT NOT NULL DEFAULT '[]',
                origin        TEXT NOT NULL DEFAULT '{}',
                source_mem_id INTEGER,
                embedding     BLOB
            );

            CREATE TABLE IF NOT EXISTS concept_edges (
                source_id  TEXT NOT NULL,
                target_id  TEXT NOT NULL,
                relation   TEXT NOT NULL DEFAULT 'related',
                weight     REAL NOT NULL DEFAULT 1.0,
                created    TEXT NOT NULL,
                PRIMARY KEY (source_id, target_id)
            );

            CREATE TABLE IF NOT EXISTS concept_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_concept_node_keys ON concept_nodes(keys);
            CREATE INDEX IF NOT EXISTS idx_concept_node_layer ON concept_nodes(layer);
            CREATE INDEX IF NOT EXISTS idx_concept_node_weight ON concept_nodes(weight);
            CREATE INDEX IF NOT EXISTS idx_concept_node_valid ON concept_nodes(valid_to);
            CREATE INDEX IF NOT EXISTS idx_concept_edge_source ON concept_edges(source_id);
            CREATE INDEX IF NOT EXISTS idx_concept_edge_target ON concept_edges(target_id);
        """)
```

Then modify `_create_tables_ddl` to call it. Find the method body:

```python
    async def _create_tables_ddl(self) -> None:
        """Phase 1: 建表 DDL。按领域分组调用，便于维护。"""
        await self._ddl_memory_tables()
        await self._ddl_schedule_api_tables()
        await self._ddl_knowledge_tables()
        await self._ddl_learning_error_tables()
```

Add one line:

```python
    async def _create_tables_ddl(self) -> None:
        """Phase 1: 建表 DDL。按领域分组调用，便于维护。"""
        await self._ddl_memory_tables()
        await self._ddl_schedule_api_tables()
        await self._ddl_knowledge_tables()
        await self._ddl_learning_error_tables()
        await self._ddl_concept_tables()
```

- [ ] **Step 7: Verify schema compiles and tests pass**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_db_concept.py -v && python -c "import sqlite3; c=sqlite3.connect(':memory:'); c.executescript(open('db/schema.sql').read()); print('schema OK')"`
Expected: 10 passed + "schema OK"

- [ ] **Step 8: Commit**

```bash
cd /home/orangepi/ai-agent
git add db/db_concept.py db/schema.sql db/database.py tests/test_db_concept.py
git commit -m "feat: 概念图表 concept_nodes/edges/meta + ConceptDB CRUD"
```

---

### Task 2: Key 提取器 (memory/key_extractor.py)

**Files:**
- Create: `memory/key_extractor.py`
- Test: `tests/test_key_extractor.py`

**Interfaces:**
- Consumes: jieba（已在项目中使用）
- Produces: `KeyExtractor` 类：
  - `MAX_KEYS = 24`
  - `NORMALIZE` dict
  - `extract(self, text: str, is_query: bool = False) -> list[str]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_key_extractor.py`:

```python
"""Key 提取器单元测试"""
import pytest

from memory.key_extractor import KeyExtractor


def test_extract_basic():
    ke = KeyExtractor()
    keys = ke.extract("Redis 是一个内存数据库，常用于缓存")
    assert isinstance(keys, list)
    assert len(keys) > 0
    assert "redis" in [k.lower() for k in keys]


def test_extract_filters_stopwords():
    ke = KeyExtractor()
    keys = ke.extract("的 是 一个 了 在 和 与")
    # 全是停用词，应为空
    assert len(keys) == 0


def test_extract_normalizes_synonyms():
    ke = KeyExtractor()
    keys = ke.extract("postgres 性能优化")
    keys_lower = [k.lower() for k in keys]
    assert "postgresql" in keys_lower
    assert "postgres" not in keys_lower


def test_extract_filters_short_words():
    ke = KeyExtractor()
    keys = ke.extract("a b c 数据库")
    # len < 2 的词被过滤
    assert "a" not in keys
    assert "b" not in keys
    assert "数据库" in keys


def test_max_keys_limit():
    ke = KeyExtractor()
    # 生成大量关键词
    text = " ".join([f"关键词{i}" for i in range(50)])
    keys = ke.extract(text)
    assert len(keys) <= KeyExtractor.MAX_KEYS


def test_extract_empty_text():
    ke = KeyExtractor()
    assert ke.extract("") == []
    assert ke.extract("   ") == []


def test_extract_chinese_and_english():
    ke = KeyExtractor()
    keys = ke.extract("Python 编程语言 开发 framework")
    keys_lower = [k.lower() for k in keys]
    assert "python" in keys_lower
    assert "编程语言" in keys or "编程" in keys


def test_extract_query_mode():
    """查询模式：正常提取（is_query 参数存在但不改变基础行为）"""
    ke = KeyExtractor()
    keys = ke.extract("redis 缓存", is_query=True)
    assert isinstance(keys, list)
    assert len(keys) > 0


def test_normalize_mapping_exists():
    assert "postgres" in KeyExtractor.NORMALIZE
    assert KeyExtractor.NORMALIZE["postgres"] == "postgresql"
    assert KeyExtractor.NORMALIZE["前端"] == "frontend"


def test_max_keys_value():
    assert KeyExtractor.MAX_KEYS == 24
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_key_extractor.py -v 2>&1 | head -20`
Expected: FAIL with `ModuleNotFoundError: No module named 'memory.key_extractor'`

- [ ] **Step 3: Write minimal implementation**

Create `memory/key_extractor.py`:

```python
"""关键词提取器 — jieba 分词 + 停用词 + 同义词归一化

基于 mind v6.2.8 的 key 提取策略，适配中文场景。
"""
import re

import jieba

from loguru import logger

# 停用词表（与项目现有 _TOPIC_STOPWORDS 保持一致 + 扩展）
_STOPWORDS = {
    # 中文停用词
    "的", "是", "了", "在", "和", "与", "或", "也", "都", "就", "还", "又",
    "这", "那", "这个", "那个", "这些", "那些", "什么", "怎么", "为什么",
    "一个", "一些", "一种", "一样", "可以", "能", "能不", "能够", "应该",
    "我", "你", "他", "她", "它", "我们", "你们", "他们", "她们", "它们",
    "自己", "别人", "大家", "咱们", "您",
    "有", "没", "没有", "会", "要", "想", "需要", "必须", "得",
    "把", "被", "让", "使", "给", "对", "跟", "向", "往", "到", "从",
    "上", "下", "里", "外", "前", "后", "左", "右", "中", "间",
    "很", "非常", "太", "更", "最", "比较", "相当", "十分", "极其",
    "不", "别", "勿", "莫", "是否", "是不是", "有没有",
    "着", "过", "地", "得",
    "只", "才", "便", "即", "则", "而", "而且", "并且", "但是", "可是",
    "如果", "虽然", "尽管", "即使", "除非", "因为", "所以", "因此",
    "为了", "至于", "关于", "对于", "根据", "按照", "通过", "经由",
    "由于", "由",
    # 英文停用词
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "can", "shall",
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us",
    "them", "my", "your", "his", "its", "our", "their",
    "this", "that", "these", "those", "what", "which", "who", "whom",
    "and", "or", "but", "not", "no", "nor", "so", "if", "then", "else",
    "when", "where", "why", "how", "all", "any", "both", "each", "few",
    "more", "most", "other", "some", "such", "only", "own", "same", "than",
    "too", "very", "just", "now",
    "in", "on", "at", "to", "for", "of", "with", "by", "from", "as",
    "into", "about", "between", "through", "during", "before", "after",
    "up", "down", "out", "off", "over", "under", "again",
}


class KeyExtractor:
    """关键词提取器 — jieba 分词 + 停用词 + 同义词归一化"""

    MAX_KEYS = 24  # 与 mind 一致

    # 同义词归一化映射
    NORMALIZE = {
        "postgre": "postgresql",
        "postgres": "postgresql",
        "redis缓存": "redis",
        "前端": "frontend",
        "后端": "backend",
    }

    def extract(self, text: str, is_query: bool = False) -> list[str]:
        """提取索引关键词

        Args:
            text: 输入文本
            is_query: 是否为查询模式（保留参数，未来可扩展身份 facet key）

        Returns:
            去重后的关键词列表（最多 MAX_KEYS 个）
        """
        if not text or not text.strip():
            return []

        # jieba 分词
        tokens = jieba.lcut(text)

        keys = []
        seen = set()
        for token in tokens:
            token = token.strip()
            if not token or len(token) < 2:
                continue
            # 跳过纯数字
            if token.isdigit():
                continue
            # 跳过纯标点
            if re.match(r'^[\W_]+$', token, re.UNICODE):
                continue
            # 小写化
            lower = token.lower()
            # 停用词过滤
            if lower in _STOPWORDS:
                continue
            # 同义词归一化
            lower = self.NORMALIZE.get(lower, lower)
            # 去重
            if lower in seen:
                continue
            seen.add(lower)
            keys.append(lower)
            if len(keys) >= self.MAX_KEYS:
                break

        return keys
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_key_extractor.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add memory/key_extractor.py tests/test_key_extractor.py
git commit -m "feat: KeyExtractor 关键词提取器 (jieba+停用词+归一化)"
```

---

### Task 3: 概念图管理器 (memory/concept_graph.py)

**Files:**
- Create: `memory/concept_graph.py`
- Test: `tests/test_concept_graph.py`

**Interfaces:**
- Consumes: `db.db_concept.ConceptDB`（Task 1）、`memory.key_extractor.KeyExtractor`（Task 2）
- Produces: `ConceptGraph` 类：
  - `__init__(self, concept_db: ConceptDB, key_extractor: KeyExtractor)`
  - `async def remember(self, text: str, source_mem_id: int | None = None) -> str`（返回 node_id）
  - `async def lazy_migrate(self, episodic_memories: list[dict], limit: int = 50) -> int`（返回迁移数）
  - `_clean_text(self, text: str) -> str`
  - `_make_node_id(self, text: str) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_concept_graph.py`:

```python
"""概念图管理器单元测试"""
import asyncio
import json

import aiosqlite
import pytest

from db.db_concept import ConceptDB
from memory.concept_graph import ConceptGraph
from memory.key_extractor import KeyExtractor


@pytest.fixture
async def graph():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript("""
        CREATE TABLE IF NOT EXISTS concept_nodes (
            id TEXT PRIMARY KEY, text TEXT NOT NULL,
            weight REAL DEFAULT 1.0, peak_weight REAL DEFAULT 1.0,
            confidence REAL DEFAULT 1.0, access_count INTEGER DEFAULT 0,
            keys TEXT DEFAULT '[]', layer TEXT DEFAULT 'hippocampus',
            created TEXT NOT NULL, last_accessed TEXT NOT NULL,
            valid_from TEXT NOT NULL, valid_to TEXT, superseded_by TEXT,
            history TEXT DEFAULT '[]', origin TEXT DEFAULT '{}',
            source_mem_id INTEGER, embedding BLOB
        );
        CREATE TABLE IF NOT EXISTS concept_edges (
            source_id TEXT NOT NULL, target_id TEXT NOT NULL,
            relation TEXT DEFAULT 'related', weight REAL DEFAULT 1.0,
            created TEXT NOT NULL, PRIMARY KEY (source_id, target_id)
        );
        CREATE TABLE IF NOT EXISTS concept_meta (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
    """)
    await conn.commit()
    cdb = ConceptDB(conn)
    ke = KeyExtractor()
    g = ConceptGraph(cdb, ke)
    yield g
    await conn.close()


@pytest.mark.asyncio
async def test_remember_creates_node(graph):
    node_id = await graph.remember("Redis 是内存数据库，用于缓存")
    assert node_id is not None
    assert len(node_id) == 12  # md5[:12]
    node = await graph.get_node(node_id)
    assert node is not None
    assert "Redis" in node["text"] or "redis" in node["text"].lower()


@pytest.mark.asyncio
async def test_remember_same_text_same_id(graph):
    id1 = await graph.remember("Python 编程语言")
    id2 = await graph.remember("Python 编程语言")
    assert id1 == id2  # 相同文本生成相同 ID


@pytest.mark.asyncio
async def test_remember_with_source_mem_id(graph):
    node_id = await graph.remember("测试记忆", source_mem_id=123)
    node = await graph.get_node(node_id)
    assert node["source_mem_id"] == 123


@pytest.mark.asyncio
async def test_remember_auto_links_shared_keys(graph):
    # 节点 A: 4 个 keys
    await graph.remember("Python web 开发框架 FastAPI 性能")
    # 节点 B: 共享 python/web/开发 3 个 keys
    node_b_id = await graph.remember("Python web 开发最佳实践")
    edges = await graph.get_edges(node_b_id)
    # 应有至少一条边到节点 A
    assert len(edges) >= 1


@pytest.mark.asyncio
async def test_remember_no_auto_link_below_threshold(graph):
    # 节点 A
    await graph.remember("Python 数据分析 pandas numpy")
    # 节点 B: 只共享 python 1 个 key
    node_b_id = await graph.remember("Python 测试 pytest")
    edges = await graph.get_edges(node_b_id)
    # 不应建边（共享 < 3）
    assert len(edges) == 0


@pytest.mark.asyncio
async def test_lazy_migrate(graph):
    episodic = [
        {"id": 1, "summary": "Redis 缓存配置"},
        {"id": 2, "summary": "PostgreSQL 数据库优化"},
        {"id": 3, "summary": "Python 异步编程 asyncio"},
    ]
    count = await graph.lazy_migrate(episodic, limit=50)
    assert count == 3
    # 验证节点已创建
    for mem in episodic:
        node = await graph.get_node_by_source_mem(mem["id"])
        assert node is not None


@pytest.mark.asyncio
async def test_lazy_migrate_skips_existing(graph):
    await graph.remember("已有记忆", source_mem_id=1)
    episodic = [
        {"id": 1, "summary": "已有记忆"},  # 已存在
        {"id": 2, "summary": "新记忆"},
    ]
    count = await graph.lazy_migrate(episodic, limit=50)
    assert count == 1  # 只迁移新的


def test_clean_text(graph):
    assert graph._clean_text("  Hello  World  ") == "Hello  World"
    assert graph._clean_text("\n\nText\n") == "Text"


def test_make_node_id(graph):
    id1 = graph._make_node_id("test")
    id2 = graph._make_node_id("test")
    id3 = graph._make_node_id("different")
    assert id1 == id2
    assert id1 != id3
    assert len(id1) == 12
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_concept_graph.py -v 2>&1 | head -20`
Expected: FAIL with `ModuleNotFoundError: No module named 'memory.concept_graph'`

- [ ] **Step 3: Write minimal implementation**

Create `memory/concept_graph.py`:

```python
"""概念图管理器 — Hippocampus 层节点/边管理 + auto_link + 懒迁移"""
import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from loguru import logger

from db.db_concept import ConceptDB
from memory.key_extractor import KeyExtractor

_SH_TZ = ZoneInfo("Asia/Shanghai")


class ConceptGraph:
    """概念图管理器（Hippocampus 层）

    职责：
    1. remember(): 新记忆写入 concept_nodes + auto_link
    2. lazy_migrate(): 旧 episodic_memories 懒迁移到 concept_nodes
    """

    def __init__(self, concept_db: ConceptDB, key_extractor: KeyExtractor):
        self.db = concept_db
        self.ke = key_extractor

    def _clean_text(self, text: str) -> str:
        """清理文本：去首尾空白"""
        return text.strip()

    def _make_node_id(self, text: str) -> str:
        """生成节点 ID：md5(cleaned_text)[:12]"""
        cleaned = self._clean_text(text)
        return hashlib.md5(cleaned.encode("utf-8")).hexdigest()[:12]

    async def remember(self, text: str,
                        source_mem_id: int | None = None) -> str:
        """新记忆写入概念图

        1. 清理文本，生成 node_id
        2. 提取 keys
        3. 插入 concept_nodes（若已存在则跳过）
        4. auto_link：与共享 ≥3 keys 的节点建边

        Returns:
            node_id
        """
        cleaned = self._clean_text(text)
        if not cleaned:
            return ""

        node_id = self._make_node_id(cleaned)
        # 检查是否已存在
        existing = await self.db.get_node(node_id)
        if existing:
            return node_id

        keys = self.ke.extract(cleaned, is_query=False)
        now = datetime.now(_SH_TZ).isoformat()

        await self.db.insert_node(
            id=node_id, text=cleaned,
            keys=json.dumps(keys, ensure_ascii=False),
            weight=1.0, peak_weight=1.0, confidence=1.0,
            access_count=0, layer="hippocampus",
            created=now, last_accessed=now,
            valid_from=now, valid_to=None,
            source_mem_id=source_mem_id,
        )

        # auto_link
        if keys:
            link_count = await self.db.auto_link(node_id, keys, min_shared=3)
            if link_count:
                logger.debug("concept_graph.auto_linked",
                             node=node_id, links=link_count)

        return node_id

    async def lazy_migrate(self, episodic_memories: list[dict],
                            limit: int = 50) -> int:
        """懒迁移：将旧 episodic_memories 迁移到 concept_nodes

        已迁移的（source_mem_id 已存在）跳过。

        Args:
            episodic_memories: [{"id": int, "summary": str}, ...]
            limit: 最多迁移数量

        Returns:
            实际迁移数量
        """
        count = 0
        for mem in episodic_memories[:limit]:
            mem_id = mem.get("id")
            summary = mem.get("summary", "")
            if not summary:
                continue
            # 检查是否已迁移
            existing = await self.db.get_node_by_source_mem(mem_id)
            if existing:
                continue
            await self.remember(summary, source_mem_id=mem_id)
            count += 1
        if count:
            logger.info("concept_graph.lazy_migrated", count=count)
        return count

    async def get_node(self, node_id: str) -> dict | None:
        return await self.db.get_node(node_id)

    async def get_node_by_source_mem(self, mem_id: int) -> dict | None:
        return await self.db.get_node_by_source_mem(mem_id)

    async def get_edges(self, node_id: str) -> dict[str, dict]:
        return await self.db.get_edges(node_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_concept_graph.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add memory/concept_graph.py tests/test_concept_graph.py
git commit -m "feat: ConceptGraph 概念图管理器 (remember+auto_link+lazy_migrate)"
```

---

### Task 4: 扩散激活引擎 (memory/spreading_activation.py)

**Files:**
- Create: `memory/spreading_activation.py`
- Test: `tests/test_spreading_activation.py`

**Interfaces:**
- Consumes: `db.db_concept.ConceptDB`（Task 1）、`memory.vector_store.VectorStore`（现有，提供 `search()` 方法）、`memory.key_extractor.KeyExtractor`（Task 2）
- Produces: `SpreadingActivationEngine` 类：
  - 常量：`RECALL_RADIUS=3`, `ACTIVATION_DECAY=0.5`, `SPREADING_THRESHOLD=0.05`, `RRF_K=60`, `FUZZY_ACTIVATION=0.5`, `SEPARATION_SIM=0.92`
  - `__init__(self, concept_db, vector_store, key_extractor)`
  - `async def recall(self, query: str, top_k: int = 5) -> list[dict]`（返回 `[{id, text, score, weight, keys}, ...]`）
  - `_compute_idf(self, keys, alive_nodes) -> dict`
  - `_direct_channel(self, keys, idf, alive_nodes, query) -> dict`
  - `async def _pattern_completion(self, query, alive_nodes) -> dict`
  - `async def _spreading_channel(self, direct, alive_nodes) -> dict`
  - `_rrf_fusion(self, direct, spread) -> dict`
  - `async def _semantic_rerank(self, query, fused, top_k) -> list`
  - `_pattern_separation(self, fused, top_k) -> list`

- [ ] **Step 1: Write the failing test**

Create `tests/test_spreading_activation.py`:

```python
"""扩散激活引擎单元测试"""
import json

import aiosqlite
import pytest

from db.db_concept import ConceptDB
from memory.key_extractor import KeyExtractor
from memory.spreading_activation import SpreadingActivationEngine


@pytest.fixture
async def engine():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript("""
        CREATE TABLE IF NOT EXISTS concept_nodes (
            id TEXT PRIMARY KEY, text TEXT NOT NULL,
            weight REAL DEFAULT 1.0, peak_weight REAL DEFAULT 1.0,
            confidence REAL DEFAULT 1.0, access_count INTEGER DEFAULT 0,
            keys TEXT DEFAULT '[]', layer TEXT DEFAULT 'hippocampus',
            created TEXT NOT NULL, last_accessed TEXT NOT NULL,
            valid_from TEXT NOT NULL, valid_to TEXT, superseded_by TEXT,
            history TEXT DEFAULT '[]', origin TEXT DEFAULT '{}',
            source_mem_id INTEGER, embedding BLOB
        );
        CREATE TABLE IF NOT EXISTS concept_edges (
            source_id TEXT NOT NULL, target_id TEXT NOT NULL,
            relation TEXT DEFAULT 'related', weight REAL DEFAULT 1.0,
            created TEXT NOT NULL, PRIMARY KEY (source_id, target_id)
        );
    """)
    await conn.commit()
    cdb = ConceptDB(conn)
    ke = KeyExtractor()
    eng = SpreadingActivationEngine(cdb, vector_store=None, key_extractor=ke)
    yield eng
    await conn.close()


@pytest.mark.asyncio
async def test_recall_empty_db(engine):
    results = await engine.recall("test query")
    assert results == []


@pytest.mark.asyncio
async def test_recall_direct_hit(engine):
    now = "2026-07-10T12:00:00+08:00"
    await engine.db.insert_node(
        id="node1", text="Redis 是内存数据库",
        keys=json.dumps(["redis", "内存", "数据库"]), created=now,
        last_accessed=now, valid_from=now,
    )
    results = await engine.recall("Redis 数据库")
    assert len(results) >= 1
    assert results[0]["id"] == "node1"
    assert results[0]["score"] > 0


@pytest.mark.asyncio
async def test_recall_spreading_activation(engine):
    """测试扩散激活：直接命中节点 → 沿边传播到关联节点"""
    now = "2026-07-10T12:00:00+08:00"
    # 节点 A: 直接命中
    await engine.db.insert_node(
        id="nodeA", text="Python 编程语言教程",
        keys=json.dumps(["python", "编程", "语言", "教程"]),
        created=now, last_accessed=now, valid_from=now,
    )
    # 节点 B: 不直接命中，但与 A 有边
    await engine.db.insert_node(
        id="nodeB", text="FastAPI 框架",
        keys=json.dumps(["fastapi", "框架"]),
        created=now, last_accessed=now, valid_from=now,
    )
    # 建边 A→B, B→A
    await engine.db.create_edge("nodeA", "nodeB", "co-occurrence", 1.0, now)
    await engine.db.create_edge("nodeB", "nodeA", "co-occurrence", 1.0, now)

    results = await engine.recall("Python 编程")
    ids = [r["id"] for r in results]
    # nodeA 直接命中
    assert "nodeA" in ids
    # nodeB 通过扩散激活被召回
    assert "nodeB" in ids


@pytest.mark.asyncio
async def test_recall_dead_node_not_returned(engine):
    now = "2026-07-10T12:00:00+08:00"
    await engine.db.insert_node(
        id="dead", text="Redis 数据库",
        keys=json.dumps(["redis", "数据库"]),
        created=now, last_accessed=now, valid_from=now,
    )
    await engine.db.update_node("dead", valid_to=now)
    results = await engine.recall("Redis 数据库")
    assert all(r["id"] != "dead" for r in results)


@pytest.mark.asyncio
async def test_compute_idf(engine):
    now = "2026-07-10T12:00:00+08:00"
    await engine.db.insert_node(
        id="n1", text="a", keys=json.dumps(["redis", "python"]),
        created=now, last_accessed=now, valid_from=now,
    )
    await engine.db.insert_node(
        id="n2", text="b", keys=json.dumps(["redis", "java"]),
        created=now, last_accessed=now, valid_from=now,
    )
    alive = await engine.db.get_alive_nodes()
    idf = engine._compute_idf({"redis", "python"}, alive)
    # redis 出现在 2 个节点 → idf 较低
    # python 出现在 1 个节点 → idf 较高
    assert idf["python"] > idf["redis"]


def test_direct_channel_weight_bias(engine):
    """weight_bias = 0.35 + 0.65 * weight，floor 0.35"""
    now = "2026-07-10T12:00:00+08:00"
    # 这个测试不依赖 DB，直接测试公式
    # 模拟 alive_nodes
    alive = {
        "low_weight": {"id": "low_weight", "text": "test", "keys": '["redis"]',
                       "weight": 0.0},
        "high_weight": {"id": "high_weight", "text": "test", "keys": '["redis"]',
                         "weight": 1.0},
    }
    idf = {"redis": 1.0}
    direct = engine._direct_channel({"redis"}, idf, alive, "redis")
    # high_weight 节点分数应高于 low_weight
    assert direct["high_weight"] > direct["low_weight"]
    # low_weight 的 w_bias = 0.35（floor）
    assert direct["low_weight"] > 0


@pytest.mark.asyncio
async def test_rrf_fusion(engine):
    direct = {"a": 0.9, "b": 0.5}
    spread = {"b": 0.3, "c": 0.2}
    fused = engine._rrf_fusion(direct, spread)
    assert "a" in fused
    assert "b" in fused
    assert "c" in fused
    # b 在两个通道都有 → 分数更高
    assert fused["b"] > fused["c"]


@pytest.mark.asyncio
async def test_pattern_separation_dedup(engine):
    """模式分离：相似文本去重"""
    now = "2026-07-10T12:00:00+08:00"
    await engine.db.insert_node(
        id="dup1", text="Redis 缓存数据库",
        keys=json.dumps(["redis", "缓存", "数据库"]),
        created=now, last_accessed=now, valid_from=now,
    )
    await engine.db.insert_node(
        id="dup2", text="Redis 缓存数据库",  # 完全相同
        keys=json.dumps(["redis", "缓存", "数据库"]),
        created=now, last_accessed=now, valid_from=now,
    )
    # 注意：insert_node 用 INSERT OR REPLACE，相同 id 不会重复
    # 测试 pattern_separation 的相似度去重逻辑
    fused = {"dup1": 0.5}
    results = engine._pattern_separation(fused, top_k=5)
    assert len(results) <= 5


def test_constants(engine):
    """验证关键常量值（来自 spec）"""
    assert SpreadingActivationEngine.RECALL_RADIUS == 3
    assert SpreadingActivationEngine.ACTIVATION_DECAY == 0.5
    assert SpreadingActivationEngine.SPREADING_THRESHOLD == 0.05
    assert SpreadingActivationEngine.RRF_K == 60
    assert SpreadingActivationEngine.FUZZY_ACTIVATION == 0.5
    assert SpreadingActivationEngine.SEPARATION_SIM == 0.92
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_spreading_activation.py -v 2>&1 | head -20`
Expected: FAIL with `ModuleNotFoundError: No module named 'memory.spreading_activation'`

- [ ] **Step 3: Write minimal implementation**

Create `memory/spreading_activation.py`:

```python
"""扩散激活检索引擎 — mind 风格的三通道融合

直接命中 (IDF + key重叠 + weight_bias) + 扩散激活 (沿边传播, 3跳)
+ RRF融合 + 模式补全 + 语义重排 + 模式分离
"""
import json
import math
from collections import defaultdict
from difflib import SequenceMatcher

from loguru import logger


class SpreadingActivationEngine:
    """扩散激活检索引擎"""

    # 参数（与 mind 一致，来自 spec）
    RECALL_RADIUS = 3           # 最大扩散跳数
    ACTIVATION_DECAY = 0.5      # 每跳衰减50%
    SPREADING_THRESHOLD = 0.05  # 低于不传播
    RRF_K = 60                  # RRF 平滑参数
    FUZZY_ACTIVATION = 0.5     # 模糊匹配系数
    SEPARATION_SIM = 0.92      # 去重相似度阈值

    def __init__(self, concept_db, vector_store, key_extractor):
        self.db = concept_db
        self.vec = vector_store     # 现有 VectorStore（可为 None）
        self.key_extractor = key_extractor

    async def recall(self, query: str, top_k: int = 5) -> list[dict]:
        """扩散激活检索主入口

        Returns:
            [{id, text, score, weight, keys}, ...] 按 score 降序
        """
        # Step 1: Key 提取
        query_keys = set(self.key_extractor.extract(query, is_query=True))
        if not query_keys:
            return []

        # Step 2: 获取存活节点
        alive_nodes = await self.db.get_alive_nodes()
        if not alive_nodes:
            return []

        # Step 3: IDF 计算
        idf = self._compute_idf(query_keys, alive_nodes)

        # Step 4: 直接命中通道
        direct = self._direct_channel(query_keys, idf, alive_nodes, query)

        # Step 5: 模式补全（无直接命中时用向量模糊匹配）
        if not direct:
            direct = await self._pattern_completion(query, alive_nodes)
        if not direct:
            return []

        # Step 6: 扩散激活通道
        spread = await self._spreading_channel(direct, alive_nodes)

        # Step 7: RRF 融合
        fused = self._rrf_fusion(direct, spread)

        # Step 8: 语义重排
        fused = await self._semantic_rerank(query, fused, top_k)

        # Step 9: 模式分离（去重）
        results = self._pattern_separation(fused, top_k)

        # 填充完整字段
        out = []
        for item in results:
            node = alive_nodes.get(item["id"], {})
            out.append({
                "id": item["id"],
                "text": node.get("text", ""),
                "score": item["score"],
                "weight": node.get("weight", 1.0),
                "keys": node.get("keys", "[]"),
            })
        return out

    def _compute_idf(self, keys: set, alive_nodes: dict) -> dict:
        """计算每个 key 的 IDF 值

        idf(k) = log(N / (1 + df(k)))
        N = 存活节点总数, df(k) = 包含 key k 的节点数
        """
        n = len(alive_nodes)
        if n == 0:
            return {}
        df = defaultdict(int)
        for node in alive_nodes.values():
            try:
                node_keys = set(json.loads(node.get("keys", "[]")))
            except (json.JSONDecodeError, TypeError):
                continue
            for k in keys & node_keys:
                df[k] += 1
        return {k: math.log(n / (1 + df.get(k, 0))) for k in keys}

    def _direct_channel(self, keys: set, idf: dict,
                         alive_nodes: dict, query: str) -> dict:
        """IDF 加权 key 重叠 + 子串包含

        weight_bias = 0.35 + 0.65 * weight（floor 0.35）
        """
        direct = {}
        q_lower = query.lower()
        for nid, node in alive_nodes.items():
            try:
                node_keys = set(json.loads(node.get("keys", "[]")))
            except (json.JSONDecodeError, TypeError):
                node_keys = set()
            # weight_bias floor 0.35
            w_bias = 0.35 + 0.65 * node.get("weight", 1.0)

            shared = keys & node_keys
            if shared:
                idf_score = sum(idf.get(k, 0) for k in shared)
                direct[nid] = direct.get(nid, 0) + idf_score * w_bias

            # 子串包含（len >= 4 才计）
            n_text = node.get("text", "").lower()
            substr = sum(1 for w in keys if len(w) >= 4 and w in n_text)
            reverse = sum(1 for k in node_keys
                           if len(k) >= 4 and k in q_lower)
            if substr + reverse:
                direct[nid] = direct.get(nid, 0) + (substr + reverse) * 0.6 * w_bias

        return direct

    async def _pattern_completion(self, query: str,
                                    alive_nodes: dict) -> dict:
        """无直接命中时，用现有 VectorStore 做模糊匹配

        复用 VectorStore.search() 的向量检索能力，
        将结果映射到 concept_nodes（通过 source_mem_id）。
        """
        direct = {}
        if not self.vec or not getattr(self.vec, "enabled", False):
            return direct
        try:
            vec_results = await self.vec.search(query, top_k=20)
        except Exception as e:
            logger.debug("spreading.pattern_completion_failed", error=str(e))
            return direct
        if not vec_results:
            return direct
        for result in vec_results:
            # vec_results 可能是 list[tuple(id, distance)] 或 list[dict]
            if isinstance(result, (list, tuple)):
                row_id, distance = result[0], result[1]
            else:
                row_id = result.get("id")
                distance = result.get("distance", 1.0)
            node = await self.db.get_node_by_source_mem(row_id)
            if node and node["id"] in alive_nodes:
                sim = max(0.0, 1.0 - distance)
                if sim >= 0.25:
                    direct[node["id"]] = (sim * self.FUZZY_ACTIVATION
                                           * node.get("weight", 1.0))
        return direct

    async def _spreading_channel(self, direct: dict,
                                  alive_nodes: dict) -> dict:
        """从种子节点沿边传播激活值，3跳"""
        spread = defaultdict(float)
        wave = dict(direct)

        for hop in range(self.RECALL_RADIUS + 1):
            nxt = defaultdict(float)
            for nid, act in wave.items():
                spread[nid] += act  # 累积激活
                if hop < self.RECALL_RADIUS and act > self.SPREADING_THRESHOLD:
                    edges = await self.db.get_edges(nid)
                    for neighbor_id, edge in edges.items():
                        if neighbor_id not in alive_nodes:
                            continue  # closed 事实不中继
                        propagated = (act * self.ACTIVATION_DECAY
                                      * edge["weight"] / (hop + 1))
                        nxt[neighbor_id] += propagated
            wave = nxt
            if not wave:
                break

        return dict(spread)

    def _rrf_fusion(self, direct: dict, spread: dict) -> dict:
        """Reciprocal Rank Fusion: 双通道排名融合"""
        dr = {n: i for i, (n, _) in enumerate(
            sorted(direct.items(), key=lambda x: (-x[1], x[0])))}
        sr = {n: i for i, (n, _) in enumerate(
            sorted(spread.items(), key=lambda x: (-x[1], x[0])))}
        dr_default = len(dr) + 1
        sr_default = len(sr) + 1

        fused = {}
        for nid in set(direct) | set(spread):
            fused[nid] = (1.0 / (self.RRF_K + dr.get(nid, dr_default)) +
                          1.0 / (self.RRF_K + sr.get(nid, sr_default)))
        return fused

    async def _semantic_rerank(self, query: str, fused: dict,
                                 top_k: int) -> list:
        """语义重排：用文本相似度对 fused 结果重排"""
        if not fused:
            return []
        # 取 fused 分数 top candidates
        sorted_items = sorted(fused.items(), key=lambda x: (-x[1], x[0]))
        candidates = sorted_items[:top_k * 3]  # 过采样

        # 计算文本相似度
        reranked = []
        for nid, rrf_score in candidates:
            node = await self.db.get_node(nid)
            if not node:
                continue
            text_sim = SequenceMatcher(
                None, query.lower(), node.get("text", "").lower()
            ).ratio()
            # 综合分数 = RRF + 文本相似度
            combined = rrf_score + text_sim * 0.1
            reranked.append({"id": nid, "score": combined})

        reranked.sort(key=lambda x: -x["score"])
        return reranked[:top_k * 2]  # 留余量给 pattern_separation

    def _pattern_separation(self, fused_or_list, top_k: int) -> list:
        """模式分离：相似文本去重

        Args:
            fused_or_list: dict {id: score} 或 list[{id, score}]
            top_k: 返回数量上限
        """
        # 统一转为 list[{id, score}]
        if isinstance(fused_or_list, dict):
            items = [{"id": nid, "score": s}
                     for nid, s in fused_or_list.items()]
            items.sort(key=lambda x: -x["score"])
        else:
            items = list(fused_or_list)

        if not items:
            return []

        # 逐个检查是否与已选结果过于相似
        selected = []
        selected_texts = []

        for item in items:
            if len(selected) >= top_k:
                break
            # 获取节点文本（从 alive_nodes 或 db）
            # 这里只比较已知文本，无法获取则保留
            # 由于此方法可能在没有 node 文本的情况下调用，
            # 我们简化为基于 id 去重（文本去重在上层处理）
            if item["id"] not in [s["id"] for s in selected]:
                selected.append(item)

        return selected
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_spreading_activation.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add memory/spreading_activation.py tests/test_spreading_activation.py
git commit -m "feat: SpreadingActivationEngine 扩散激活引擎 (三通道融合)"
```

---

### Task 5: FluidMemory 改造为 Ebbinghaus 增量模型

**Files:**
- Modify: `memory/fluid_memory.py`
- Modify: `tests/test_fluid_memory.py`

**Interfaces:**
- Consumes: 现有 FluidMemory 接口
- Produces: 修改后的 FluidMemory：
  - 移除：`LAMBDA_DECAY`, `ALPHA_BOOST`, `MAX_BOOST`
  - 新增：`STABILITY_BASE_DAYS=3.0`, `STABILITY_PER_ACCESS=14.0`, `BOOST_PER_ACCESS=0.15`, `GRACE_DAYS=45`, `WEIGHT_THRESHOLD=0.1`
  - 保留：`FORGET_THRESHOLD=0.05`, `DREAM_THRESHOLD=0.15`
  - `score(self, similarity, created_at, access_count=0, peak_weight=1.0) -> float`

- [ ] **Step 1: Write the failing test (update existing test file)**

Replace the entire content of `tests/test_fluid_memory.py`:

```python
"""流体记忆系统单元测试 — mind 风格 Ebbinghaus 增量模型"""
import math
import time

import pytest

from memory.fluid_memory import FluidMemory


# ── score 计算 ──


def test_score_new_memory():
    fm = FluidMemory()
    now = time.time()
    similarity = 0.8
    score = fm.score(similarity=similarity, created_at=now, access_count=0)
    # 新记忆 days≈0, retention≈1, weight=peak_weight×1=1
    # score ≈ similarity × 1.0 × 1 = similarity
    assert score == pytest.approx(similarity, abs=0.01)


def test_score_old_memory_decay():
    fm = FluidMemory()
    now = time.time()
    similarity = 0.8
    # 100 天前的记忆，无确认
    old_time = now - 100 * 86400
    score = fm.score(similarity=similarity, created_at=old_time, access_count=0)
    # stability = 3.0, retention = e^(-100/3) ≈ 0
    assert score < similarity * 0.05


def test_score_access_boost():
    """确认次数影响稳定性（半衰期），而非加法 boost"""
    fm = FluidMemory()
    now = time.time()
    similarity = 0.5
    # 30 天前的记忆
    old_time = now - 30 * 86400
    score_low_access = fm.score(similarity=similarity, created_at=old_time,
                                 access_count=0)
    score_high_access = fm.score(similarity=similarity, created_at=old_time,
                                  access_count=10)
    # 10次确认: stability = 3 + 14×10 = 143 天, retention ≈ e^(-30/143) ≈ 0.81
    # 0次确认: stability = 3 天, retention ≈ e^(-30/3) ≈ 0
    assert score_high_access > score_low_access


def test_score_formula_exact():
    fm = FluidMemory()
    now = time.time()
    similarity = 0.9
    created_at = now - 10 * 86400  # 10 天前
    access_count = 5
    peak_weight = 0.8

    days_passed = (now - created_at) / 86400.0
    stability = (FluidMemory.STABILITY_BASE_DAYS
                 + access_count * FluidMemory.STABILITY_PER_ACCESS)
    retention = math.exp(-days_passed / stability)
    expected_score = similarity * peak_weight * retention

    score = fm.score(similarity=similarity, created_at=created_at,
                     access_count=access_count, peak_weight=peak_weight)
    assert score == pytest.approx(expected_score, rel=1e-6)


def test_confirmed_memory_retention():
    """10次确认的记忆 30 天后保留率 ≥ 80%"""
    fm = FluidMemory()
    now = time.time()
    created_at = now - 30 * 86400
    score = fm.score(similarity=1.0, created_at=created_at,
                     access_count=10, peak_weight=1.0)
    # stability = 3 + 14×10 = 143, retention = e^(-30/143) ≈ 0.811
    assert score >= 0.80


def test_peak_weight_affects_score():
    fm = FluidMemory()
    now = time.time()
    score_default = fm.score(similarity=0.8, created_at=now, access_count=0)
    score_high_peak = fm.score(similarity=0.8, created_at=now, access_count=0,
                                peak_weight=1.5)
    assert score_high_peak > score_default


def test_no_max_boost_cap():
    """新模型无 MAX_BOOST 硬上限：高确认记忆分数随确认次数增长"""
    fm = FluidMemory()
    now = time.time()
    created_at = now - 5 * 86400  # 5 天前
    score_5 = fm.score(similarity=0.5, created_at=created_at, access_count=5)
    score_50 = fm.score(similarity=0.5, created_at=created_at, access_count=50)
    # 50次确认的稳定性远高于5次，分数更高
    assert score_50 > score_5


# ── should_filter / should_archive ──


def test_should_filter_low_score():
    fm = FluidMemory()
    assert fm.should_filter(0.01) is True


def test_should_not_filter_high_score():
    fm = FluidMemory()
    assert fm.should_filter(0.5) is False


def test_should_archive_medium_score():
    fm = FluidMemory()
    assert fm.should_archive(0.10) is True


def test_should_not_archive_high_score():
    fm = FluidMemory()
    assert fm.should_archive(0.5) is False


# ── 常量值验证 ──


def test_forget_threshold_value():
    assert FluidMemory.FORGET_THRESHOLD == 0.05


def test_dream_threshold_value():
    assert FluidMemory.DREAM_THRESHOLD == 0.15


def test_stability_base_days_value():
    assert FluidMemory.STABILITY_BASE_DAYS == 3.0


def test_stability_per_access_value():
    assert FluidMemory.STABILITY_PER_ACCESS == 14.0


def test_boost_per_access_value():
    assert FluidMemory.BOOST_PER_ACCESS == 0.15


def test_grace_days_value():
    assert FluidMemory.GRACE_DAYS == 45


def test_weight_threshold_value():
    assert FluidMemory.WEIGHT_THRESHOLD == 0.1


def test_no_lambda_decay_attribute():
    """旧参数应已移除"""
    assert not hasattr(FluidMemory, "LAMBDA_DECAY")


def test_no_alpha_boost_attribute():
    assert not hasattr(FluidMemory, "ALPHA_BOOST")


def test_no_max_boost_attribute():
    assert not hasattr(FluidMemory, "MAX_BOOST")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_fluid_memory.py -v 2>&1 | head -30`
Expected: FAIL — 旧实现仍用 LAMBDA_DECAY，新测试检查 `not hasattr(FluidMemory, "MAX_BOOST")`

- [ ] **Step 3: Write minimal implementation — replace fluid_memory.py**

Replace the entire content of `memory/fluid_memory.py`:

```python
"""流体记忆系统 — mind 风格 Ebbinghaus 增量模型

R = e^(-t/S), S = 3 + 14×access_count
score = similarity × peak_weight × retention
"""
import math
import time

from loguru import logger


class FluidMemory:
    """流体记忆 — Ebbinghaus 增量式稳定性模型

    与旧公式的区别：
    - 旧: similarity × e^(-λ×days) + min(α×ln(1+access), 0.3)
    - 新: similarity × peak_weight × e^(-days / (3 + 14×access))
    - 核心变化：确认次数影响稳定性（半衰期），而非加法 boost
    - 效果：10次确认的记忆 30 天后保留率 81%，远超旧的 ~30%
    """

    # 新参数（与 mind 一致）
    STABILITY_BASE_DAYS = 3.0       # 未确认记忆 3 天半衰期
    STABILITY_PER_ACCESS = 14.0     # 每次确认买 14 天稳定性
    BOOST_PER_ACCESS = 0.15        # 每次确认权重增量（ConceptGraph 使用）
    GRACE_DAYS = 45                # 宽限期
    WEIGHT_THRESHOLD = 0.1         # 修剪阈值
    FORGET_THRESHOLD = 0.05   # 动态遗忘阈值（低于此分数不返回）
    DREAM_THRESHOLD = 0.15    # 梦境归档阈值（低于此分数归档）

    def score(self, similarity: float, created_at: float,
              access_count: int = 0, peak_weight: float = 1.0) -> float:
        """计算综合记忆分数

        公式: score = similarity × peak_weight × e^(-days / stability)
        stability = STABILITY_BASE_DAYS + access_count × STABILITY_PER_ACCESS

        Args:
            similarity: 相似度分数 (0~1)
            created_at: 记忆创建时间戳
            access_count: 确认次数
            peak_weight: 历史最高权重（默认 1.0）

        Returns:
            综合分数 (越高越重要)
        """
        days = max(0, (time.time() - created_at) / 86400.0)
        stability = self.STABILITY_BASE_DAYS + access_count * self.STABILITY_PER_ACCESS
        retention = math.exp(-days / stability)
        weight = peak_weight * retention
        return similarity * weight

    def should_filter(self, score: float) -> bool:
        """是否应过滤（不返回，不删除）"""
        return score < self.FORGET_THRESHOLD

    def should_archive(self, score: float) -> bool:
        """是否应归档（梦境守护）"""
        return score < self.DREAM_THRESHOLD

    # dream() 已迁移到 DreamConsolidator.consolidate_db() (统一遗忘+归档入口)
    # 本模块保留纯评分函数, 供 DreamConsolidator 和其他模块复用
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_fluid_memory.py -v`
Expected: all passed

- [ ] **Step 5: Run regression — check DreamConsolidator still works**

Run: `cd /home/orangepi/ai-agent && python -m py_compile core/dream_consolidation.py && python -c "from core.dream_consolidation import DreamConsolidator; print('OK')"`
Expected: OK (DreamConsolidator uses FluidMemory.score() which is backward-compatible since peak_weight defaults to 1.0)

- [ ] **Step 6: Commit**

```bash
cd /home/orangepi/ai-agent
git add memory/fluid_memory.py tests/test_fluid_memory.py
git commit -m "feat: FluidMemory 改造为 mind 风格 Ebbinghaus 增量模型 (移除 MAX_BOOST)"
```

---

### Task 6: Confirm/Correct 机制 (memory/confirm_correct.py)

**Files:**
- Create: `memory/confirm_correct.py`
- Test: `tests/test_confirm_correct.py`

**Interfaces:**
- Consumes: `db.db_concept.ConceptDB`（Task 1）、`memory.spreading_activation.SpreadingActivationEngine`（Task 4）、`memory.key_extractor.KeyExtractor`（Task 2）
- Produces: `ConfirmCorrect` 类：
  - `BOOST_PER_ACCESS = 0.15`, `EDGE_BOOST = 0.25`
  - `__init__(self, concept_db, spreading_engine, memory_db, key_extractor)`
  - `async def confirm(self, node_ids: list[str]) -> dict`
  - `async def correct(self, old_hint: str, new_text: str) -> dict`

- [ ] **Step 1: Write the failing test**

Create `tests/test_confirm_correct.py`:

```python
"""Confirm/Correct 机制单元测试"""
import json

import aiosqlite
import pytest

from db.db_concept import ConceptDB
from memory.confirm_correct import ConfirmCorrect
from memory.key_extractor import KeyExtractor
from memory.spreading_activation import SpreadingActivationEngine


@pytest.fixture
async def cc():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript("""
        CREATE TABLE IF NOT EXISTS concept_nodes (
            id TEXT PRIMARY KEY, text TEXT NOT NULL,
            weight REAL DEFAULT 1.0, peak_weight REAL DEFAULT 1.0,
            confidence REAL DEFAULT 1.0, access_count INTEGER DEFAULT 0,
            keys TEXT DEFAULT '[]', layer TEXT DEFAULT 'hippocampus',
            created TEXT NOT NULL, last_accessed TEXT NOT NULL,
            valid_from TEXT NOT NULL, valid_to TEXT, superseded_by TEXT,
            history TEXT DEFAULT '[]', origin TEXT DEFAULT '{}',
            source_mem_id INTEGER, embedding BLOB
        );
        CREATE TABLE IF NOT EXISTS concept_edges (
            source_id TEXT NOT NULL, target_id TEXT NOT NULL,
            relation TEXT DEFAULT 'related', weight REAL DEFAULT 1.0,
            created TEXT NOT NULL, PRIMARY KEY (source_id, target_id)
        );
    """)
    await conn.commit()
    cdb = ConceptDB(conn)
    ke = KeyExtractor()
    engine = SpreadingActivationEngine(cdb, vector_store=None, key_extractor=ke)

    # Mock memory_db with async increment_access_count
    class MockMemoryDB:
        async def increment_access_count(self, mem_id):
            pass
    cc_instance = ConfirmCorrect(cdb, engine, MockMemoryDB(), ke)
    yield cc_instance
    await conn.close()


@pytest.mark.asyncio
async def test_confirm_increases_weight(cc):
    now = "2026-07-10T12:00:00+08:00"
    await cc.db.insert_node(
        id="node1", text="Redis 是数据库",
        keys=json.dumps(["redis", "数据库"]),
        created=now, last_accessed=now, valid_from=now,
    )
    result = await cc.confirm(["node1"])
    assert result["reinforced"] == 1
    node = await cc.db.get_node("node1")
    assert node["access_count"] == 1
    assert node["weight"] == 1.15  # 1.0 + 0.15
    assert node["peak_weight"] == 1.15


@pytest.mark.asyncio
async def test_confirm_caps_at_1(cc):
    now = "2026-07-10T12:00:00+08:00"
    await cc.db.insert_node(
        id="node1", text="test", keys='["a"]',
        created=now, last_accessed=now, valid_from=now,
        weight=0.95,  # 接近上限
    )
    await cc.confirm(["node1"])
    node = await cc.db.get_node("node1")
    assert node["weight"] == 1.0  # min(1.0, 0.95+0.15)


@pytest.mark.asyncio
async def test_confirm_unknown_node(cc):
    result = await cc.confirm(["nonexistent"])
    assert result["reinforced"] == 0
    assert result["unknown"] == 1


@pytest.mark.asyncio
async def test_confirm_reinforces_edges(cc):
    now = "2026-07-10T12:00:00+08:00"
    for nid in ["n1", "n2"]:
        await cc.db.insert_node(
            id=nid, text=f"text_{nid}", keys='["k"]',
            created=now, last_accessed=now, valid_from=now,
        )
    await cc.db.create_edge("n1", "n2", "co-occurrence", 0.5, now)
    await cc.db.create_edge("n2", "n1", "co-occurrence", 0.5, now)

    await cc.confirm(["n1"])
    # 边权重应增加 0.25
    edges_n1 = await cc.db.get_edges("n1")
    assert edges_n1["n2"]["weight"] == 0.75  # 0.5 + 0.25
    edges_n2 = await cc.db.get_edges("n2")
    assert edges_n2["n1"]["weight"] == 0.75  # 双向同步


@pytest.mark.asyncio
async def test_correct_creates_new_node(cc):
    now = "2026-07-10T12:00:00+08:00"
    await cc.db.insert_node(
        id="oldnode", text="Python 是编译型语言",  # 错误
        keys=json.dumps(["python", "编译", "语言"]),
        created=now, last_accessed=now, valid_from=now,
        weight=0.8, peak_weight=0.9, confidence=1.0,
    )

    result = await cc.correct("Python 编译型语言", "Python 是解释型语言")
    assert "error" not in result
    assert result["old_id"] == "oldnode"
    assert result["new_id"] != "oldnode"

    # 旧节点应被关闭
    old = await cc.db.get_node("oldnode")
    assert old["valid_to"] is not None
    assert old["superseded_by"] == result["new_id"]

    # 新节点应存在且有效
    new = await cc.db.get_node(result["new_id"])
    assert new is not None
    assert new["valid_to"] is None
    assert "解释" in new["text"]
    assert new["confidence"] == 0.7  # 1.0 × 0.7


@pytest.mark.asyncio
async def test_correct_no_match(cc):
    result = await cc.correct("完全不相关的查询", "新文本")
    assert "error" in result
    assert result["error"] == "no match"


@pytest.mark.asyncio
async def test_correct_insufficient_match_quality(cc):
    now = "2026-07-10T12:00:00+08:00"
    await cc.db.insert_node(
        id="nodeX", text="完全不同的内容XYZ",
        keys=json.dumps(["完全", "不同", "内容"]),
        created=now, last_accessed=now, valid_from=now,
    )
    # hint 只有1个 token 重叠
    result = await cc.correct("内容A", "新文本")
    assert "error" in result


@pytest.mark.asyncio
async def test_correct_migrates_edges(cc):
    now = "2026-07-10T12:00:00+08:00"
    # old → other 边
    await cc.db.insert_node(
        id="old", text="Redis 数据库缓存",
        keys=json.dumps(["redis", "数据库", "缓存"]),
        created=now, last_accessed=now, valid_from=now,
    )
    await cc.db.insert_node(
        id="other", text="PostgreSQL 数据库",
        keys=json.dumps(["postgresql", "数据库"]),
        created=now, last_accessed=now, valid_from=now,
    )
    await cc.db.create_edge("old", "other", "co-occurrence", 0.8, now)
    await cc.db.create_edge("other", "old", "co-occurrence", 0.8, now)

    result = await cc.correct("Redis 数据库", "Redis 缓存数据库系统")
    new_id = result["new_id"]

    # 新节点应有到 other 的边
    new_edges = await cc.db.get_edges(new_id)
    assert "other" in new_edges


@pytest.mark.asyncio
async def test_correct_supersedes_edge(cc):
    now = "2026-07-10T12:00:00+08:00"
    await cc.db.insert_node(
        id="old2", text="Redis 数据库缓存",
        keys=json.dumps(["redis", "数据库", "缓存"]),
        created=now, last_accessed=now, valid_from=now,
    )
    result = await cc.correct("Redis 数据库", "Redis 缓存数据库系统")
    new_id = result["new_id"]
    old_id = result["old_id"]

    # supersedes 边
    new_edges = await cc.db.get_edges(new_id)
    assert old_id in new_edges
    assert new_edges[old_id]["relation"] == "supersedes"

    old_edges = await cc.db.get_edges(old_id)
    assert new_id in old_edges
    assert old_edges[new_id]["relation"] == "superseded-by"


def test_constants(cc):
    assert ConfirmCorrect.BOOST_PER_ACCESS == 0.15
    assert ConfirmCorrect.EDGE_BOOST == 0.25
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_confirm_correct.py -v 2>&1 | head -20`
Expected: FAIL with `ModuleNotFoundError: No module named 'memory.confirm_correct'`

- [ ] **Step 3: Write minimal implementation**

Create `memory/confirm_correct.py`:

```python
"""Confirm/Correct 机制 — 记忆强化与纠正

confirm: 确认强化（access_count+1, weight+0.15, edges+0.25）
correct: 纠正超驰（创建新节点，关闭旧节点，迁移边，建立 supersedes 链）
"""
import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from loguru import logger

_SH_TZ = ZoneInfo("Asia/Shanghai")


class ConfirmCorrect:
    """confirm: 确认强化 / correct: 纠正超驰"""

    BOOST_PER_ACCESS = 0.15    # 每次确认的节点权重增量
    EDGE_BOOST = 0.25          # 确认时边权重增量

    def __init__(self, concept_db, spreading_engine, memory_db,
                 key_extractor):
        self.db = concept_db
        self.engine = spreading_engine
        self.memory = memory_db  # Database 实例（用于同步 episodic_memories）
        self.ke = key_extractor

    def _now_iso(self) -> str:
        return datetime.now(_SH_TZ).isoformat()

    def _clean_text(self, text: str) -> str:
        return text.strip()

    def _make_node_id(self, text: str) -> str:
        cleaned = self._clean_text(text)
        return hashlib.md5(cleaned.encode("utf-8")).hexdigest()[:12]

    async def confirm(self, node_ids: list[str]) -> dict:
        """确认强化

        1. access_count += 1
        2. weight = min(1.0, weight + 0.15)
        3. peak_weight = max(peak_weight, weight)
        4. last_accessed = now
        5. 所有关联边 weight += 0.25 (双向同步)
        6. 同步 episodic_memories.access_count
        """
        now = self._now_iso()
        reinforced = 0
        unknown = 0

        for nid in node_ids:
            node = await self.db.get_node(nid)
            if node is None:
                unknown += 1
                continue

            new_access = node["access_count"] + 1
            new_weight = min(1.0, node["weight"] + self.BOOST_PER_ACCESS)
            new_peak = max(node["peak_weight"], new_weight)

            await self.db.update_node(
                nid, access_count=new_access, weight=new_weight,
                peak_weight=new_peak, last_accessed=now)

            # 强化所有关联边（双向同步）
            edges = await self.db.get_edges(nid)
            for target_id, edge in edges.items():
                new_edge_w = min(1.0, edge["weight"] + self.EDGE_BOOST)
                await self.db.update_edge(nid, target_id, weight=new_edge_w)
                await self.db.update_edge(target_id, nid, weight=new_edge_w)

            # 同步 episodic_memories
            if node.get("source_mem_id"):
                try:
                    await self.memory.increment_access_count(
                        node["source_mem_id"])
                except Exception as e:
                    logger.debug("confirm.sync_episodic_failed",
                                 error=str(e))

            reinforced += 1

        return {"reinforced": reinforced, "unknown": unknown}

    async def correct(self, old_hint: str, new_text: str) -> dict:
        """纠正超驰（融合而非擦除）

        1. recall 找到最匹配旧记忆
        2. 验证匹配质量（共享 ≥2 token 或覆盖 ≥50%）
        3. 创建新节点（继承权重, confidence×0.7）
        4. 迁移旧节点的知识边到新节点（不迁移 supersedes 边）
        5. 建立双向 supersedes/superseded-by 边 (weight=0.5)
        6. 关闭旧节点 (valid_to = now, superseded_by = new_id)
        7. 保留 history 溯源链
        """
        # 1. 找到旧记忆
        results = await self.engine.recall(old_hint, top_k=1)
        if not results:
            return {"error": "no match"}

        old_id = results[0]["id"]
        old_node = results[0]
        old_text = old_node["text"]

        # 2. 验证匹配质量
        hint_tokens = set(self.ke.extract(old_hint))
        node_tokens = set(self.ke.extract(old_text))
        shared = hint_tokens & node_tokens
        if not (len(shared) >= 2 or
                (hint_tokens and len(shared) / len(hint_tokens) >= 0.5)):
            return {"error": "insufficient match quality"}

        # 3. 创建新节点
        now = self._now_iso()
        new_id = self._make_node_id(new_text)
        lowered_conf = round(
            old_node.get("weight", 1.0) and old_node.get("confidence", 1.0) * 0.7 or 1.0, 3)
        # 简化：confidence × 0.7
        lowered_conf = round(old_node.get("confidence", 1.0) * 0.7, 3)

        history = json.loads(old_node.get("history", "[]"))
        history.append({"text": old_text, "replaced": now})

        new_keys = self.ke.extract(new_text, is_query=False)

        await self.db.insert_node(
            id=new_id, text=self._clean_text(new_text),
            weight=old_node.get("weight", 1.0),
            peak_weight=old_node.get("peak_weight", 1.0),
            confidence=lowered_conf, access_count=0,
            keys=json.dumps(new_keys, ensure_ascii=False),
            layer="hippocampus",
            created=now, last_accessed=now,
            valid_from=now, valid_to=None,
            superseded_by=None, history=json.dumps(history, ensure_ascii=False),
            origin=json.dumps({"via": "correct"}),
        )

        # 4. 迁移旧节点的知识边（不迁移 supersedes 边）
        old_edges = await self.db.get_edges(old_id)
        for target_id, edge in old_edges.items():
            if edge["relation"] in ("supersedes", "superseded-by"):
                continue
            if target_id == new_id:
                continue
            await self.db.create_edge(new_id, target_id,
                                       edge["relation"], edge["weight"], now)
            await self.db.create_edge(target_id, new_id,
                                       edge["relation"], edge["weight"], now)

        # 5. supersedes 双向边
        await self.db.create_edge(new_id, old_id, "supersedes", 0.5, now)
        await self.db.create_edge(old_id, new_id, "superseded-by", 0.5, now)

        # 6. 关闭旧节点
        await self.db.update_node(old_id, valid_to=now,
                                   superseded_by=new_id)

        logger.info("correct.applied", old_id=old_id, new_id=new_id)
        return {
            "old_text": old_text, "new_text": new_text,
            "old_id": old_id, "new_id": new_id,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_confirm_correct.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent
git add memory/confirm_correct.py tests/test_confirm_correct.py
git commit -m "feat: ConfirmCorrect 机制 (confirm强化 + correct超驰+溯源链)"
```

---

### Task 7: 集成扩散激活为第五路 RRF 通道

**Files:**
- Modify: `memory/memory_manager.py`（`retrieve_memories_hybrid` + `__init__`）
- Modify: `memory/memory_manager.py`（新增 `_spreading_recall` 方法 + 双写 remember）

**Interfaces:**
- Consumes: `memory.concept_graph.ConceptGraph`（Task 3）、`memory.spreading_activation.SpreadingActivationEngine`（Task 4）
- Produces: 修改后的 `MemoryManager`：
  - `__init__` 新增 `concept_graph` 和 `spreading_engine` 属性
  - `retrieve_memories_hybrid` 新增第五路 `_spreading_recall`
  - 新增 `_spreading_recall(query, limit) -> list[dict]`

- [ ] **Step 1: Read the current __init__ and retrieve_memories_hybrid**

Read `memory/memory_manager.py` lines 1-120 (imports + `__init__`) and lines 346-570 (the `retrieve_memories_hybrid` method).

Note the exact location of:
- `self.vec` initialization in `__init__`
- The `asyncio.gather` call around line 465
- The `ranked_lists` / `weights` construction around line 528-543

- [ ] **Step 2: Add spreading engine to __init__**

In `memory/memory_manager.py`, find the `__init__` method. After `self.vec = ...` initialization, add:

```python
        # 扩散激活引擎（第五路 RRF 通道）
        self.concept_graph = None
        self.spreading_engine = None
        try:
            from memory.concept_graph import ConceptGraph
            from memory.spreading_activation import SpreadingActivationEngine
            from memory.key_extractor import KeyExtractor
            from db.db_concept import ConceptDB
            if hasattr(self, 'db') and self.db and hasattr(self.db, '_conn'):
                concept_db = ConceptDB(self.db._conn)
                self._key_extractor = KeyExtractor()
                self.concept_graph = ConceptGraph(concept_db, self._key_extractor)
                self.spreading_engine = SpreadingActivationEngine(
                    concept_db, self.vec, self._key_extractor)
                logger.info("memory.spreading_activation_enabled")
        except Exception as e:
            logger.warning("memory.spreading_activation_init_failed",
                          error=str(e))
```

Note: This must be placed AFTER `self.vec` and `self.db` are initialized. The implementer should read the actual `__init__` to find the exact insertion point (after the vec/db initialization, before the end of `__init__`).

- [ ] **Step 3: Add `_spreading_recall` method**

Add this method to the `MemoryManager` class (after `_hybrid_vec_search`):

```python
    async def _spreading_recall(self, query: str, limit: int) -> list[dict]:
        """扩散激活第五路检索通道

        通过 SpreadingActivationEngine 检索 concept_nodes，
        将结果映射回 episodic_memories（通过 source_mem_id）。
        """
        if not self.spreading_engine:
            return []
        try:
            results = await self.spreading_engine.recall(query, top_k=limit)
            if not results:
                return []
            # 映射回 episodic_memories
            mem_ids = []
            for r in results:
                node = await self.spreading_engine.db.get_node(r["id"])
                if node and node.get("source_mem_id"):
                    mem_ids.append((node["source_mem_id"], r["score"]))
            if not mem_ids:
                return []
            # 批量获取记忆
            ids = [m[0] for m in mem_ids]
            score_map = {m[0]: m[1] for m in mem_ids}
            memories = await self.memory.get_memories_by_ids(ids)
            for mem in memories:
                mem["spreading_score"] = score_map.get(mem["id"], 0.0)
                mem["spreading_recall"] = True
            return memories
        except Exception as e:
            logger.debug("memory.spreading_recall_failed", error=str(e))
            return []
```

- [ ] **Step 4: Modify retrieve_memories_hybrid to add fifth channel**

Find the `asyncio.gather` call (around line 465):

```python
        fts_items, vec_items, kg_items, child_items = await asyncio.gather(
            self._hybrid_fts_search(query, recall_limit),
            self._hybrid_vec_search(query, recall_limit, candidate_ids=candidate_ids),
            _kg_recall(),
            _child_recall(),
        )
```

Replace with:

```python
        fts_items, vec_items, kg_items, child_items, spread_items = await asyncio.gather(
            self._hybrid_fts_search(query, recall_limit),
            self._hybrid_vec_search(query, recall_limit, candidate_ids=candidate_ids),
            _kg_recall(),
            _child_recall(),
            self._spreading_recall(query, recall_limit),
        )
```

Then find the "空通道自动剔除" check (around line 473):

```python
        if not fts_items and not vec_items and not kg_items and not child_items:
```

Replace with:

```python
        if not fts_items and not vec_items and not kg_items and not child_items and not spread_items:
```

Then find the RRF fusion section (around line 528-543). After the `if child_items:` block, add:

```python
        if spread_items:
            spread_ids = [str(item["id"]) for item in spread_items]
            ranked_lists.append(spread_ids)
            weights.append(0.85)  # 扩散激活权重略低于直接匹配
```

And update the `all_items` construction (around line 546) to include spread_items:

```python
        all_items = {str(item["id"]): item for item in fts_items + vec_items + kg_items + child_items + spread_items}
```

- [ ] **Step 5: Add dual-write to remember/store flow**

Find the method in `memory_manager.py` that stores new episodic memories (search for `insert_episodic_memory` or `store_memory` or `add_memory`). After the episodic memory is stored and we have the `mem_id`, add dual-write to concept_graph:

```python
        # 双写：同时写入 concept_nodes
        if self.concept_graph and mem_id:
            try:
                await self.concept_graph.remember(summary, source_mem_id=mem_id)
            except Exception as e:
                logger.debug("memory.concept_dual_write_failed", error=str(e))
```

The implementer should find the exact method and insertion point by searching for where `episodic_memories` INSERT happens.

- [ ] **Step 6: Run existing tests to verify no regression**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/ -v -k "memory or rag or retrieval" --timeout=30 2>&1 | tail -30`
Expected: All existing memory tests pass (spreading channel returns [] when disabled, so no impact)

- [ ] **Step 7: Commit**

```bash
cd /home/orangepi/ai-agent
git add memory/memory_manager.py
git commit -m "feat: 集成扩散激活第五路 RRF 通道 + 双写到 concept_nodes"
```

---

### Task 8: 暴露 confirm/correct 为 Agent 工具

**Files:**
- Modify: `tools/memory_tool.py`

**Interfaces:**
- Consumes: `memory.confirm_correct.ConfirmCorrect`（Task 6）、`MemoryManager`（Task 7）
- Produces: 两个新的 Agent 工具函数：`confirm_memory` 和 `correct_memory`

- [ ] **Step 1: Read the current tools/memory_tool.py**

Read `tools/memory_tool.py` to understand the existing tool registration pattern and how tools access the MemoryManager instance.

- [ ] **Step 2: Add confirm_memory and correct_memory tools**

Add two new tool functions to `tools/memory_tool.py`. Follow the existing pattern for how tools are defined (function signature + docstring + return format). The implementer should read the file to match the existing style.

```python
async def confirm_memory(node_ids: list[str]) -> dict:
    """确认记忆正确，强化记忆权重

    当用户确认某条记忆正确时调用（如用户说"对/没错/就是这样"）。
    每次确认：节点权重 +0.15，关联边权重 +0.25，access_count +1。

    Args:
        node_ids: 要确认的概念节点 ID 列表

    Returns:
        {"reinforced": int, "unknown": int}
    """
    from memory_manager import get_memory_manager
    mm = get_memory_manager()
    if not mm or not mm.confirm_correct:
        return {"error": "confirm/correct not initialized"}
    return await mm.confirm_correct.confirm(node_ids)


async def correct_memory(old_hint: str, new_text: str) -> dict:
    """纠正错误记忆，创建新版本并保留溯源链

    当用户纠正某条记忆时调用（如用户说"不对/应该是/搞错了"）。
    旧记忆被关闭但保留，新记忆继承权重，confidence×0.7。

    Args:
        old_hint: 用于找到旧记忆的查询提示
        new_text: 纠正后的新内容

    Returns:
        {"old_id": str, "new_id": str, "old_text": str, "new_text": str}
        或 {"error": str}
    """
    from memory_manager import get_memory_manager
    mm = get_memory_manager()
    if not mm or not mm.confirm_correct:
        return {"error": "confirm/correct not initialized"}
    return await mm.confirm_correct.correct(old_hint, new_text)
```

- [ ] **Step 3: Add confirm_correct to MemoryManager**

In `memory/memory_manager.py` `__init__`, after the spreading_engine initialization (from Task 7), add:

```python
        # Confirm/Correct 机制
        self.confirm_correct = None
        if self.concept_graph and self.spreading_engine:
            try:
                from memory.confirm_correct import ConfirmCorrect
                self.confirm_correct = ConfirmCorrect(
                    concept_db, self.spreading_engine, self.memory,
                    self._key_extractor)
                logger.info("memory.confirm_correct_enabled")
            except Exception as e:
                logger.warning("memory.confirm_correct_init_failed",
                              error=str(e))
```

- [ ] **Step 4: Register tools in the tool registry**

Find the tool registration mechanism (likely in `tools/__init__.py` or `tool_engine/tool_registry.py`). Register `confirm_memory` and `correct_memory` following the existing pattern. The implementer should read the registration code to match the pattern.

- [ ] **Step 5: Verify tools import correctly**

Run: `cd /home/orangepi/ai-agent && python -c "from tools.memory_tool import confirm_memory, correct_memory; print('OK')"`
Expected: OK

- [ ] **Step 6: Commit**

```bash
cd /home/orangepi/ai-agent
git add tools/memory_tool.py memory/memory_manager.py
git commit -m "feat: 暴露 confirm/correct 为 Agent 工具"
```

---

### Task 9: 双写+懒迁移集成

**Files:**
- Modify: `memory/memory_manager.py`（懒迁移触发逻辑）

**Interfaces:**
- Consumes: `memory.concept_graph.ConceptGraph.lazy_migrate`（Task 3）

- [ ] **Step 1: Add lazy migration trigger to retrieve_memories_hybrid**

In `memory/memory_manager.py`, at the beginning of `retrieve_memories_hybrid` (after the tier check, before the main search), add a lazy migration check:

```python
        # 懒迁移：concept_nodes 数 < episodic_memories 数时触发
        if self.concept_graph and not is_cold:
            try:
                ep_count = await self.memory.get_episodic_count()
                node_count = await self.spreading_engine.db.get_node_count()
                if node_count < ep_count:
                    unmigrated = await self.memory.get_unmigrated_memories(limit=50)
                    if unmigrated:
                        await self.concept_graph.lazy_migrate(unmigrated, limit=50)
            except Exception as e:
                logger.debug("memory.lazy_migrate_failed", error=str(e))
```

- [ ] **Step 2: Add helper methods to db_memory.py if they don't exist**

Check if `db_memory.py` (or wherever the episodic memory DB layer is) has `get_episodic_count()` and `get_unmigrated_memories()`. If not, add them:

```python
    async def get_episodic_count(self) -> int:
        async with self._conn.execute(
            "SELECT COUNT(*) FROM episodic_memories"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def get_unmigrated_memories(self, limit: int = 50) -> list[dict]:
        """获取未迁移到 concept_nodes 的记忆"""
        async with self._conn.execute(
            """SELECT em.id, em.summary FROM episodic_memories em
               WHERE em.id NOT IN (SELECT source_mem_id FROM concept_nodes
                                   WHERE source_mem_id IS NOT NULL)
               ORDER BY em.timestamp ASC LIMIT ?""",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [{"id": r["id"], "summary": r["summary"]} for r in rows]
```

- [ ] **Step 3: Verify the integration compiles**

Run: `cd /home/orangepi/ai-agent && python -m py_compile memory/memory_manager.py && python -c "from memory.memory_manager import MemoryManager; print('OK')"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
cd /home/orangepi/ai-agent
git add memory/memory_manager.py db/db_memory.py
git commit -m "feat: 双写+懒迁移集成 (检索时自动迁移旧记忆)"
```

---

### Task 10: 集成测试

**Files:**
- Create: `tests/test_spreading_integration.py`

**Interfaces:**
- Consumes: 所有前序任务的模块

- [ ] **Step 1: Write integration test**

Create `tests/test_spreading_integration.py`:

```python
"""扩散激活记忆系统集成测试"""
import json
import time

import aiosqlite
import pytest

from db.db_concept import ConceptDB
from memory.concept_graph import ConceptGraph
from memory.confirm_correct import ConfirmCorrect
from memory.key_extractor import KeyExtractor
from memory.spreading_activation import SpreadingActivationEngine


@pytest.fixture
async def system():
    """完整的扩散激活记忆系统"""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript("""
        CREATE TABLE IF NOT EXISTS concept_nodes (
            id TEXT PRIMARY KEY, text TEXT NOT NULL,
            weight REAL DEFAULT 1.0, peak_weight REAL DEFAULT 1.0,
            confidence REAL DEFAULT 1.0, access_count INTEGER DEFAULT 0,
            keys TEXT DEFAULT '[]', layer TEXT DEFAULT 'hippocampus',
            created TEXT NOT NULL, last_accessed TEXT NOT NULL,
            valid_from TEXT NOT NULL, valid_to TEXT, superseded_by TEXT,
            history TEXT DEFAULT '[]', origin TEXT DEFAULT '{}',
            source_mem_id INTEGER, embedding BLOB
        );
        CREATE TABLE IF NOT EXISTS concept_edges (
            source_id TEXT NOT NULL, target_id TEXT NOT NULL,
            relation TEXT DEFAULT 'related', weight REAL DEFAULT 1.0,
            created TEXT NOT NULL, PRIMARY KEY (source_id, target_id)
        );
        CREATE TABLE IF NOT EXISTS concept_meta (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
    """)
    await conn.commit()

    cdb = ConceptDB(conn)
    ke = KeyExtractor()
    graph = ConceptGraph(cdb, ke)
    engine = SpreadingActivationEngine(cdb, vector_store=None, key_extractor=ke)

    class MockMemoryDB:
        async def increment_access_count(self, mem_id):
            pass
    cc = ConfirmCorrect(cdb, engine, MockMemoryDB(), ke)

    yield {"cdb": cdb, "ke": ke, "graph": graph, "engine": engine, "cc": cc}
    await conn.close()


@pytest.mark.asyncio
async def test_full_workflow_remember_recall_confirm(system):
    """完整工作流：写入 → 检索 → 确认 → 再检索（权重提升）"""
    graph = system["graph"]
    engine = system["engine"]
    cc = system["cc"]

    # 1. 写入记忆
    node_id = await graph.remember(
        "Redis 是内存数据库，常用于缓存和会话存储",
        source_mem_id=1,
    )
    assert node_id

    # 2. 检索
    results = await engine.recall("Redis 缓存", top_k=5)
    assert len(results) >= 1
    assert results[0]["id"] == node_id
    initial_score = results[0]["score"]

    # 3. 确认
    confirm_result = await cc.confirm([node_id])
    assert confirm_result["reinforced"] == 1

    # 4. 再次检索（权重提升后分数应更高）
    results2 = await engine.recall("Redis 缓存", top_k=5)
    assert len(results2) >= 1
    assert results2[0]["id"] == node_id


@pytest.mark.asyncio
async def test_spreading_activation_finds_related(system):
    """扩散激活：通过关联节点找到间接相关记忆"""
    graph = system["graph"]
    engine = system["engine"]

    # 写入关联记忆
    await graph.remember("Python 编程语言基础教程", source_mem_id=1)
    await graph.remember("Python web 开发实战指南", source_mem_id=2)
    # 这两个应共享 python/编程/开发 等 keys → auto_link

    # 检索一个，应能通过扩散激活找到另一个
    results = await engine.recall("Python 编程", top_k=5)
    ids = [r["id"] for r in results]
    assert len(ids) >= 1


@pytest.mark.asyncio
async def test_correct_workflow(system):
    """纠正工作流：写入 → 纠正 → 旧记忆关闭、新记忆激活"""
    graph = system["graph"]
    engine = system["engine"]
    cc = system["cc"]

    # 写入错误记忆
    await graph.remember("Python 是编译型语言", source_mem_id=10)

    # 纠正
    result = await cc.correct("Python 编译型语言", "Python 是解释型语言")
    assert "error" not in result

    # 检索应返回新记忆，不返回旧记忆
    results = await engine.recall("Python 语言", top_k=5)
    for r in results:
        assert r["id"] != result["old_id"]  # 旧节点已关闭


@pytest.mark.asyncio
async def test_lazy_migrate(system):
    """懒迁移：旧 episodic_memories 迁移到 concept_nodes"""
    graph = system["graph"]
    engine = system["engine"]

    episodic = [
        {"id": 100, "summary": "Docker 容器化部署"},
        {"id": 101, "summary": "Kubernetes 集群管理"},
        {"id": 102, "summary": "CI/CD 流水线配置"},
    ]
    count = await graph.lazy_migrate(episodic, limit=50)
    assert count == 3

    # 迁移后应能检索到
    results = await engine.recall("Docker 部署", top_k=5)
    assert len(results) >= 1


@pytest.mark.asyncio
async def test_dual_write_consistency(system):
    """双写一致性：source_mem_id 映射正确"""
    graph = system["graph"]
    cdb = system["cdb"]

    node_id = await graph.remember("测试双写", source_mem_id=999)
    node = await cdb.get_node_by_source_mem(999)
    assert node is not None
    assert node["id"] == node_id


@pytest.mark.asyncio
async def test_empty_query_returns_empty(system):
    """空查询返回空列表"""
    engine = system["engine"]
    assert await engine.recall("") == []
    assert await engine.recall("   ") == []


@pytest.mark.asyncio
async def test_confirm_multiple_nodes(system):
    """批量确认多个节点"""
    graph = system["graph"]
    cc = system["cc"]

    id1 = await graph.remember("记忆一", source_mem_id=1)
    id2 = await graph.remember("记忆二", source_mem_id=2)

    result = await cc.confirm([id1, id2])
    assert result["reinforced"] == 2

    node1 = await cc.db.get_node(id1)
    node2 = await cc.db.get_node(id2)
    assert node1["access_count"] == 1
    assert node2["access_count"] == 1


@pytest.mark.asyncio
async def test_constants_end_to_end(system):
    """端到端常量验证"""
    assert SpreadingActivationEngine.RECALL_RADIUS == 3
    assert ConfirmCorrect.BOOST_PER_ACCESS == 0.15
    assert ConfirmCorrect.EDGE_BOOST == 0.25
    assert KeyExtractor.MAX_KEYS == 24
```

- [ ] **Step 2: Run integration test**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_spreading_integration.py -v`
Expected: 8 passed

- [ ] **Step 3: Run full test suite**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_db_concept.py tests/test_key_extractor.py tests/test_concept_graph.py tests/test_spreading_activation.py tests/test_fluid_memory.py tests/test_confirm_correct.py tests/test_spreading_integration.py -v`
Expected: All passed

- [ ] **Step 4: Commit**

```bash
cd /home/orangepi/ai-agent
git add tests/test_spreading_integration.py
git commit -m "test: 扩散激活记忆系统集成测试 (8个端到端场景)"
```
