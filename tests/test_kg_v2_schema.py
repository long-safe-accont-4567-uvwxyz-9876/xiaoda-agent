"""KG v2 Schema 迁移测试 — 表创建、数据迁移、幂等性。"""
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
async def test_fresh_database_migrates_to_latest(tmp_path):
    db_path = tmp_path / "fresh_kg.db"
    manager = DatabaseManager(db_path)
    await manager.init()
    assert CURRENT_SCHEMA_VERSION == 18
    assert await _schema_version(manager) == 18
    assert V2_TABLES <= await _table_names(manager)
    await manager.close()


@pytest.mark.asyncio
async def test_v14_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "idempotent_v14.db"
    manager = DatabaseManager(db_path)
    await manager.init()
    await manager._migrate_v14()
    await manager.commit()
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

    with sqlite3.connect(db_path) as conn:
        for t in V2_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.execute("DELETE FROM schema_version WHERE version >= 14")
        conn.commit()

    upgraded = DatabaseManager(db_path)
    await upgraded.init()
    assert await _schema_version(upgraded) == 18

    entity = await upgraded.fetch_one("SELECT * FROM kg_entities_v2 WHERE name = ?", ("篮球",))
    assert entity is not None
    assert entity["kind"] == "概念"
    assert entity["summary"] == '["团队运动"]'
    assert entity["summary_version"] == 0

    rel = await upgraded.fetch_one("SELECT * FROM kg_relations_v2 WHERE id = ?", ("REL-old1",))
    assert rel is not None
    assert rel["from_entity"] == "用户"
    assert rel["relation_type"] == "喜欢"
    assert rel["to_entity"] == "篮球"
    assert rel["fact"] == "用户 喜欢 篮球"
    assert rel["is_current"] == 1
    assert rel["valid_at"] == 1000.0

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
    row = await manager.fetch_one(
        "SELECT id FROM kg_entities_v2_fts WHERE name_summary MATCH ?", ('"测试实体"',)
    )
    assert row is not None
    assert row["id"] == "ENT-fts1"
    await manager.close()