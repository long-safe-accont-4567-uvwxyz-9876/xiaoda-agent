"""KG v2 向量存储 + 混合检索测试。"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from db.database import DatabaseManager
from db.db_kg_v2 import KnowledgeDBV2


@pytest.fixture
def mock_vec_store():
    """创建带 mock embed 的 VectorStore。"""
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite_vec not available")
    from memory.vector_store import VectorStore
    import tempfile
    import os

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = VectorStore(path, embed_api_key="fake-key")
    return store, path


@pytest.mark.asyncio
async def test_kg_vec_tables_created(mock_vec_store):
    store, path = mock_vec_store
    try:
        await store.init()
        # Verify tables exist
        import sqlite3
        conn = sqlite3.connect(path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "kg_entities_vec" in tables
        assert "kg_relations_vec" in tables
    finally:
        await store.close()
        import os
        os.unlink(path)


@pytest.mark.asyncio
async def test_upsert_and_search_kg_entity(mock_vec_store):
    store, path = mock_vec_store
    try:
        await store.init()
        # Mock embed to return deterministic 1024-dim vector
        store._cache.clear() if hasattr(store._cache, 'clear') else None
        store.embed = AsyncMock(return_value=[0.1] * 1024)

        ok = await store.upsert_kg_entity(1, "篮球: 团队运动")
        assert ok is True

        results = await store.search_kg_entities("团队运动", top_k=5)
        assert len(results) >= 1
        assert results[0][0] == 1  # rowid
    finally:
        await store.close()
        import os
        os.unlink(path)


@pytest.mark.asyncio
async def test_upsert_and_search_kg_relation(mock_vec_store):
    store, path = mock_vec_store
    try:
        await store.init()
        store.embed = AsyncMock(return_value=[0.2] * 1024)

        ok = await store.upsert_kg_relation(1, "用户喜欢打篮球")
        assert ok is True

        results = await store.search_kg_relations("篮球", top_k=5)
        assert len(results) >= 1
        assert results[0][0] == 1  # rowid
    finally:
        await store.close()
        import os
        os.unlink(path)
