import sqlite3

import pytest

from db.database import CURRENT_SCHEMA_VERSION, DatabaseManager
from db.db_temporal_memory import TemporalMemoryDB


TEMPORAL_TABLES = {
    "memory_facts",
    "memory_fact_sources",
    "memory_preferences",
    "memory_preference_sources",
    "memory_edges",
}


async def _schema_version(manager: DatabaseManager) -> int:
    row = await manager.fetch_one("SELECT MAX(version) AS version FROM schema_version")
    return row["version"]


async def _table_names(manager: DatabaseManager) -> set[str]:
    rows = await manager.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")
    return {row["name"] for row in rows}


@pytest.mark.asyncio
async def test_fresh_database_migrates_to_v13_idempotently(tmp_path):
    db_path = tmp_path / "fresh.db"
    manager = DatabaseManager(db_path)

    await manager.init()
    assert CURRENT_SCHEMA_VERSION == 16
    assert await _schema_version(manager) == 16
    assert TEMPORAL_TABLES <= await _table_names(manager)
    assert isinstance(manager.temporal, TemporalMemoryDB)
    assert await manager.fetch_one("PRAGMA foreign_keys") == {"foreign_keys": 1}
    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            """INSERT INTO memory_fact_sources (fact_id, memory_id, created_at)
               VALUES (999, 999, 1)"""
        )
    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            """INSERT INTO memory_edges
               (source_memory_id, target_memory_id, edge_type, created_at, updated_at)
               VALUES (999, 1000, 'similar', 1, 1)"""
        )

    await manager._migrate_v13()
    await manager.commit()

    await manager.init()
    versions = await manager.fetch_all(
        "SELECT version, COUNT(*) AS count FROM schema_version GROUP BY version"
    )
    assert versions[-1] == {"version": 16, "count": 1}
    await manager.close()


@pytest.mark.asyncio
async def test_v12_database_upgrades_without_losing_existing_data(tmp_path):
    db_path = tmp_path / "v12.db"
    manager = DatabaseManager(db_path)
    await manager.init()
    await manager.execute(
        "INSERT INTO episodic_memories (timestamp, summary) VALUES (?, ?)",
        (123.0, "preserve me"),
    )
    await manager.close()

    with sqlite3.connect(db_path) as conn:
        for table in TEMPORAL_TABLES:
            conn.execute(f"DROP TABLE {table}")
        conn.execute("DELETE FROM schema_version WHERE version > 12")
        conn.commit()

    upgraded = DatabaseManager(db_path)
    await upgraded.init()
    assert await _schema_version(upgraded) == 16
    assert await upgraded.fetch_one(
        "SELECT summary FROM episodic_memories WHERE timestamp = ?", (123.0,)
    ) == {"summary": "preserve me"}
    assert TEMPORAL_TABLES <= await _table_names(upgraded)
    await upgraded.close()


@pytest.mark.asyncio
async def test_fact_queries_support_current_as_of_filters_and_null_valid_time(tmp_path):
    manager = DatabaseManager(tmp_path / "facts.db")
    await manager.init()
    temporal = TemporalMemoryDB(manager._conn)
    rows = [
        ("user", "city", "Paris", 10.0, 30.0, 12.0, 40.0, "superseded", "f1"),
        ("user", "city", "Berlin", 30.0, None, 35.0, None, "active", "f2"),
        ("user", "language", "Chinese", None, None, 15.0, None, "active", "f3"),
        ("user", "city", "Rome", 20.0, None, 20.0, None, "uncertain", "f4"),
    ]
    for subject, predicate, obj, valid_from, valid_to, learned_at, expired_at, status, fact_hash in rows:
        await manager.execute(
            """INSERT INTO memory_facts
               (subject, predicate, object, valid_from, valid_to, learned_at, expired_at,
                status, fact_hash, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (subject, predicate, obj, valid_from, valid_to, learned_at, expired_at,
             status, fact_hash, learned_at, learned_at),
        )

    assert [row["object"] for row in await temporal.get_current_facts(subject="user")] == [
        "Berlin",
        "Chinese",
    ]
    assert [row["object"] for row in await temporal.get_facts_as_of(25.0, known_at=20.0)] == [
        "Paris",
        "Chinese",
    ]
    assert [row["object"] for row in await temporal.get_facts_as_of(35.0, known_at=36.0)] == [
        "Berlin",
        "Chinese",
    ]
    await manager.close()


@pytest.mark.asyncio
async def test_preference_queries_support_current_as_of_filters_and_null_valid_time(tmp_path):
    manager = DatabaseManager(tmp_path / "preferences.db")
    await manager.init()
    temporal = TemporalMemoryDB(manager._conn)
    rows = [
        ("drink", "coffee", "food", "global", 10.0, 30.0, 12.0, 40.0, "superseded"),
        ("drink", "tea", "food", "global", 30.0, None, 35.0, None, "active"),
        ("theme", "dark", "ui", "work", None, None, 15.0, None, "active"),
        ("drink", "water", "food", "global", 20.0, None, 20.0, None, "uncertain"),
    ]
    for key, value, pref_type, scope, valid_from, valid_to, learned_at, expired_at, status in rows:
        await manager.execute(
            """INSERT INTO memory_preferences
               (preference_key, preference_value, preference_type, scope, valid_from, valid_to,
                learned_at, expired_at, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (key, value, pref_type, scope, valid_from, valid_to, learned_at, expired_at,
             status, learned_at, learned_at),
        )

    assert [row["preference_value"] for row in await temporal.get_current_preferences()] == [
        "tea",
        "dark",
    ]
    assert [row["preference_value"] for row in await temporal.get_preferences_as_of(
        25.0, known_at=20.0, preference_type="food"
    )] == ["coffee"]
    assert [row["preference_value"] for row in await temporal.get_preferences_as_of(
        35.0, known_at=36.0, scope="global"
    )] == ["tea"]
    await manager.close()


@pytest.mark.asyncio
async def test_supersede_fact_closes_system_time_and_only_known_valid_time(tmp_path):
    manager = DatabaseManager(tmp_path / "supersede-fact.db")
    await manager.init()
    temporal = TemporalMemoryDB(manager._conn)
    old_id = await manager.execute(
        """INSERT INTO memory_facts
           (subject, predicate, object, valid_from, learned_at, status, fact_hash, created_at, updated_at)
           VALUES ('user', 'city', 'Paris', 10, 12, 'active', 'old-fact', 12, 12)"""
    )
    complementary_id = await manager.execute(
        """INSERT INTO memory_facts
           (subject, predicate, object, learned_at, status, fact_hash, created_at, updated_at)
           VALUES ('user', 'language', 'Chinese', 13, 'active', 'complementary', 13, 13)"""
    )
    new_id = await manager.execute(
        """INSERT INTO memory_facts
           (subject, predicate, object, valid_from, learned_at, status, fact_hash, created_at, updated_at)
           VALUES ('user', 'city', 'Berlin', 30, 35, 'active', 'new-fact', 35, 35)"""
    )

    await temporal.supersede_fact(old_id, new_id, effective_at=30.0, known_at=35.0)
    old = await manager.fetch_one("SELECT * FROM memory_facts WHERE id = ?", (old_id,))
    complementary = await manager.fetch_one(
        "SELECT status, valid_to, expired_at FROM memory_facts WHERE id = ?", (complementary_id,)
    )
    assert (old["status"], old["valid_to"], old["expired_at"], old["superseded_by"]) == (
        "superseded", 30.0, 35.0, new_id
    )
    assert complementary == {"status": "active", "valid_to": None, "expired_at": None}
    assert [row["object"] for row in await temporal.get_facts_as_of(20.0, known_at=34.0)] == [
        "Paris", "Chinese"
    ]
    assert [row["object"] for row in await temporal.get_facts_as_of(40.0, known_at=36.0)] == [
        "Chinese", "Berlin"
    ]

    unknown_old_id = await manager.execute(
        """INSERT INTO memory_facts
           (subject, predicate, object, learned_at, status, fact_hash, created_at, updated_at)
           VALUES ('user', 'job', 'Engineer', 40, 'active', 'unknown-old', 40, 40)"""
    )
    unknown_new_id = await manager.execute(
        """INSERT INTO memory_facts
           (subject, predicate, object, learned_at, status, fact_hash, created_at, updated_at)
           VALUES ('user', 'job', 'Manager', 50, 'active', 'unknown-new', 50, 50)"""
    )
    await temporal.supersede_fact(unknown_old_id, unknown_new_id, known_at=50.0)
    unknown_old = await manager.fetch_one(
        "SELECT valid_to, expired_at FROM memory_facts WHERE id = ?", (unknown_old_id,)
    )
    assert unknown_old == {"valid_to": None, "expired_at": 50.0}
    await manager.close()


@pytest.mark.asyncio
async def test_supersede_preference_preserves_history_and_other_keys(tmp_path):
    manager = DatabaseManager(tmp_path / "supersede-preference.db")
    await manager.init()
    temporal = TemporalMemoryDB(manager._conn)
    old_id = await manager.execute(
        """INSERT INTO memory_preferences
           (preference_key, preference_value, valid_from, learned_at, status, created_at, updated_at)
           VALUES ('drink', 'coffee', 10, 12, 'active', 12, 12)"""
    )
    other_id = await manager.execute(
        """INSERT INTO memory_preferences
           (preference_key, preference_value, learned_at, status, created_at, updated_at)
           VALUES ('theme', 'dark', 13, 'active', 13, 13)"""
    )
    new_id = await manager.execute(
        """INSERT INTO memory_preferences
           (preference_key, preference_value, valid_from, learned_at, status, created_at, updated_at)
           VALUES ('drink', 'tea', 30, 35, 'active', 35, 35)"""
    )

    await temporal.supersede_preference(old_id, new_id, effective_at=30.0, known_at=35.0)
    old = await manager.fetch_one("SELECT * FROM memory_preferences WHERE id = ?", (old_id,))
    other = await manager.fetch_one(
        "SELECT status, valid_to, expired_at FROM memory_preferences WHERE id = ?", (other_id,)
    )
    assert (old["status"], old["valid_to"], old["expired_at"], old["superseded_by"]) == (
        "superseded", 30.0, 35.0, new_id
    )
    assert other == {"status": "active", "valid_to": None, "expired_at": None}
    assert [row["preference_value"] for row in await temporal.get_preferences_as_of(
        20.0, known_at=34.0
    )] == ["coffee", "dark"]
    assert [row["preference_value"] for row in await temporal.get_current_preferences()] == [
        "dark", "tea"
    ]
    await manager.close()