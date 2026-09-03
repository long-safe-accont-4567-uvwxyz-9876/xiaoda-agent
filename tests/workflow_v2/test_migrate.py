# tests/workflow_v2/test_migrate.py
import sqlite3

import aiosqlite
import pytest

from db.db_workflow import migrate_review_attempt_uniqueness
from workflow_v2.migrate import migrate_v1
from workflow_v2.models import NodeType


def test_migrate_maps_types_and_chains_edges():
    v1 = {
        "id": "w1", "name": "demo",
        "nodes": [
            {"id": "n1", "type": "tool", "ref": "t.a"},
            {"id": "n2", "type": "skill", "ref": "s.b"},
            {"id": "n3", "type": "model", "ref": "gpt"},
        ],
    }
    rev, report = migrate_v1(v1)
    ids = {n.id: n for n in rev.nodes}
    assert ids["n1"].type == NodeType.TOOL
    # skill -> agent with skill_refs, model -> agent with model_policy
    assert ids["n2"].type == NodeType.AGENT and "s.b" in ids["n2"].config["skill_refs"]
    assert ids["n3"].type == NodeType.AGENT and ids["n3"].config["model_policy"]["ref"] == "gpt"
    # start/end synthesized, linear edges preserved
    assert any(n.type == NodeType.START for n in rev.nodes)
    assert any(n.type == NodeType.END for n in rev.nodes)


def test_migrate_unknown_custom_becomes_legacy_prompt_with_warning():
    v1 = {"id": "w2", "name": "d2", "nodes": [{"id": "c", "type": "custom", "note": "freeform"}]}
    rev, report = migrate_v1(v1)
    node = next(n for n in rev.nodes if n.id == "c")
    assert node.type == NodeType.LEGACY_PROMPT
    assert any(r["node_id"] == "c" and r["warning"] for r in report)


@pytest.mark.asyncio
async def test_review_unique_migration_preserves_history_and_is_idempotent():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("""CREATE TABLE wf_review (
        review_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        title TEXT DEFAULT '',
        note TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        decided_by TEXT,
        decision_note TEXT DEFAULT '',
        created_at REAL DEFAULT 0,
        decided_at REAL
    )""")
    await conn.executemany(
        "INSERT INTO wf_review(review_id, run_id, node_id, attempt, status, created_at) "
        "VALUES(?,?,?,?,?,?)",
        [
            ("rev-old", "r1", "review", 1, "approved", 1.0),
            ("rev-new", "r1", "review", 1, "pending", 2.0),
        ],
    )
    await conn.commit()

    await migrate_review_attempt_uniqueness(conn)
    await migrate_review_attempt_uniqueness(conn)

    rows = await (await conn.execute(
        "SELECT review_id, run_id, node_id, attempt, status FROM wf_review ORDER BY created_at"
    )).fetchall()
    assert len(rows) == 2
    assert rows[0]["run_id"] == "r1"
    assert rows[0]["node_id"] == "review"
    assert rows[0]["attempt"] == 1
    assert rows[1]["status"] == "superseded"
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute(
            "INSERT INTO wf_review(review_id, run_id, node_id, attempt) VALUES(?,?,?,?)",
            ("rev-third", "r1", "review", 1),
        )
    await conn.close()
