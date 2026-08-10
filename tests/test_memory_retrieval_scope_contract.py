from unittest.mock import AsyncMock

import pytest
from loguru import logger

from memory.memory_manager import MemoryManager
from memory.scope import Scope, bind_scope, reset_scope


def make_manager(cached_result):
    manager = MemoryManager.__new__(MemoryManager)
    manager._query_cache = AsyncMock()
    manager._query_cache.get.return_value = cached_result
    return manager


async def test_retrieve_memories_requires_bound_or_explicit_scope():
    manager = make_manager([])

    with pytest.raises(RuntimeError, match="scope is not bound"):
        await manager.retrieve_memories("我的偏好")


async def test_retrieve_memories_uses_bound_scope_in_cache_namespace():
    manager = make_manager([{"summary": "alice memory"}])
    token = bind_scope(Scope(user_id="alice", agent_id="xiaoli"))
    try:
        result = await manager.retrieve_memories("我的偏好")
    finally:
        reset_scope(token)

    assert result == [{"summary": "alice memory"}]
    cache_key = manager._query_cache.get.await_args.args[0]
    assert cache_key == "alice::xiaoli::::我的偏好"


async def test_explicit_scope_takes_precedence_over_bound_scope():
    manager = make_manager([{"summary": "bob memory"}])
    token = bind_scope(Scope(user_id="alice", agent_id="xiaoda"))
    try:
        result = await manager.retrieve_memories(
            "我的偏好", scope=Scope(user_id="bob", agent_id="xiaoke")
        )
    finally:
        reset_scope(token)

    assert result == [{"summary": "bob memory"}]
    cache_key = manager._query_cache.get.await_args.args[0]
    assert cache_key == "bob::xiaoke::::我的偏好"


async def test_same_query_uses_isolated_scope_cache_and_logs_hit_miss(monkeypatch):
    manager = MemoryManager.__new__(MemoryManager)
    manager._query_cache = AsyncMock()
    manager._query_cache.get.side_effect = [None, [{"summary": "bob-memory"}]]
    manager._query_transformer = None
    manager._try_temporal_search = AsyncMock(return_value=None)
    manager._is_retrieval_simple = lambda _: True
    manager.retrieve_memories_hybrid = AsyncMock(return_value=[])
    manager._dedup_by_content_similarity = lambda value: value
    monkeypatch.setattr("config.QUERY_CACHE_ENABLED", True)
    events = []
    sink_id = logger.add(lambda message: events.append(message.record), level="DEBUG")
    try:
        alice_result = await manager.retrieve_memories(
            "同一问题", scope=Scope(user_id="alice", agent_id="xiaoda")
        )
        bob_result = await manager.retrieve_memories(
            "同一问题", scope=Scope(user_id="bob", agent_id="xiaoda")
        )
    finally:
        logger.remove(sink_id)

    assert alice_result == []
    assert bob_result == [{"summary": "bob-memory"}]
    assert [call.args[0] for call in manager._query_cache.get.await_args_list] == [
        "alice::xiaoda::::同一问题",
        "bob::xiaoda::::同一问题",
    ]
    scope_logs = [
        record["extra"] for record in events
        if record["message"] == "memory.scope_resolved"
    ]
    assert [item["scope_user_id"] for item in scope_logs] == ["alice", "bob"]
    assert sum(r["message"] == "memory.cache_lookup" for r in events) == 2
    assert sum(r["message"] == "memory.cache_miss" for r in events) == 1
    assert sum(r["message"] == "memory.cache_hit" for r in events) == 1
