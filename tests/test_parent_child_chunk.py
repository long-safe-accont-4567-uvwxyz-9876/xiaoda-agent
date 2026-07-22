"""父子Chunk RAG优化 + Contextual Retrieval 测试"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 确保项目路径
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(PROJECT_ROOT))


# ── 单元测试：_split_into_children ──────────────────────────

class TestSplitIntoChildren:
    """测试子chunk切分逻辑"""

    def _make_manager(self):
        """创建最小化的 MemoryManager 实例（仅测试 _split_into_children）"""
        from memory.memory_manager import MemoryManager
        mgr = MemoryManager.__new__(MemoryManager)
        return mgr

    def test_basic_split(self):
        """测试基本切分：8轮对话生成子chunk"""
        mgr = self._make_manager()
        exchanges = [
            {"role": "user", "content": "我想用React重写前端"},
            {"role": "assistant", "content": "好的，我来帮你规划React重写方案"},
            {"role": "user", "content": "需要哪些依赖？"},
            {"role": "assistant", "content": "需要安装react、react-dom等核心包"},
        ]
        parent_summary = "用户说: 我想用React重写前端；好的，我来帮你规划React重写方案"

        children = mgr._split_into_children(exchanges, parent_id=1,
                                             parent_summary=parent_summary)
        assert len(children) == 4
        assert all(c["chunk_type"] == "segment" for c in children)

    def test_contextual_retrieval_prefix(self):
        """测试 Contextual Retrieval 前缀注入"""
        mgr = self._make_manager()
        exchanges = [{"role": "user", "content": "测试内容"}]
        parent_summary = "这是一段父摘要"

        children = mgr._split_into_children(exchanges, parent_id=1,
                                             parent_summary=parent_summary)
        assert len(children) == 1
        # embed_content 应包含上下文前缀
        assert "[上下文:" in children[0]["embed_content"]
        assert parent_summary[:80] in children[0]["embed_content"]

    def test_overlap_window(self):
        """测试重叠窗口：第二个子chunk应包含第一个子chunk的尾部"""
        mgr = self._make_manager()
        long_content = "A" * 100 + "B" * 50  # 150字内容
        exchanges = [
            {"role": "user", "content": long_content},
            {"role": "assistant", "content": "C" * 100},
        ]

        children = mgr._split_into_children(exchanges, parent_id=1,
                                             parent_summary="摘要")
        assert len(children) == 2
        # 第二个子chunk应有 overlap_hash
        assert children[1]["overlap_hash"] != ""
        # 第二个子chunk的content应包含前一个的尾部
        first_tail = children[0]["content"][-30:]
        assert first_tail in children[1]["content"]

    def test_max_children_limit(self):
        """测试子chunk数量上限"""
        mgr = self._make_manager()
        # 生成20轮对话
        exchanges = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"消息{i}"}
            for i in range(20)
        ]

        children = mgr._split_into_children(exchanges, parent_id=1,
                                             parent_summary="摘要")
        # 默认上限10个，但只有20轮对话中最后8轮会被处理
        assert len(children) <= 10

    def test_user_weight_higher_than_assistant(self):
        """测试用户消息权重高于助手消息"""
        mgr = self._make_manager()
        exchanges = [
            {"role": "user", "content": "用户消息"},
            {"role": "assistant", "content": "助手消息"},
        ]

        children = mgr._split_into_children(exchanges, parent_id=1,
                                             parent_summary="摘要")
        assert children[0]["weight"] == 1.0  # user
        assert children[1]["weight"] == 0.8  # assistant

    def test_empty_exchanges(self):
        """测试空对话列表"""
        mgr = self._make_manager()
        children = mgr._split_into_children([], parent_id=1, parent_summary="摘要")
        assert children == []

    def test_skip_empty_content(self):
        """测试跳过空内容消息"""
        mgr = self._make_manager()
        exchanges = [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "有内容"},
        ]

        children = mgr._split_into_children(exchanges, parent_id=1,
                                             parent_summary="摘要")
        assert len(children) == 1  # 只有非空消息被处理


# ── 单元测试：DB层 child chunk CRUD ──────────────────────────

class TestChildChunkDB:
    """测试子chunk数据库操作"""

    @pytest.fixture
    async def memory_db(self, tmp_path):
        """创建临时内存数据库"""
        import aiosqlite

        from db.db_memory import MemoryDB

        db_path = str(tmp_path / "test_child.db")
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row

        # 创建表结构
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS episodic_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                summary TEXT NOT NULL,
                importance REAL DEFAULT 0.5,
                emotion_label TEXT DEFAULT '',
                session_id TEXT DEFAULT 'user',
                embedding_id INTEGER DEFAULT -1,
                rag_status TEXT DEFAULT 'pending',
                rag_synced_at REAL DEFAULT 0,
                doc_id TEXT DEFAULT '',
                source TEXT DEFAULT 'user',
                access_count INTEGER DEFAULT 0,
                distilled INTEGER DEFAULT 0,
                user_id TEXT DEFAULT 'default',
                agent_id TEXT DEFAULT 'xiaoda',
                is_raw INTEGER DEFAULT 0
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS episodic_memory_fts USING fts5(
                id UNINDEXED, summary_index
            );
            CREATE TABLE IF NOT EXISTS memory_child_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                embed_content TEXT DEFAULT '',
                chunk_type TEXT NOT NULL DEFAULT 'segment',
                importance REAL DEFAULT 0.5,
                overlap_hash TEXT DEFAULT '',
                created_at REAL NOT NULL,
                FOREIGN KEY (parent_id) REFERENCES episodic_memories(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_child_parent ON memory_child_chunks(parent_id);
            CREATE INDEX IF NOT EXISTS idx_child_type ON memory_child_chunks(chunk_type);
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_child_chunks_fts
                USING fts5(content, tokenize='unicode61');
        """)
        await conn.commit()

        mdb = MemoryDB(conn)
        return mdb, conn

    @pytest.mark.asyncio
    async def test_insert_and_get_child(self, memory_db):
        """测试插入子chunk并查询"""
        mdb, conn = memory_db
        # 先插入父记录
        parent_id = await mdb.insert_episodic_memory(
            summary="测试父摘要", importance=0.8)

        # 插入子chunk
        child_id = await mdb.insert_child_chunk(
            parent_id=parent_id,
            content="用户说：测试内容",
            embed_content="[上下文] 用户说：测试内容",
            chunk_type="segment",
            importance=0.8,
            overlap_hash="abc12345",
        )
        assert child_id > 0

        # 查询子chunk
        children = await mdb.get_children_by_parent(parent_id)
        assert len(children) == 1
        assert children[0]["content"] == "用户说：测试内容"
        assert children[0]["chunk_type"] == "segment"

    @pytest.mark.asyncio
    async def test_search_child_fts(self, memory_db):
        """测试子chunk FTS检索"""
        mdb, conn = memory_db
        parent_id = await mdb.insert_episodic_memory(
            summary="React frontend rewrite", importance=0.8)

        await mdb.insert_child_chunk(
            parent_id=parent_id, content="user said: React frontend rewrite plan",
            chunk_type="segment")
        await mdb.insert_child_chunk(
            parent_id=parent_id, content="assistant: need install react dependencies",
            chunk_type="segment")

        results = await mdb.search_child_fts("React", limit=10)
        assert len(results) >= 1
        assert all(r["parent_id"] == parent_id for r in results)

    @pytest.mark.asyncio
    async def test_get_child_parent_ids(self, memory_db):
        """测试子chunk→父chunk ID映射"""
        mdb, conn = memory_db
        pid1 = await mdb.insert_episodic_memory(summary="父1", importance=0.5)
        pid2 = await mdb.insert_episodic_memory(summary="父2", importance=0.5)

        cid1 = await mdb.insert_child_chunk(parent_id=pid1, content="子1")
        cid2 = await mdb.insert_child_chunk(parent_id=pid1, content="子2")
        cid3 = await mdb.insert_child_chunk(parent_id=pid2, content="子3")

        parent_ids = await mdb.get_child_parent_ids([cid1, cid2, cid3])
        assert set(parent_ids) == {pid1, pid2}

    @pytest.mark.asyncio
    async def test_delete_children_by_parent(self, memory_db):
        """测试删除父chunk的所有子chunk"""
        mdb, conn = memory_db
        pid = await mdb.insert_episodic_memory(summary="父", importance=0.5)

        await mdb.insert_child_chunk(parent_id=pid, content="子1")
        await mdb.insert_child_chunk(parent_id=pid, content="子2")
        await mdb.insert_child_chunk(parent_id=pid, content="子3")

        deleted = await mdb.delete_children_by_parent(pid)
        assert deleted == 3

        children = await mdb.get_children_by_parent(pid)
        assert len(children) == 0


# ── 单元测试：VectorStore 子chunk方法 ──────────────────────

class TestVectorStoreChild:
    """测试VectorStore子chunk方法（不需要真实API）"""

    def test_search_child_returns_empty_when_not_initialized(self):
        """测试未初始化时search_child返回空"""
        from memory.vector_store import VectorStore
        vs = VectorStore.__new__(VectorStore)
        vs._initialized = False
        vs._closed = False
        vs._vec_conn = None

        result = asyncio.run(vs.search_child([0.1, 0.2], top_k=5))
        assert result == []

    def test_upsert_child_skips_when_not_initialized(self):
        """测试未初始化时upsert_child跳过"""
        from memory.vector_store import VectorStore
        vs = VectorStore.__new__(VectorStore)
        vs._initialized = False
        vs._closed = False
        vs._vec_conn = None

        # 不应抛出异常
        asyncio.run(vs.upsert_child(1, "测试文本"))

    def test_batch_upsert_children_skips_empty(self):
        """测试空列表时batch_upsert_children跳过"""
        from memory.vector_store import VectorStore
        vs = VectorStore.__new__(VectorStore)
        vs._initialized = False
        vs._closed = False
        vs._vec_conn = None

        asyncio.run(vs.batch_upsert_children([]))


# ── 集成测试：encode_memory 生成子chunk ──────────────────────

class TestEncodeMemoryChildChunks:
    """测试encode_memory是否正确生成子chunk"""

    def _make_mock_manager(self):
        """创建带mock的MemoryManager"""
        from memory.memory_manager import MemoryManager
        mgr = MemoryManager.__new__(MemoryManager)

        mgr.memory = MagicMock()
        mgr.memory.insert_episodic_memory = AsyncMock(return_value=1)
        mgr.memory.insert_child_chunk = AsyncMock(return_value=100)
        mgr.memory.insert_consolidation_candidate = AsyncMock(return_value=1)
        mgr.memory.mark_candidate_applied = AsyncMock(return_value=None)
        mgr.memory.update_memory_enrichment = AsyncMock(return_value=None)

        mgr.vec = MagicMock()
        mgr.vec.upsert = AsyncMock(return_value=True)
        mgr.vec.batch_upsert_children = AsyncMock(return_value=None)
        mgr.vec.enabled = True

        mgr.kg = None
        mgr._governance = None
        mgr._security_filter = None
        mgr._last_encode_time = 0
        mgr._pending_encode = False
        mgr.distiller = MagicMock()
        mgr.entity_extractor = None
        mgr.entity_store = None

        return mgr

    @pytest.mark.asyncio
    async def test_encode_creates_child_chunks(self):
        """测试encode_memory生成子chunk"""
        mgr = self._make_mock_manager()

        mock_security = MagicMock()
        mock_security.scan_threats.return_value.is_safe = True
        mgr._security_filter = mock_security
        mgr.concept_graph = None
        mgr.kg = None
        mgr._governance = None
        mgr._fsrs = MagicMock()

        with patch("memory.memory_manager.validate_memory_content", return_value=""), \
             patch("security.security.SecurityFilter", return_value=mock_security), \
             patch("memory.memory_manager.estimate_initial_difficulty", return_value=5.0):
            mgr.memory.update_fsrs_state = AsyncMock(return_value=None)
            mgr._estimate_importance = MagicMock(return_value=0.7)
            mgr._save_state_json = MagicMock()
            mgr.invalidate_memory_count_cache = MagicMock()

            exchanges = [
                {"role": "user", "content": "我想用React重写前端"},
                {"role": "assistant", "content": "好的，我来帮你规划"},
            ]

            await mgr.encode_memory({"exchanges": exchanges})

            assert mgr.memory.insert_child_chunk.called
            call_count = mgr.memory.insert_child_chunk.call_count
            assert call_count >= 2

            assert mgr.vec.batch_upsert_children.called

    @pytest.mark.asyncio
    async def test_encode_skips_child_when_disabled(self):
        """测试PARENT_CHILD_CHUNK_ENABLED=false时跳过子chunk"""
        mgr = self._make_mock_manager()

        mock_security = MagicMock()
        mock_security.scan_threats.return_value.is_safe = True
        mgr._security_filter = mock_security
        mgr.concept_graph = None
        mgr.kg = None
        mgr._governance = None
        mgr._fsrs = MagicMock()

        with patch("memory.memory_manager.validate_memory_content", return_value=""), \
             patch("security.security.SecurityFilter", return_value=mock_security), \
             patch("memory.memory_manager.estimate_initial_difficulty", return_value=5.0), \
             patch("config.PARENT_CHILD_CHUNK_ENABLED", False):
            mgr.memory.update_fsrs_state = AsyncMock(return_value=None)
            mgr._estimate_importance = MagicMock(return_value=0.7)
            mgr._save_state_json = MagicMock()
            mgr.invalidate_memory_count_cache = MagicMock()

            exchanges = [
                {"role": "user", "content": "测试"},
                {"role": "assistant", "content": "回复"},
            ]

            await mgr.encode_memory({"exchanges": exchanges})

            assert not mgr.memory.insert_child_chunk.called


# ── 向后兼容测试 ──────────────────────────────────────────

class TestBackwardCompatibility:
    """测试向后兼容性"""

    @pytest.mark.asyncio
    async def test_old_memory_without_children(self, tmp_path):
        """测试旧记忆（无子chunk）的检索兼容性"""
        import aiosqlite

        from db.db_memory import MemoryDB

        db_path = str(tmp_path / "test_compat.db")
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row

        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS episodic_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                summary TEXT NOT NULL,
                importance REAL DEFAULT 0.5,
                emotion_label TEXT DEFAULT '',
                session_id TEXT DEFAULT 'user',
                embedding_id INTEGER DEFAULT -1,
                rag_status TEXT DEFAULT 'pending',
                rag_synced_at REAL DEFAULT 0,
                doc_id TEXT DEFAULT '',
                source TEXT DEFAULT 'user',
                access_count INTEGER DEFAULT 0,
                distilled INTEGER DEFAULT 0,
                user_id TEXT DEFAULT 'default',
                agent_id TEXT DEFAULT 'xiaoda',
                is_raw INTEGER DEFAULT 0
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS episodic_memory_fts USING fts5(
                id UNINDEXED, summary_index
            );
            CREATE TABLE IF NOT EXISTS memory_child_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                embed_content TEXT DEFAULT '',
                chunk_type TEXT NOT NULL DEFAULT 'segment',
                importance REAL DEFAULT 0.5,
                overlap_hash TEXT DEFAULT '',
                created_at REAL NOT NULL,
                FOREIGN KEY (parent_id) REFERENCES episodic_memories(id) ON DELETE CASCADE
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_child_chunks_fts
                USING fts5(content, tokenize='unicode61');
        """)
        await conn.commit()

        mdb = MemoryDB(conn)

        # 插入旧记忆（无子chunk）
        _pid = await mdb.insert_episodic_memory(
            summary="旧记忆：用户讨论了Python编程", importance=0.7)

        # 子chunk FTS检索应返回空（不崩溃）
        results = await mdb.search_child_fts("Python", limit=10)
        assert results == []

        # 子chunk→父ID映射应返回空
        parent_ids = await mdb.get_child_parent_ids([1, 2, 3])
        assert parent_ids == []

        await conn.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
