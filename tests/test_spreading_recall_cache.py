"""G13: 扩散激活 recall 缓存测试

验证 SpreadingActivationEngine.recall 的 LRU+TTL 缓存:
- 相同 (query, top_k) 命中缓存
- 不同 query 或 top_k 不命中
- TTL 过期后重新计算
- clear_cache 后重新计算
- 返回副本，修改不影响缓存
"""
import json
import time
from unittest.mock import patch

import aiosqlite
import pytest

from db.db_concept import ConceptDB
from memory.key_extractor import KeyExtractor
from memory.spreading_activation import SpreadingActivationEngine


@pytest.fixture
async def engine():
    """构建带缓存功能的 SpreadingActivationEngine，预置 1 条命中数据"""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript("""
        CREATE TABLE IF NOT EXISTS concept_nodes (
            id TEXT PRIMARY KEY, text TEXT NOT NULL,
            weight REAL DEFAULT 1.0, peak_weight REAL DEFAULT 1.0,
            confidence REAL DEFAULT 1.0, access_count INTEGER DEFAULT 0,
            keys TEXT DEFAULT '[]', layer TEXT DEFAULT 'hippocampus',
            created TEXT NOT NULL, last_accessed TEXT NOT NULL,
            valid_from TEXT NOT NULL, valid_to TEXT, superseded_by TEXT,
            history TEXT DEFAULT '[]', origin TEXT DEFAULT '{}',
            source_mem_id INTEGER, embedding BLOB,
            difficulty REAL DEFAULT 5.0, stability REAL DEFAULT 3.0,
            phase TEXT DEFAULT 'buffer', last_review REAL DEFAULT 0.0,
            reinforcement_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS concept_edges (
            source_id TEXT NOT NULL, target_id TEXT NOT NULL,
            relation TEXT DEFAULT 'related', weight REAL DEFAULT 1.0,
            created TEXT NOT NULL, PRIMARY KEY (source_id, target_id)
        );
    """)
    await conn.commit()
    cdb = ConceptDB(conn)
    ke = KeyExtractor()
    eng = SpreadingActivationEngine(cdb, vector_store=None, key_extractor=ke)
    now = "2026-07-10T12:00:00+08:00"
    await eng.db.insert_node(
        id="node1", text="Redis 是内存数据库",
        keys=json.dumps(["redis", "内存", "数据库"]), created=now,
        last_accessed=now, valid_from=now,
    )
    yield eng
    await conn.close()


@pytest.mark.asyncio
async def test_recall_cache_hit(engine):
    """相同 query+top_k 第二次应命中缓存（_compute_idf 只调一次）"""
    with patch.object(engine, "_compute_idf", wraps=engine._compute_idf) as spy:
        r1 = await engine.recall("Redis 数据库", top_k=5)
        r2 = await engine.recall("Redis 数据库", top_k=5)
    assert r1 == r2
    assert spy.call_count == 1, f"期望 1 次，实际 {spy.call_count} 次"


@pytest.mark.asyncio
async def test_recall_cache_miss_different_query(engine):
    """不同 query 不命中缓存"""
    with patch.object(engine, "_compute_idf", wraps=engine._compute_idf) as spy:
        await engine.recall("Redis 数据库", top_k=5)
        await engine.recall("数据库 内存", top_k=5)
    assert spy.call_count == 2, f"期望 2 次，实际 {spy.call_count} 次"


@pytest.mark.asyncio
async def test_recall_cache_miss_different_top_k(engine):
    """不同 top_k 不命中缓存"""
    with patch.object(engine, "_compute_idf", wraps=engine._compute_idf) as spy:
        await engine.recall("Redis 数据库", top_k=5)
        await engine.recall("Redis 数据库", top_k=10)
    assert spy.call_count == 2, f"期望 2 次，实际 {spy.call_count} 次"


@pytest.mark.asyncio
async def test_recall_cache_ttl_expiry(engine):
    """TTL 过期后重新计算"""
    with patch.object(engine, "_compute_idf", wraps=engine._compute_idf) as spy:
        await engine.recall("Redis 数据库", top_k=5)
        assert spy.call_count == 1
        # 直接将缓存条目设为过期（模拟 TTL 到期）
        for k in list(engine._recall_cache.keys()):
            expiry, cached = engine._recall_cache[k]
            engine._recall_cache[k] = (time.monotonic() - 1, cached)
        await engine.recall("Redis 数据库", top_k=5)
    assert spy.call_count == 2, f"过期后期望 2 次，实际 {spy.call_count} 次"


@pytest.mark.asyncio
async def test_recall_cache_clear(engine):
    """clear_cache 后重新计算"""
    with patch.object(engine, "_compute_idf", wraps=engine._compute_idf) as spy:
        await engine.recall("Redis 数据库", top_k=5)
        assert spy.call_count == 1
        engine.clear_cache()
        await engine.recall("Redis 数据库", top_k=5)
    assert spy.call_count == 2, f"clear_cache 后期望 2 次，实际 {spy.call_count} 次"


@pytest.mark.asyncio
async def test_recall_cache_returns_copy(engine):
    """返回的是副本，修改不影响缓存"""
    r1 = await engine.recall("Redis 数据库", top_k=5)
    assert len(r1) >= 1
    orig_text = r1[0]["text"]
    orig_score = r1[0]["score"]
    orig_len = len(r1)

    # 篡改返回值
    r1[0]["text"] = "MODIFIED"
    r1[0]["score"] = 999.0
    r1.append({"id": "fake", "text": "injected", "score": 0,
               "weight": 0, "keys": "[]"})

    # 第二次调用应返回未受污染的结果
    r2 = await engine.recall("Redis 数据库", top_k=5)
    assert r2[0]["text"] == orig_text
    assert r2[0]["score"] == orig_score
    assert len(r2) == orig_len


@pytest.mark.asyncio
async def test_lazy_migrate_clears_recall_cache():
    """G13 fix: lazy_migrate 写入 concept_nodes 后应清空 recall 缓存。

    复现路径：retrieve_memories_hybrid 内的 lazy_migrate 块。
    断言：concept_graph.lazy_migrate 被调用后，spreading_engine.clear_cache 也被调用。
    """
    from unittest.mock import MagicMock, AsyncMock, patch

    with patch("memory.memory_manager.MemoryDistiller"), \
         patch("memory.memory_manager.QueryCache") as MockQC, \
         patch("memory.memory_manager.RetrievalAssessor"), \
         patch("memory.memory_manager.FSRSModel"), \
         patch("memory.memory_manager.get_agent_display_name", return_value="小妲"):
        MockQC.return_value = MagicMock()

        from memory.memory_manager import MemoryManager

        mm = MemoryManager(db=MagicMock(), memory=MagicMock())

        # 配置 concept_graph / spreading_engine mock
        mm.concept_graph = MagicMock()
        mm.concept_graph.lazy_migrate = AsyncMock(return_value=1)
        mm.spreading_engine = MagicMock()
        mm.spreading_engine.clear_cache = MagicMock()
        mm.spreading_engine.db = MagicMock()
        mm.spreading_engine.db.get_node_count = AsyncMock(return_value=0)

        # 触发 lazy_migrate 的条件：episodic_count > node_count 且有 unmigrated
        mm.memory.get_episodic_count = AsyncMock(return_value=5)
        mm.memory.get_unmigrated_memories = AsyncMock(
            return_value=[{"id": 1, "summary": "测试记忆"}]
        )

        # 重置节流时间戳，确保本次进入 lazy_migrate 块
        mm._last_lazy_migrate_ts = 0

        # 跳过冷启动路径，直接走 hot tier（避开 is_cold 早返回）
        mm.get_memory_tier = AsyncMock(return_value="hot")

        # 所有检索通道返回空，触发 "all empty" fallback 早返回
        mm._hybrid_fts_search_scoped = AsyncMock(return_value=[])
        mm._hybrid_vec_search = AsyncMock(return_value=[])
        mm._spreading_recall = AsyncMock(return_value=[])
        mm._entity_recall = AsyncMock(return_value=[])
        mm._extract_deterministic_selectors = MagicMock(return_value={})
        mm._get_candidate_ids_by_selectors = AsyncMock(return_value=None)

        await mm.retrieve_memories_hybrid("测试查询", k=5)

        # 断言 lazy_migrate 确实被调用（前提条件）
        mm.concept_graph.lazy_migrate.assert_awaited_once(), \
            "lazy_migrate 应被调用（测试前置条件不满足）"
        # 核心断言：lazy_migrate 后必须清空 recall 缓存
        assert mm.spreading_engine.clear_cache.called, \
            "lazy_migrate 写入新 concept_nodes 后必须调用 spreading_engine.clear_cache()"
