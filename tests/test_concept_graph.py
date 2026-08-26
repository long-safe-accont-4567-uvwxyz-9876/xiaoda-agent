"""概念图管理器单元测试"""

import aiosqlite
import pytest

from db.db_concept import ConceptDB
from memory.concept_graph import ConceptGraph
from memory.key_extractor import KeyExtractor


@pytest.fixture
async def graph():
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
        CREATE TABLE IF NOT EXISTS concept_meta (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
    """)
    await conn.commit()
    cdb = ConceptDB(conn)
    ke = KeyExtractor()
    g = ConceptGraph(cdb, ke)
    yield g
    await conn.close()


@pytest.mark.asyncio
async def test_remember_creates_node(graph):
    node_id = await graph.remember("Redis 是内存数据库，用于缓存")
    assert node_id is not None
    assert len(node_id) == 12  # md5[:12]
    node = await graph.get_node(node_id)
    assert node is not None
    assert "Redis" in node["text"] or "redis" in node["text"].lower()


@pytest.mark.asyncio
async def test_remember_same_text_same_id(graph):
    id1 = await graph.remember("Python 编程语言")
    id2 = await graph.remember("Python 编程语言")
    assert id1 == id2  # 相同文本生成相同 ID


@pytest.mark.asyncio
async def test_remember_with_source_mem_id(graph):
    node_id = await graph.remember("测试记忆", source_mem_id=123)
    node = await graph.get_node(node_id)
    assert node["source_mem_id"] == 123


@pytest.mark.asyncio
async def test_remember_auto_links_shared_keys(graph):
    # 节点 A: 4 个 keys
    await graph.remember("Python web 开发框架 FastAPI 性能")
    # 节点 B: 共享 python/web/开发 3 个 keys
    node_b_id = await graph.remember("Python web 开发最佳实践")
    edges = await graph.get_edges(node_b_id)
    # 应有至少一条边到节点 A
    assert len(edges) >= 1


@pytest.mark.asyncio
async def test_remember_no_auto_link_below_threshold(graph):
    # 节点 A
    await graph.remember("Python 数据分析 pandas numpy")
    # 节点 B: 只共享 python 1 个 key
    node_b_id = await graph.remember("Python 测试 pytest")
    edges = await graph.get_edges(node_b_id)
    # 不应建边（共享 < 3）
    assert len(edges) == 0


@pytest.mark.asyncio
async def test_lazy_migrate(graph):
    episodic = [
        {"id": 1, "summary": "Redis 缓存配置"},
        {"id": 2, "summary": "PostgreSQL 数据库优化"},
        {"id": 3, "summary": "Python 异步编程 asyncio"},
    ]
    count = await graph.lazy_migrate(episodic, limit=50)
    assert count == 3
    # 验证节点已创建
    for mem in episodic:
        node = await graph.get_node_by_source_mem(mem["id"])
        assert node is not None


@pytest.mark.asyncio
async def test_lazy_migrate_skips_existing(graph):
    await graph.remember("已有记忆", source_mem_id=1)
    episodic = [
        {"id": 1, "summary": "已有记忆"},  # 已存在
        {"id": 2, "summary": "新记忆"},
    ]
    count = await graph.lazy_migrate(episodic, limit=50)
    assert count == 1  # 只迁移新的


def test_clean_text(graph):
    assert graph._clean_text("  Hello  World  ") == "Hello  World"
    assert graph._clean_text("\n\nText\n") == "Text"


def test_make_node_id(graph):
    id1 = graph._make_node_id("test")
    id2 = graph._make_node_id("test")
    id3 = graph._make_node_id("different")
    assert id1 == id2
    assert id1 != id3
    assert len(id1) == 12
