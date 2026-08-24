import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger

from memory.memory_manager import MemoryManager
from memory.query_cache import QueryCache
from memory.retrieval.pipeline import RetrievalEngine
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


async def test_visible_memory_id_pages_are_recent_first_and_continue(tmp_path):
    from db.database import DatabaseManager

    database = DatabaseManager(tmp_path / "scope-pages.db")
    await database.init()
    scope = Scope.group("alice", "group-a")
    ids = [
        await database.memory.insert_episodic_memory(
            summary=f"page evidence {index}", scope=scope
        )
        for index in range(5)
    ]
    first = await database.memory.get_visible_memory_id_page(
        scope, page_size=2
    )
    second = await database.memory.get_visible_memory_id_page(
        scope, before_id=first["next_cursor"], page_size=2
    )
    await database.close()

    assert first == {
        "ids": [ids[4], ids[3]],
        "next_cursor": ids[3],
        "has_more": True,
    }
    assert second["ids"] == [ids[2], ids[1]]
    assert second["has_more"] is True


async def test_vector_scope_pages_merge_top_n_and_include_new_ids(monkeypatch):
    records = {
        row_id: {
            "id": row_id,
            "summary": f"record {row_id}",
            "status": "active",
            "user_id": "alice",
            "agent_id": "xiaoda",
            "session_id": "qq_group:group-a",
        }
        for row_id in range(1, 6)
    }
    pages = {
        None: {"ids": [5, 4], "next_cursor": 4, "has_more": True},
        4: {"ids": [3, 2], "next_cursor": 2, "has_more": True},
        2: {"ids": [1], "next_cursor": 1, "has_more": False},
    }
    distances = {5: 0.2, 4: 0.9, 3: 0.1, 2: 0.8, 1: 0.7}

    class FakeVector:
        async def search(self, _query, top_k, candidate_ids=None, **_kwargs):
            return sorted(
                [(row_id, distances[row_id]) for row_id in candidate_ids],
                key=lambda item: item[1],
            )[:top_k]

    class FakeRepository:
        def __init__(self):
            self.cursors = []

        async def get_visible_memory_id_page(
            self, scope, before_id=None, page_size=2
        ):
            self.cursors.append(before_id)
            return pages[before_id]

        async def get_visible_memories_by_ids(self, ids, scope=None):
            return [records[row_id] for row_id in ids]

    repository = FakeRepository()
    manager = MagicMock()
    manager.vec = FakeVector()
    manager.memory = repository
    manager.db = None
    manager._query_transformer = None
    engine = RetrievalEngine(manager)
    monkeypatch.setattr("config.SCOPE_SCAN_PAGE_SIZE", 2, raising=False)
    monkeypatch.setattr("config.SCOPE_SCAN_MIN_PAGES", 2, raising=False)

    results = await engine._hybrid_vec_search(
        "same query", 2, scope=Scope.group("alice", "group-a"),
        query_vec=[1.0],
    )

    assert [row["id"] for row in results] == [3, 5]
    assert repository.cursors[:2] == [None, 4]


async def test_scope_page_budget_exposes_partial_degradation(monkeypatch):
    from memory.retrieval.trace import begin_retrieval_trace, read_retrieval_trace
    from utils.metrics import metrics

    class FakeVector:
        async def search(self, _query, top_k, candidate_ids=None, **_kwargs):
            return [(candidate_ids[0], 0.1)]

    class FakeRepository:
        async def get_visible_memory_id_page(
            self, scope, before_id=None, page_size=1
        ):
            return {"ids": [9], "next_cursor": 9, "has_more": True}

        async def get_visible_memories_by_ids(self, ids, scope=None):
            return [{
                "id": 9, "summary": "partial", "status": "active",
                "user_id": "alice", "agent_id": "xiaoda",
                "session_id": "qq_group:group-a",
            }]

    manager = MagicMock()
    manager.vec = FakeVector()
    manager.memory = FakeRepository()
    manager.db = None
    manager._query_transformer = None
    engine = RetrievalEngine(manager)
    monkeypatch.setattr("config.SCOPE_SCAN_PAGE_SIZE", 1, raising=False)
    monkeypatch.setattr("config.SCOPE_SCAN_MAX_PAGES", 1, raising=False)
    begin_retrieval_trace()
    before = metrics.get_snapshot()["counters"].get(
        "retrieval.scope_scan.partial", 0
    )

    await engine._hybrid_vec_search(
        "query", 2, scope=Scope.group("alice", "group-a"), query_vec=[1.0]
    )

    after = metrics.get_snapshot()["counters"].get(
        "retrieval.scope_scan.partial", 0
    )
    assert "scope_scan_partial" in read_retrieval_trace()
    assert after == before + 1


async def test_vector_candidates_are_prefiltered_so_other_scopes_cannot_fill_top_n():
    records = {
        1: {"id": 1, "summary": "private closer", "status": "active",
            "user_id": "alice", "agent_id": "xiaoda", "session_id": "private-1"},
        2: {"id": 2, "summary": "group b closer", "status": "active",
            "user_id": "alice", "agent_id": "xiaoda", "session_id": "qq_group:group-b"},
        3: {"id": 3, "summary": "group a allowed", "status": "active",
            "user_id": "alice", "agent_id": "xiaoda", "session_id": "qq_group:group-a"},
    }

    class FakeVector:
        async def search(self, _query, top_k, candidate_ids=None, **_kwargs):
            ranked = [(1, 0.01), (2, 0.02), (3, 0.40)]
            if candidate_ids is not None:
                allowed = set(candidate_ids)
                ranked = [hit for hit in ranked if hit[0] in allowed]
            return ranked[:top_k]

    class FakeRepository:
        async def get_visible_memory_ids(self, scope, limit):
            return [
                row_id for row_id, row in records.items()
                if scope.matches_record(row)
            ][:limit]

        async def get_visible_memories_by_ids(self, ids, scope=None):
            return [
                records[row_id] for row_id in ids
                if scope is None or scope.matches_record(records[row_id])
            ]

    manager = MagicMock()
    manager.vec = FakeVector()
    manager.memory = FakeRepository()
    manager.db = None
    manager._query_transformer = None
    engine = RetrievalEngine(manager)

    results = await engine._hybrid_vec_search(
        "same query", 1, scope=Scope.group("alice", "group-a"), query_vec=[1.0]
    )

    assert [row["id"] for row in results] == [3]


async def test_kg_v1_continues_bounded_candidates_until_scope_is_filled():
    scope = Scope.group("alice", "group-a")
    calls = []

    class FakeKg:
        async def recall_by_query(self, _query, limit):
            calls.append(limit)
            return [f"entity-{index}" for index in range(limit)]

    class FakeRepository:
        async def search_memories_by_entities_scoped(self, names, limit, scope):
            if len(names) < 4:
                return []
            return [{
                "id": 4,
                "summary": "group-a evidence",
                "user_id": "alice",
                "agent_id": "xiaoda",
                "session_id": "qq_group:group-a",
            }][:limit]

    manager = MagicMock()
    manager.kg = FakeKg()
    manager.memory = FakeRepository()
    engine = RetrievalEngine(manager)

    results = await engine._recall_kg("query", 1, scope, use_kg=True)

    assert [row["id"] for row in results] == [4]
    assert calls == [1, 2, 4]


async def test_kg_v2_group_drops_global_entities_before_fusion(monkeypatch):
    class FakeKgV2:
        def __init__(self):
            self.top_ks = []

        async def search(self, _query, top_k, scope):
            self.top_ks.append(top_k)
            rows = [
                {"type": "entity", "id": "global-1", "name": "Alice",
                 "summary": "private entity summary"},
                {"type": "entity", "id": "global-2", "name": "Bob",
                 "summary": "other group entity summary"},
                {"type": "relation", "id": "group-a", "fact": "group fact"},
            ]
            return rows[:top_k]

    manager = MagicMock()
    manager._kg_v2_engine = FakeKgV2()
    engine = RetrievalEngine(manager)
    monkeypatch.setattr("config.KG_V2_ENABLED", True)
    scope = Scope.group("alice", "group-a")

    results = await engine._recall_kg_v2("query", 1, scope)

    assert [row["id"] for row in results] == ["group-a"]
    assert manager._kg_v2_engine.top_ks == [1, 2, 4]
    assert results[0]["session_id"] == "qq_group:group-a"


async def test_real_sqlite_vector_scope_finds_farther_allowed_group_result(tmp_path):
    from db.database import DatabaseManager
    from memory.vector_store import VectorStore

    database = DatabaseManager(tmp_path / "memory.db")
    await database.init()
    vector = VectorStore(
        tmp_path / "vectors.db", embed_mode="remote", dimensions=2
    )
    await vector.init()
    try:
        scopes = (
            Scope.personal("alice", "private-1"),
            Scope.group("alice", "group-b"),
            Scope.group("alice", "group-a"),
        )
        vectors = ([1.0, 0.0], [0.99, 0.01], [0.0, 1.0])
        ids = []
        for index, (scope, embedding) in enumerate(
            zip(scopes, vectors, strict=True)
        ):
            memory_id = await database.memory.insert_episodic_memory(
                summary=f"vector evidence {index}", scope=scope
            )
            ids.append(memory_id)
            vector.embed = AsyncMock(return_value=[embedding])
            assert await vector.upsert(memory_id, f"vector evidence {index}")

        manager = MagicMock()
        manager.vec = vector
        manager.memory = database.memory
        manager.db = None
        manager._query_transformer = None
        engine = RetrievalEngine(manager)

        results = await engine._hybrid_vec_search(
            "same query",
            1,
            scope=scopes[2],
            query_vec=[1.0, 0.0],
        )

        assert [row["id"] for row in results] == [ids[2]]
        assert results[0]["session_id"] == "qq_group:group-a"
    finally:
        await vector.close()
        await database.close()


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


def _multi_recall_manager(**overrides):
    """七路召回的最小 manager 桩：默认全通道空结果，按需覆盖。"""
    manager = MagicMock()
    manager.kg = None
    manager.vec = None
    manager.db = None
    manager._query_transformer = None
    manager.memory = SimpleNamespace(search_child_fts=AsyncMock(return_value=[]))
    manager._hybrid_fts_search_scoped = AsyncMock(return_value=[])
    manager._hybrid_vec_search = AsyncMock(return_value=[])
    manager._spreading_recall = AsyncMock(return_value=[])
    manager._entity_recall = AsyncMock(return_value=[])
    for key, value in overrides.items():
        setattr(manager, key, value)
    return manager


class _FailingKgV2:
    async def search(self, *_args, **_kwargs):
        raise RuntimeError("kg v2 down")


async def test_capture_channel_trace_only_visible_after_explicit_merge():
    from memory.retrieval.trace import (
        begin_retrieval_trace,
        capture_channel_trace,
        mark_retrieval_degraded,
        merge_channel_outcomes,
        read_retrieval_trace,
    )

    async def channel():
        await asyncio.sleep(0)
        mark_retrieval_degraded("deep_component")
        return ["hit"]

    begin_retrieval_trace()
    outcome = (await asyncio.gather(capture_channel_trace(channel())))[0]

    assert outcome.result == ["hit"]
    assert outcome.degraded == ("deep_component",)
    # gather 子任务 contextvars 副本里的打点对父协程不可见，直到显式合并
    assert read_retrieval_trace() == ()
    merge_channel_outcomes([outcome])
    assert read_retrieval_trace() == ("deep_component",)


async def test_multi_recall_merges_kg_v2_degradation_from_gather_subtask(monkeypatch):
    from memory.retrieval.trace import begin_retrieval_trace, read_retrieval_trace

    manager = _multi_recall_manager(_kg_v2_engine=_FailingKgV2())
    monkeypatch.setattr("config.KG_V2_ENABLED", True, raising=False)
    engine = RetrievalEngine(manager)

    begin_retrieval_trace()
    channels = await engine._run_multi_recall(
        "query", 5, Scope.group("alice", "group-a"), None, None, None, True)

    assert not any(channels)
    # 回归（B1）：kg_v2 降级发生在 gather 子任务上下文里，父协程必须可见
    assert "kg_v2" in read_retrieval_trace()


async def test_multi_recall_merges_scope_scan_partial_from_vector_subtask(monkeypatch):
    from memory.retrieval.trace import begin_retrieval_trace, read_retrieval_trace

    class FakeVector:
        enabled = False

        async def search(self, _query, top_k, candidate_ids=None, **_kwargs):
            return [(candidate_ids[0], 0.1)]

    class FakeRepository:
        async def get_visible_memory_id_page(
            self, scope, before_id=None, page_size=1
        ):
            return {"ids": [9], "next_cursor": 9, "has_more": True}

        async def get_visible_memories_by_ids(self, ids, scope=None):
            return [{
                "id": 9, "summary": "partial", "status": "active",
                "user_id": "alice", "agent_id": "xiaoda",
                "session_id": "qq_group:group-a",
            }]

        async def search_child_fts(self, *_args, **_kwargs):
            return []

    manager = _multi_recall_manager(vec=FakeVector(), memory=FakeRepository())
    monkeypatch.setattr("config.SCOPE_SCAN_PAGE_SIZE", 1, raising=False)
    monkeypatch.setattr("config.SCOPE_SCAN_MAX_PAGES", 1, raising=False)
    engine = RetrievalEngine(manager)
    # 让七路召回里的 vec 槽位走真实通道实现（其余槽位保持桩）
    manager._hybrid_vec_search = engine._hybrid_vec_search

    begin_retrieval_trace()
    channels = await engine._run_multi_recall(
        "query", 5, Scope.group("alice", "group-a"), None, None, None, True)

    assert [row["id"] for row in channels.vec_items] == [9]
    # 回归（B1）：vec 通道扫描预算耗尽打在子任务里，父协程必须可见
    assert "scope_scan_partial" in read_retrieval_trace()


async def test_degradation_marks_do_not_leak_across_requests(monkeypatch):
    from memory.retrieval.trace import begin_retrieval_trace, read_retrieval_trace

    manager = _multi_recall_manager(_kg_v2_engine=_FailingKgV2())
    engine = RetrievalEngine(manager)

    # 请求 A：KG v2 故障，degraded 必须出现在本次 trace 里
    monkeypatch.setattr("config.KG_V2_ENABLED", True, raising=False)
    begin_retrieval_trace()
    await engine._run_multi_recall(
        "query", 5, Scope.group("alice", "group-a"), None, None, None, True)
    assert "kg_v2" in read_retrieval_trace()

    # 请求 B：全新 trace + 健康通道，不得串到请求 A 的 kg_v2 打点
    monkeypatch.setattr("config.KG_V2_ENABLED", False, raising=False)
    begin_retrieval_trace()
    await engine._run_multi_recall(
        "query", 5, Scope.group("alice", "group-a"), None, None, None, True)
    assert read_retrieval_trace() == ()
