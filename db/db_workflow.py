from __future__ import annotations
import aiosqlite

DDL = [
    """CREATE TABLE IF NOT EXISTS wf_definition (
        workflow_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        enabled INTEGER DEFAULT 1,
        current_revision_id TEXT,
        etag TEXT DEFAULT '',
        created_at REAL DEFAULT 0,
        updated_at REAL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS wf_revision (
        revision_id TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL,
        graph_json TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        created_at REAL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS wf_run (
        run_id TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        status TEXT NOT NULL,
        lock_version INTEGER NOT NULL DEFAULT 0,
        parent_run_id TEXT,
        idempotency_key TEXT,
        input_json TEXT DEFAULT '{}',
        output_json TEXT DEFAULT '{}',
        cancel_requested_at REAL,
        created_at REAL DEFAULT 0,
        updated_at REAL DEFAULT 0
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_wf_run_idem
        ON wf_run(workflow_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL""",
    """CREATE TABLE IF NOT EXISTS wf_step_run (
        run_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        status TEXT NOT NULL,
        input_json TEXT DEFAULT '{}',
        output_json TEXT DEFAULT '{}',
        error_code TEXT,
        error_message TEXT,
        lease_owner TEXT,
        lease_expires_at REAL,
        PRIMARY KEY (run_id, node_id, attempt)
    )""",
    """CREATE TABLE IF NOT EXISTS wf_run_event (
        run_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        run_status TEXT NOT NULL,
        step_id TEXT,
        attempt INTEGER,
        payload_json TEXT DEFAULT '{}',
        timestamp REAL DEFAULT 0,
        schema_version INTEGER DEFAULT 1,
        PRIMARY KEY (run_id, seq)
    )""",
]


async def create_schema(conn: aiosqlite.Connection) -> None:
    for stmt in DDL:
        await conn.execute(stmt)
    await conn.commit()
