from unittest.mock import AsyncMock

import pytest
from loguru import logger

from memory.memory_manager import MemoryManager
from memory.query_cache import QueryCache
from memory.scope import Scope, bind_scope, reset_scope


def make_manager(cached_result):
    manager = MemoryManager.__new__(MemoryManager)
    manager._query_cache = AsyncMock()
    manager._query_cache.get.return_value = cached_result
    return manager


async def test_retrieval_epoch_uses_database_memory_repository_when_needed():
    manager = MemoryManager.__new__(MemoryManager)
    repository = type("MemoryRepository", (), {})()
    repository.get_retrieval_epoch = AsyncMock(return_value=7)
    manager.db = type("Database", (), {"memory": repository})()

    epoch = await manager._retrieval._get_retrieval_epoch(
        Scope(user_id="alice", agent_id="xiaoda")
    )

    assert epoch == 7
    repository.get_retrieval_epoch.assert_awaited_once()


async def test_retrieval_epoch_ignores_non_async_mock_methods():
    manager = MemoryManager.__new__(MemoryManager)
    manager.memory = AsyncMock()

    assert await manager._retrieval._get_retrieval_epoch(Scope()) == 0


async def test_query_cache_semantic_matches_are_isolated_by_namespace():
    async def same_embedding(_text: str) -> list[float]:
        return [1.0, 0.0]

    cache = QueryCache(embed_func=same_embedding, threshold=0.8)
    await cache.put("alice::xiaoda", "我的偏好", [{"summary": "alice-memory"}])
    await cache.put("bob::xiaoda", "我的偏好", [{"summary": "bob-memory"}])

    assert await cache.get("alice::xiaoda", "我喜欢什么") == [
        {"summary": "alice-memory"}
    ]
    assert await cache.get("bob::xiaoda", "我喜欢什么") == [
        {"summary": "bob-memory"}
    ]
    assert await cache.get("carol::xiaoda", "我喜欢什么") is None


async def test_same_user_query_cache_is_isolated_between_private_and_groups():
    async def same_embedding(_text: str) -> list[float]:
        return [1.0, 0.0]

    cache = QueryCache(embed_func=same_embedding, threshold=0.8)
    private = Scope.personal(user_id="alice", session_id="private-1")
    group_a = Scope.group(user_id="alice", group_id="group-a")
    group_b = Scope.group(user_id="alice", group_id="group-b")

    await cache.put(private.cache_namespace(), "same query", [{"summary": "private"}])
    await cache.put(group_a.cache_namespace(), "same query", [{"summary": "group-a"}])
    await cache.put(group_b.cache_namespace(), "same query", [{"summary": "group-b"}])

    assert await cache.get(private.cache_namespace(), "equivalent query") == [
        {"summary": "private"}
    ]
    assert await cache.get(group_a.cache_namespace(), "equivalent query") == [
        {"summary": "group-a"}
    ]
    assert await cache.get(group_b.cache_namespace(), "equivalent query") == [
        {"summary": "group-b"}
    ]


async def test_retrieve_memories_derives_conversation_user_filter_from_scope():
    manager = make_manager([{"summary": "alice memory"}])

    await manager.retrieve_memories(
        "昨天发生了什么", scope=Scope(user_id="alice", agent_id="xiaoda")
    )

    namespace, query = manager._query_cache.get.await_args.args
    assert namespace == "alice::xiaoda::alice::epoch=0"
    assert query == "昨天发生了什么"


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
    namespace, query = manager._query_cache.get.await_args.args
    assert namespace == "alice::xiaoli::alice::epoch=0"
    assert query == "我的偏好"


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
    namespace, query = manager._query_cache.get.await_args.args
    assert namespace == "bob::xiaoke::bob::epoch=0"
    assert query == "我的偏好"


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
    assert [call.args for call in manager._query_cache.get.await_args_list] == [
        ("alice::xiaoda::alice::epoch=0", "同一问题"),
        ("bob::xiaoda::bob::epoch=0", "同一问题"),
    ]
    scope_logs = [
        record["extra"] for record in events
        if record["message"] == "memory.scope_resolved"
    ]
    assert [item["scope_user_id"] for item in scope_logs] == ["alice", "bob"]
    assert sum(r["message"] == "memory.cache_lookup" for r in events) == 2
    assert sum(r["message"] == "memory.cache_miss" for r in events) == 1
    assert sum(r["message"] == "memory.cache_hit" for r in events) == 1
