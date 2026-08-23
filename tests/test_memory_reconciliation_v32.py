from __future__ import annotations

import sqlite3

import pytest

from db.database import CURRENT_SCHEMA_VERSION, DatabaseManager
from db.db_memory_utils import active_memory_visibility_sql


def test_shadow_visibility_keeps_active_raw_and_knowledge(monkeypatch) -> None:
    import config_constants

    monkeypatch.setattr(config_constants, "MEMORY_RECONCILIATION_MODE", "shadow")
    predicate = active_memory_visibility_sql("em")
    assert predicate == "em.status = 'active'"
    assert "memory_knowledge_sources" not in predicate


def test_enforce_visibility_hides_raw_with_active_canonical(monkeypatch) -> None:
    import config

    monkeypatch.setattr(config, "MEMORY_RECONCILIATION_MODE", "enforce")
    monkeypatch.setattr(
        config, "MEMORY_RECONCILIATION_ALLOWED_ACTIONS", "store", raising=False,
    )
    predicate = active_memory_visibility_sql("em")
    assert "memory_knowledge_sources" in predicate
    assert "active_knowledge.status = 'active'" in predicate


@pytest.mark.asyncio
async def test_v32_repairs_missing_visibility_column(tmp_path) -> None:
    path = tmp_path / "partial-v32.db"
    original = DatabaseManager(path)
    await original.init()
    await original.close()

    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX IF EXISTS idx_episodic_active_scope")
        conn.execute("ALTER TABLE episodic_memories DROP COLUMN superseded_by")
        conn.commit()

    repaired = DatabaseManager(path)
    await repaired.init()
    columns = {
        row["name"] for row in await repaired.fetch_all(
            "PRAGMA table_info(episodic_memories)"
        )
    }
    version = await repaired.fetch_one(
        "SELECT MAX(version) AS version FROM schema_version"
    )
    assert "superseded_by" in columns
    assert version == {"version": CURRENT_SCHEMA_VERSION}
    await repaired.close()
