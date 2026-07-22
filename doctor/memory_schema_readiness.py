"""Read-only diagnostics for memory database migration readiness."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

_REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "episodic_memories": (
        "entities",
        "event_type",
        "metadata_json",
        "content_hash",
        "version",
    ),
}
_REQUIRED_TABLES = ("memory_versions", "context_audit_log", "kg_entities_v2", "kg_relations_v2", "kg_episodes", "memory_facts", "memory_preferences")


@dataclass(frozen=True)
class MemorySchemaReadiness:
    path: Path
    ready: bool
    current_version: int
    expected_version: int
    dirty_state_available: bool
    dirty: bool | None
    missing_capabilities: tuple[str, ...]


def inspect_memory_schema(
    db_path: str | Path,
    *,
    expected_version: int,
) -> MemorySchemaReadiness:
    """Inspect a SQLite memory database without mutating it."""
    path = Path(db_path)
    if not path.is_file():
        return MemorySchemaReadiness(
            path=path,
            ready=False,
            current_version=0,
            expected_version=expected_version,
            dirty_state_available=False,
            dirty=None,
            missing_capabilities=("database_file",),
        )

    uri = f"file:{path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        current_version = 0
        if "schema_version" in tables:
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            current_version = int(row[0] or 0) if row else 0

        missing: list[str] = []
        for table, required_columns in _REQUIRED_COLUMNS.items():
            if table not in tables:
                missing.append(table)
                continue
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            missing.extend(
                f"{table}.{column}"
                for column in required_columns
                if column not in columns
            )
        missing.extend(table for table in _REQUIRED_TABLES if table not in tables)

        dirty_state_available = "migration_state" in tables
        dirty: bool | None = None
        if dirty_state_available:
            row = conn.execute(
                "SELECT dirty FROM migration_state WHERE id = 1"
            ).fetchone()
            dirty = bool(row[0]) if row is not None else None

        missing_tuple = tuple(missing)
        ready = (
            current_version >= expected_version
            and not missing_tuple
            and dirty_state_available
            and dirty is False
        )
        return MemorySchemaReadiness(
            path=path,
            ready=ready,
            current_version=current_version,
            expected_version=expected_version,
            dirty_state_available=dirty_state_available,
            dirty=dirty,
            missing_capabilities=missing_tuple,
        )
    finally:
        conn.close()
