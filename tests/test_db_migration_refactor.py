"""db/database.py 迁移拆分后的结构契约测试。"""
from __future__ import annotations

from db.database import DatabaseManager


def test_migration_entries_are_complete_and_ordered():
    db = DatabaseManager.__new__(DatabaseManager)
    entries = db._migration_entries()
    versions = [v for v, _, _ in entries]
    assert versions == list(range(1, 29))
    for _v, _desc, migrate_fn in entries:
        assert callable(migrate_fn)


def test_migrate_v14_helpers_exist():
    db = DatabaseManager.__new__(DatabaseManager)
    assert callable(db._detect_fts5)
    assert callable(db._migrate_v14_cognitive_tables)
    assert callable(db._migrate_v14_kg_v2_tables)


def test_run_migrations_helpers_exist():
    db = DatabaseManager.__new__(DatabaseManager)
    assert callable(db._setup_migration_state)
    assert callable(db._recover_dirty_state)
    assert callable(db._current_schema_version)
    assert callable(db._check_migration_integrity)
