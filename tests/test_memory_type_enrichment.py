"""P0-1 memory taxonomy, enrichment pipeline, and v31 migration contracts."""
from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from db.database import CURRENT_SCHEMA_VERSION, DatabaseManager
from memory.enrichment import parse_memory_enrichment
from memory.fsrs_model import S_PERMANENT
from memory.memory_manager import MemoryManager
from memory.scope import Scope


def _manager(db: DatabaseManager) -> MemoryManager:
    mgr = MemoryManager.__new__(MemoryManager)
    mgr.db = db
    mgr.memory = db.memory
    mgr.vec = None
    mgr.kg = None
    mgr._security_filter = None
    mgr._reranker = None
    mgr._governance = None
    mgr._last_encode_time = 0
    mgr._pending_encode = False
    mgr._last_message_time = time.time()
    mgr.entity_extractor = None
    mgr.entity_store = None
    mgr.concept_graph = None
    mgr.spreading_engine = None
    mgr._memory_count_cache = None
    mgr._memory_count_ts = 0
    mgr._query_cache = None
    mgr._assessor = None
    mgr.router = None
    mgr._query_transformer = None
    mgr._save_state_json = MagicMock()
    mgr.invalidate_memory_count_cache = MagicMock()
    mgr.invalidate_read_caches = MagicMock()
    mgr.invalidate_query_cache = MagicMock()
    return mgr


@pytest.fixture
async def memory_env(tmp_path):
    db = DatabaseManager(tmp_path / "memory-type.db")
    await db.init()
    yield db, _manager(db)
    await db.close()


@pytest.mark.parametrize("memory_type", ["fact", "event", "affect", "relation", "instruction"])
def test_parser_accepts_exact_memory_taxonomy(memory_type):
    parsed = parse_memory_enrichment(
        json.dumps({"memory_type": memory_type, "importance": 0.7})
    )
    assert parsed.memory_type == memory_type
    assert parsed.classification_status == "classified"
    assert parsed.importance == 0.7


@pytest.mark.parametrize(
    "importance",
    [True, False, "0.8", float("nan"), float("inf"), float("-inf"), -0.1, 1.1],
)
def test_parser_rejects_invalid_importance_without_affecting_other_fields(importance):
    parsed = parse_memory_enrichment(
        json.dumps({"memory_type": "fact", "importance": importance})
    )
    assert parsed.memory_type == "fact"
    assert parsed.classification_status == "classified"
    assert parsed.importance is None


def test_parser_falls_back_for_invalid_enum_and_ignores_unknown_fields():
    parsed = parse_memory_enrichment(
        "```json\n"
        '{"memory_type":"opinion","importance":0.8,"unknown":"ignored"}'
        "\n```"
    )
    assert parsed.memory_type == "event"
    assert parsed.classification_status == "fallback"
    assert parsed.importance == 0.8
    assert not hasattr(parsed, "unknown")


def test_parser_reuses_thinking_cleanup_and_bounds_structured_fields():
    payload = {
        "memory_type": "event",
        "entities": ["entity", "x" * 101] + [f"e{i}" for i in range(20)],
        "event_type": "x" * 100,
        "metadata": {
            "decision": "d" * 500,
            "topic": "topic",
            "mood": "calm",
            "unknown": "ignored",
        },
    }
    parsed = parse_memory_enrichment(
        f"<think>private reasoning</think>\n```json\n{json.dumps(payload)}\n```"
    )
    assert parsed.entities == (
        "entity", "e0", "e1", "e2", "e3", "e4", "e5", "e6", "e7", "e8",
    )
    assert parsed.event_type == ""
    assert parsed.metadata == {"topic": "topic", "mood": "calm"}


def test_config_switch_defaults_false_and_is_reexported(monkeypatch):
    monkeypatch.delenv("MEMORY_TYPE_ENRICHMENT_ENABLED", raising=False)
    import config
    import config_constants

    importlib.reload(config_constants)
    importlib.reload(config)
    assert config_constants.MEMORY_TYPE_ENRICHMENT_ENABLED is False
    assert config.MEMORY_TYPE_ENRICHMENT_ENABLED is False
    assert "MEMORY_TYPE_ENRICHMENT_ENABLED" in config.__all__


@pytest.mark.asyncio
async def test_fresh_schema_contains_current_memory_type_capabilities(tmp_path):
    db = DatabaseManager(tmp_path / "fresh-v31.db")
    await db.init()
    columns = {
        row["name"]: row
        for row in await db.fetch_all("PRAGMA table_info(episodic_memories)")
    }
    assert CURRENT_SCHEMA_VERSION == 32
    assert columns["memory_type"]["dflt_value"] == "'event'"
    assert columns["classification_status"]["dflt_value"] == "'pending'"
    assert columns["classification_version"]["dflt_value"] == "0"
    assert columns["classified_at"]["dflt_value"] == "0"
    assert columns["status"]["dflt_value"] == "'active'"
    assert columns["superseded_by"]["dflt_value"] is None
    assert await db.fetch_one("SELECT MAX(version) AS version FROM schema_version") == {
        "version": CURRENT_SCHEMA_VERSION
    }
    await db.close()


@pytest.mark.asyncio
async def test_v30_upgrade_is_non_destructive_backfills_high_precision_and_is_idempotent(tmp_path):
    db_path = tmp_path / "upgrade-v30.db"
    original = DatabaseManager(db_path)
    await original.init()
    samples = [
        ("我的生日是3月15日", "", "fact"),
        ("情绪触发：听到雷声会焦虑", "焦虑", "affect"),
        ("我答应以后称呼你为老师，这是禁忌", "", "relation"),
        ("以后请记住规则：不要在晚上提醒我", "", "instruction"),
        ("今天去了公园散步", "", "event"),
    ]
    inserted_ids = []
    for summary, emotion, _expected in samples:
        inserted_ids.append(
            await original.memory.insert_episodic_memory(
                summary, emotion_label=emotion, is_raw=1
            )
        )
    inherited_id = await original.memory.insert_episodic_memory(
        "提炼后的生日知识", is_raw=0
    )
    await original.memory.update_memory_enrichment(
        inherited_id,
        metadata_json=json.dumps({"source_raw_ids": [inserted_ids[0]]}),
    )
    await original.memory.insert_episodic_memory(
        "孤立知识声称生日是未知日期", is_raw=0
    )
    await original.close()

    with sqlite3.connect(db_path) as conn:
        for index in (
            "idx_episodic_classification_pending",
            "idx_episodic_memory_type",
        ):
            conn.execute(f"DROP INDEX IF EXISTS {index}")
        for column in (
            "classified_at",
            "classification_version",
            "classification_status",
            "memory_type",
        ):
            conn.execute(f"ALTER TABLE episodic_memories DROP COLUMN {column}")
        conn.execute("DELETE FROM schema_version WHERE version = 31")
        conn.commit()

    upgraded = DatabaseManager(db_path)
    await upgraded.init()
    rows = await upgraded.fetch_all(
        "SELECT summary, memory_type, classification_status "
        "FROM episodic_memories ORDER BY id"
    )
    assert [(row["summary"], row["memory_type"]) for row in rows] == [
        (summary, expected) for summary, _emotion, expected in samples
    ] + [
        ("提炼后的生日知识", "fact"),
        ("孤立知识声称生日是未知日期", "event"),
    ]
    assert {row["classification_status"] for row in rows} == {"backfilled"}
    await upgraded._migrate_v31()
    await upgraded.commit()
    assert await upgraded.fetch_one(
        "SELECT COUNT(*) AS count FROM episodic_memories"
    ) == {"count": 7}
    assert await upgraded.fetch_one(
        "SELECT COUNT(*) AS count FROM schema_version WHERE version=31"
    ) == {"count": 1}
    await upgraded.close()


@pytest.mark.asyncio
async def test_v31_partial_column_recovery_reclassifies_existing_rows(tmp_path):
    db_path = tmp_path / "partial-v31.db"
    original = DatabaseManager(db_path)
    await original.init()
    raw_id = await original.memory.insert_episodic_memory(
        "我的生日是3月15日", is_raw=1
    )
    await original.memory.update_memory_classification(
        raw_id,
        memory_type="fact",
        importance=0.8,
        classification_status="classified",
        classification_version=1,
        classified_at=123.0,
        phase="permanent",
        stability=S_PERMANENT,
        reinforcement_count=1,
    )
    await original.close()

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        columns = [
            row[1]
            for row in conn.execute("PRAGMA table_info(episodic_memories)")
            if row[1] != "memory_type"
        ]
        projection = ", ".join(f'"{column}"' for column in columns)
        conn.execute(
            "ALTER TABLE episodic_memories RENAME TO episodic_memories_partial"
        )
        conn.execute(
            f"CREATE TABLE episodic_memories AS SELECT {projection} "
            "FROM episodic_memories_partial"
        )
        conn.execute("DROP TABLE episodic_memories_partial")
        conn.commit()

    recovered = DatabaseManager(db_path)
    await recovered.init()
    row = await recovered.memory.get_memory_by_id(raw_id)
    assert row["memory_type"] == "fact"
    assert row["classification_status"] == "backfilled"
    assert row["classification_version"] == 1
    assert await recovered.fetch_one(
        "SELECT MAX(version) AS version FROM schema_version"
    ) == {"version": CURRENT_SCHEMA_VERSION}
    await recovered.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_columns",
    [
        {"distill_status", "memory_type"},
        {"phase", "memory_type"},
    ],
)
async def test_v31_combined_capability_recovery_resets_false_classification(
    tmp_path, missing_columns
):
    db_path = tmp_path / ("combined-" + "-".join(sorted(missing_columns)) + ".db")
    original = DatabaseManager(db_path)
    await original.init()
    raw_id = await original.memory.insert_episodic_memory(
        "我的生日是3月15日", is_raw=1
    )
    await original.memory.update_memory_classification(
        raw_id,
        memory_type="fact",
        importance=0.8,
        classification_status="classified",
        classification_version=1,
        classified_at=123.0,
        phase="permanent",
        stability=S_PERMANENT,
        reinforcement_count=1,
    )
    await original.close()

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP INDEX IF EXISTS idx_epi_phase")
        conn.execute("DROP INDEX IF EXISTS idx_episodic_memory_type")
        conn.execute("DROP INDEX IF EXISTS idx_episodic_classification_pending")
        for column in missing_columns:
            conn.execute(f"ALTER TABLE episodic_memories DROP COLUMN {column}")
        conn.commit()

    recovered = DatabaseManager(db_path)
    await recovered.init()
    row = await recovered.memory.get_memory_by_id(raw_id)
    assert row["memory_type"] == "fact"
    assert row["classification_status"] == "backfilled"
    assert row["classification_version"] == 1
    assert await recovered.fetch_one(
        "SELECT MAX(version) AS version FROM schema_version"
    ) == {"version": CURRENT_SCHEMA_VERSION}
    await recovered.close()


@pytest.mark.asyncio
async def test_pending_query_is_scoped_and_capped_at_50(memory_env):
    db, _mgr = memory_env
    alice = Scope(user_id="alice", agent_id="agent", session_id="s")
    bob = Scope(user_id="bob", agent_id="agent", session_id="s")
    for i in range(55):
        await db.memory.insert_episodic_memory(f"alice-{i}", scope=alice, is_raw=1)
    await db.memory.insert_episodic_memory("bob", scope=bob, is_raw=1)

    rows = await db.memory.get_pending_memory_classifications(scope=alice, limit=999)
    assert len(rows) == 50
    assert {row["user_id"] for row in rows} == {"alice"}
    assert [row["id"] for row in rows] == sorted(row["id"] for row in rows)


@pytest.mark.asyncio
async def test_switch_off_preserves_legacy_enrichment_without_classification_update(memory_env):
    db, mgr = memory_env
    raw_id = await db.memory.insert_episodic_memory(
        "raw remains", importance=0.8, is_raw=1
    )
    mgr.distiller = MagicMock()
    mgr.distiller._call_free_model = AsyncMock(
        return_value=json.dumps(
            {
                "memory_type": "fact",
                "importance": 0.9,
                "entities": ["Python"],
                "event_type": "学习",
                "metadata": {"topic": "Python"},
            }
        )
    )

    with patch("config.MEMORY_TYPE_ENRICHMENT_ENABLED", False):
        await mgr._enrich_memory_async(
            raw_id, [{"role": "user", "content": "learn Python"}]
        )

    row = await db.memory.get_memory_by_id(raw_id)
    assert row["entities"] == '["Python"]'
    assert row["classification_status"] == "pending"
    assert row["memory_type"] == "event"
    assert row["importance"] == 0.8


@pytest.mark.asyncio
async def test_raw_is_committed_before_enrichment_llm_starts(memory_env):
    db, mgr = memory_env
    seen = asyncio.Event()

    async def observe_raw(*_args, **_kwargs):
        row = await db.fetch_one(
            "SELECT is_raw, classification_status FROM episodic_memories "
            "ORDER BY id DESC LIMIT 1"
        )
        assert row == {"is_raw": 1, "classification_status": "pending"}
        seen.set()
        return '{"memory_type":"event","importance":0.4}'

    mgr._generate_summary = MagicMock(return_value="raw first")
    mgr._estimate_importance = MagicMock(return_value=0.4)
    mgr.distiller = MagicMock()
    mgr.distiller._call_free_model = observe_raw
    mgr._distill_to_knowledge = AsyncMock()
    context = {
        "exchanges": [
            {"role": "user", "content": "raw first please"},
            {"role": "assistant", "content": "stored"},
        ]
    }

    with patch("config.MEMORY_TYPE_ENRICHMENT_ENABLED", True):
        await mgr.encode_memory(context)
        await asyncio.wait_for(seen.wait(), timeout=1)


@pytest.mark.asyncio
async def test_llm_failure_keeps_raw_and_marks_fallback(memory_env):
    db, mgr = memory_env
    raw_id = await db.memory.insert_episodic_memory(
        "keep this raw", importance=0.7, is_raw=1
    )
    mgr.distiller = MagicMock()
    mgr.distiller._call_free_model = AsyncMock(
        side_effect=RuntimeError("provider down")
    )

    with patch("config.MEMORY_TYPE_ENRICHMENT_ENABLED", True):
        await mgr._enrich_memory_async(
            raw_id, [{"role": "user", "content": "keep this raw"}]
        )

    row = await db.memory.get_memory_by_id(raw_id)
    assert row["summary"] == "keep this raw"
    assert row["memory_type"] == "event"
    assert row["classification_status"] == "fallback"
    assert row["importance"] == 0.7


@pytest.mark.asyncio
async def test_cancellation_keeps_raw_pending_and_propagates(memory_env):
    db, mgr = memory_env
    raw_id = await db.memory.insert_episodic_memory("cancel-safe raw", is_raw=1)
    mgr.distiller = MagicMock()
    mgr.distiller._call_free_model = AsyncMock(side_effect=asyncio.CancelledError)

    with patch("config.MEMORY_TYPE_ENRICHMENT_ENABLED", True):
        with pytest.raises(asyncio.CancelledError):
            await mgr._enrich_memory_async(
                raw_id, [{"role": "user", "content": "cancel me"}]
            )

    row = await db.memory.get_memory_by_id(raw_id)
    assert row["summary"] == "cancel-safe raw"
    assert row["classification_status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("memory_type", "expected_importance", "phase", "minimum_stability"),
    [
        ("fact", 0.5, "permanent", S_PERMANENT),
        ("affect", 0.6, "reinforced", 3.0),
        ("relation", 0.6, "reinforced", 3.0),
    ],
)
async def test_classification_applies_type_specific_state(
    memory_env, memory_type, expected_importance, phase, minimum_stability
):
    db, mgr = memory_env
    raw_id = await db.memory.insert_episodic_memory(
        "typed raw", importance=0.5, is_raw=1
    )
    mgr.distiller = MagicMock()
    mgr.distiller._call_free_model = AsyncMock(
        return_value=json.dumps(
            {"memory_type": memory_type, "importance": 0.4}
        )
    )

    with patch("config.MEMORY_TYPE_ENRICHMENT_ENABLED", True):
        await mgr._enrich_memory_async(
            raw_id, [{"role": "user", "content": "typed raw"}]
        )

    row = await db.memory.get_memory_by_id(raw_id)
    assert row["memory_type"] == memory_type
    assert row["importance"] == expected_importance
    assert row["phase"] == phase
    assert row["stability"] >= minimum_stability
    assert row["reinforcement_count"] >= 1


@pytest.mark.asyncio
async def test_llm_importance_cannot_lower_rule_score(memory_env):
    db, mgr = memory_env
    raw_id = await db.memory.insert_episodic_memory(
        "important raw", importance=0.9, is_raw=1
    )
    mgr.distiller = MagicMock()
    mgr.distiller._call_free_model = AsyncMock(
        return_value='{"memory_type":"event","importance":0.1}'
    )
    with patch("config.MEMORY_TYPE_ENRICHMENT_ENABLED", True):
        await mgr._enrich_memory_async(
            raw_id, [{"role": "user", "content": "important raw"}]
        )
    assert (await db.memory.get_memory_by_id(raw_id))["importance"] == 0.9


@pytest.mark.asyncio
async def test_encode_switch_off_schedules_distill_without_waiting_for_enrichment(memory_env):
    _db, mgr = memory_env
    enrichment_release = asyncio.Event()
    enrichment_started = asyncio.Event()
    distill_started = asyncio.Event()

    async def slow_enrichment(*_args, **_kwargs):
        enrichment_started.set()
        await enrichment_release.wait()

    async def distill(*_args, **_kwargs):
        distill_started.set()

    mgr._generate_summary = MagicMock(return_value="legacy scheduling")
    mgr._estimate_importance = MagicMock(return_value=0.5)
    mgr._enrich_memory_async = slow_enrichment
    mgr._distill_to_knowledge = distill
    mgr.distiller = MagicMock()

    with patch("config.MEMORY_TYPE_ENRICHMENT_ENABLED", False):
        await mgr.encode_memory({"exchanges": [
            {"role": "user", "content": "legacy scheduling"},
            {"role": "assistant", "content": "stored"},
        ]})
        await asyncio.wait_for(enrichment_started.wait(), timeout=1)
        await asyncio.wait_for(distill_started.wait(), timeout=1)

    enrichment_release.set()


@pytest.mark.asyncio
async def test_encode_switch_on_serializes_enrichment_before_distill(memory_env):
    _db, mgr = memory_env
    enrichment_release = asyncio.Event()
    enrichment_started = asyncio.Event()
    distill_started = asyncio.Event()

    async def slow_enrichment(*_args, **_kwargs):
        enrichment_started.set()
        await enrichment_release.wait()

    async def distill(*_args, **_kwargs):
        distill_started.set()

    mgr._generate_summary = MagicMock(return_value="serial scheduling")
    mgr._estimate_importance = MagicMock(return_value=0.5)
    mgr._enrich_memory_async = slow_enrichment
    mgr._distill_to_knowledge = distill
    mgr.distiller = MagicMock()

    with patch("config.MEMORY_TYPE_ENRICHMENT_ENABLED", True):
        await mgr.encode_memory({"exchanges": [
            {"role": "user", "content": "serial scheduling"},
            {"role": "assistant", "content": "stored"},
        ]})
        await asyncio.wait_for(enrichment_started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert distill_started.is_set() is False
        enrichment_release.set()
        await asyncio.wait_for(distill_started.wait(), timeout=1)


@pytest.mark.asyncio
async def test_instruction_pipeline_only_classifies_episodic_then_distills(memory_env):
    db, mgr = memory_env
    raw_id = await db.memory.insert_episodic_memory(
        "以后请记住这条规则", importance=0.5, is_raw=1
    )
    mgr.distiller = MagicMock()
    mgr.distiller._call_free_model = AsyncMock(
        return_value='{"memory_type":"instruction","importance":0.7}'
    )
    mgr._distill_to_knowledge = AsyncMock()

    with patch("config.MEMORY_TYPE_ENRICHMENT_ENABLED", True):
        await mgr._run_enrichment_pipeline(
            raw_id,
            "以后请记住这条规则",
            Scope(),
            0.5,
            "",
            [{"role": "user", "content": "以后请记住这条规则"}],
        )

    row = await db.memory.get_memory_by_id(raw_id)
    assert row["memory_type"] == "instruction"
    assert row["importance"] == 0.7
    assert row["phase"] == "buffer"
    mgr._distill_to_knowledge.assert_awaited_once()


@pytest.mark.asyncio
async def test_pipeline_distills_after_enrichment_and_inherits_fields(memory_env):
    db, mgr = memory_env
    scope = Scope()
    raw_id = await db.memory.insert_episodic_memory(
        "source raw", importance=0.5, scope=scope, is_raw=1
    )
    order: list[str] = []

    async def enrich(memory_id, _exchanges):
        order.append("enrich")
        await db.memory.update_memory_classification(
            memory_id,
            memory_type="affect",
            importance=0.8,
            classification_status="classified",
            classification_version=1,
            classified_at=123.0,
            phase="reinforced",
            stability=4.0,
            reinforcement_count=1,
        )

    async def distill(*args, **kwargs):
        order.append("distill")
        return await mgr._encoder._distill_to_knowledge(*args, **kwargs)

    mgr._enrich_memory_async = enrich
    mgr._distill_to_knowledge = distill
    mgr.distiller = MagicMock()
    mgr.distiller.distill = AsyncMock(return_value="distilled knowledge")
    mgr._find_similar_knowledge = AsyncMock(return_value=None)

    with patch("config.MEMORY_TYPE_ENRICHMENT_ENABLED", True):
        await mgr._run_enrichment_pipeline(
            raw_id,
            "source raw",
            scope,
            0.5,
            "",
            [{"role": "user", "content": "source raw"}],
        )

    assert order == ["enrich", "distill"]
    knowledge = await db.fetch_one(
        "SELECT memory_type, importance, phase, reinforcement_count "
        "FROM episodic_memories WHERE is_raw=0 ORDER BY id DESC LIMIT 1"
    )
    assert knowledge == {
        "memory_type": "affect",
        "importance": 0.8,
        "phase": "reinforced",
        "reinforcement_count": 1,
    }


@pytest.mark.asyncio
async def test_pipeline_cancellation_does_not_continue_to_distill(memory_env):
    _db, mgr = memory_env
    mgr._enrich_memory_async = AsyncMock(side_effect=asyncio.CancelledError)
    mgr._distill_to_knowledge = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await mgr._run_enrichment_pipeline(1, "raw", Scope(), 0.5, "", [])
    mgr._distill_to_knowledge.assert_not_awaited()
