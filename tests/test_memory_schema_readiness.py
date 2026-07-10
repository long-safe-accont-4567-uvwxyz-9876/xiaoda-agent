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
        ):
            conn.execute(f"ALTER TABLE episodic_memories ADD COLUMN {column_sql}")
        conn.execute(
            "CREATE TABLE migration_state (id INTEGER PRIMARY KEY, dirty INTEGER, last_version INTEGER, last_error TEXT)"
        )
        conn.execute("INSERT INTO migration_state VALUES (1, 0, ?, '')", (version,))
        conn.execute("CREATE TABLE memory_versions (id INTEGER PRIMARY KEY, memory_id INTEGER)")
        conn.execute("CREATE TABLE context_audit_log (id INTEGER PRIMARY KEY, memory_id INTEGER)")
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
