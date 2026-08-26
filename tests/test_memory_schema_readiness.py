import sqlite3

from doctor.memory_schema_readiness import inspect_memory_schema


def _make_db(path, version: int, *, complete: bool = False) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
    conn.execute("INSERT INTO schema_version(version, applied_at) VALUES (?, 0)", (version,))
    conn.execute("CREATE TABLE episodic_memories (id INTEGER PRIMARY KEY, summary TEXT NOT NULL)")
    if complete:
        for column_sql in (
            "entities TEXT DEFAULT ''",
            "event_type TEXT DEFAULT ''",
            "metadata_json TEXT DEFAULT '{}'",
            "content_hash TEXT DEFAULT ''",
            "version INTEGER DEFAULT 1",
            "memory_type TEXT DEFAULT 'event'",
            "classification_status TEXT DEFAULT 'pending'",
            "classification_version INTEGER DEFAULT 0",
            "classified_at REAL DEFAULT 0",
            "status TEXT DEFAULT 'active'",
            "superseded_by INTEGER",
        ):
            conn.execute(f"ALTER TABLE episodic_memories ADD COLUMN {column_sql}")
        conn.execute(
            "CREATE TABLE migration_state (id INTEGER PRIMARY KEY, dirty INTEGER, last_version INTEGER, last_error TEXT)"
        )
        conn.execute("INSERT INTO migration_state VALUES (1, 0, ?, '')", (version,))
        conn.execute("CREATE TABLE memory_versions (id INTEGER PRIMARY KEY, memory_id INTEGER)")
        conn.execute("CREATE TABLE context_audit_log (id INTEGER PRIMARY KEY, memory_id INTEGER)")
        # v14 新增表
        for table in (
            "kg_entities_v2", "kg_relations_v2", "kg_episodes",
            "memory_facts", "memory_preferences", "memory_knowledge_sources",
            "memory_reconciliation_jobs", "memory_reconciliation_actions",
            "memory_reconciliation_targets", "memory_reconciliation_snapshots",
            "memory_index_outbox", "memory_retrieval_epochs",
        ):
            conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()


def test_reports_stale_runtime_database_and_missing_capabilities(tmp_path):
    db_path = tmp_path / "legacy.db"
    _make_db(db_path, 9)

    report = inspect_memory_schema(db_path, expected_version=12)

    assert report.ready is False
    assert report.current_version == 9
    assert report.expected_version == 12
    assert report.dirty_state_available is False
    assert "episodic_memories.entities" in report.missing_capabilities
    assert "memory_versions" in report.missing_capabilities
    assert "context_audit_log" in report.missing_capabilities


def test_reports_ready_only_when_version_and_required_schema_are_present(tmp_path):
    db_path = tmp_path / "ready.db"
    _make_db(db_path, 12, complete=True)

    report = inspect_memory_schema(db_path, expected_version=12)

    assert report.ready is True
    assert report.current_version == 12
    assert report.missing_capabilities == ()
    assert report.dirty is False


def test_reports_memory_type_capability_missing_even_when_version_is_current(tmp_path):
    db_path = tmp_path / "false-current.db"
    _make_db(db_path, 31, complete=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE episodic_memories DROP COLUMN memory_type")
        conn.commit()

    report = inspect_memory_schema(db_path, expected_version=31)

    assert report.ready is False
    assert "episodic_memories.memory_type" in report.missing_capabilities
