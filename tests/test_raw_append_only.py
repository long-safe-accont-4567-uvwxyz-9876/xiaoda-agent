from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from db.database import DatabaseManager
from memory.memory_manager import MemoryManager
from memory.scope import Scope


@pytest.fixture
async def memory_env(tmp_path):
    db = DatabaseManager(tmp_path / "raw-append-only.db")
    await db.init()
    yield db
    await db.close()


def _raw_identity(row: dict) -> tuple[object, ...]:
    return (
        row["summary"],
        row["timestamp"],
        row["user_id"],
        row["agent_id"],
        row["session_id"],
    )


@pytest.mark.asyncio
async def test_raw_summary_updates_only_change_derived_fields(memory_env):
    db = memory_env
    scope = Scope(user_id="owner", agent_id="agent", session_id="private-session")
    raw_id = await db.memory.insert_episodic_memory(
        "真实原始摘要", emotion_label="neutral", scope=scope, is_raw=1
    )
    before = await db.memory.get_memory_by_id(raw_id)

    await db.memory.update_memory_summary(raw_id, "内部改写")
    await db.memory.update_fallback_raw(
        raw_id, "扩长后的对话全文", "sad", distill_status="failed"
    )

    after = await db.memory.get_memory_by_id(raw_id)
    assert _raw_identity(after) == _raw_identity(before)
    assert after["emotion_label"] == "sad"
    assert after["distill_status"] == "failed"


@pytest.mark.asyncio
async def test_knowledge_summary_remains_mutable(memory_env):
    db = memory_env
    knowledge_id = await db.memory.insert_episodic_memory("旧知识", is_raw=0)

    await db.memory.update_memory_summary(knowledge_id, "新知识")
    assert (await db.memory.get_memory_by_id(knowledge_id))["summary"] == "新知识"

    await db.memory.update_fallback_raw(
        knowledge_id, "再次更新的知识", "calm", distill_status="failed"
    )
    knowledge = await db.memory.get_memory_by_id(knowledge_id)
    assert knowledge["summary"] == "再次更新的知识"
    assert knowledge["emotion_label"] == "calm"
    assert knowledge["distill_status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["empty", "exception"])
async def test_distill_exhaustion_keeps_original_raw_summary(memory_env, failure_mode):
    db = memory_env
    scope = Scope(user_id="owner", agent_id="agent", session_id="private-session")
    raw_id = await db.memory.insert_episodic_memory(
        "首次写入的真实摘要", scope=scope, is_raw=1
    )
    manager = MemoryManager.__new__(MemoryManager)
    manager.db = db
    manager.memory = db.memory
    manager.vec = MagicMock()
    manager.vec.upsert = AsyncMock()
    manager.distiller = MagicMock()
    manager.distiller.distill = AsyncMock(
        return_value="" if failure_mode == "empty" else None,
        side_effect=(
            RuntimeError("provider failed") if failure_mode == "exception" else None
        ),
    )
    manager.invalidate_read_caches = MagicMock()

    await manager._distill_to_knowledge(
        raw_id,
        "首次写入的真实摘要",
        scope,
        _retry=2,
        full_text="你说了：完整但不得覆盖的原文；我回应：收到",
    )

    raw = await db.memory.get_memory_by_id(raw_id)
    assert raw["summary"] == "首次写入的真实摘要"
    assert raw["distill_status"] == "failed"
    manager.vec.upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_delete_archives_raw_but_deletes_knowledge(memory_env):
    db = memory_env
    scope = Scope(user_id="owner", agent_id="agent", session_id="private-session")
    raw_id = await db.memory.insert_episodic_memory("raw", scope=scope, is_raw=1)
    knowledge_id = await db.memory.insert_episodic_memory(
        "knowledge", scope=scope, is_raw=0
    )

    await db.memory.delete_memory(raw_id)
    await db.memory.delete_memory(knowledge_id)

    raw = await db.memory.get_memory_by_id(raw_id)
    assert raw is not None
    assert raw["status"] == "archived"
    assert raw["session_id"] == "private-session"
    assert await db.memory.get_memory_by_id(knowledge_id) is None


@pytest.mark.asyncio
async def test_batch_and_vector_delete_do_not_hard_delete_raw(memory_env):
    db = memory_env
    first = await db.memory.insert_episodic_memory("first raw", is_raw=1)
    second = await db.memory.insert_episodic_memory("second raw", is_raw=1)
    knowledge = await db.memory.insert_episodic_memory("knowledge", is_raw=0)
    vector_store = MagicMock()
    vector_store.delete = AsyncMock()

    await db.memory.delete_memory_with_vector(first, vector_store=vector_store)
    await db.memory.delete_memories_batch(
        [second, knowledge], vector_store=vector_store
    )

    assert (await db.memory.get_memory_by_id(first))["status"] == "archived"
    assert (await db.memory.get_memory_by_id(second))["status"] == "archived"
    assert await db.memory.get_memory_by_id(knowledge) is None


@pytest.mark.asyncio
async def test_maintenance_archive_preserves_raw_scope(memory_env):
    db = memory_env
    scope = Scope(user_id="owner", agent_id="agent", session_id="private-session")
    raw_ids = [
        await db.memory.insert_episodic_memory(f"raw-{index}", scope=scope, is_raw=1)
        for index in range(2)
    ]

    await db.memory.archive_memory(raw_ids[0])
    await db.memory.archive_memories_batch([raw_ids[1]])

    for raw_id in raw_ids:
        raw = await db.memory.get_memory_by_id(raw_id)
        assert raw["status"] == "archived"
        assert raw["session_id"] == "private-session"


@pytest.mark.asyncio
async def test_explicit_user_request_can_hard_delete_raw(memory_env):
    db = memory_env
    raw_id = await db.memory.insert_episodic_memory("forget me", is_raw=1)
    vector_store = MagicMock()
    vector_store.delete = AsyncMock()

    deleted = await db.memory.hard_delete_raw_for_user_request(
        raw_id, vector_store=vector_store
    )

    assert deleted is True
    assert await db.memory.get_memory_by_id(raw_id) is None
    vector_store.delete.assert_awaited_once_with(raw_id)
