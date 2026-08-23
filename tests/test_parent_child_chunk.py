"""父子Chunk RAG优化 + Contextual Retrieval 测试"""
import asyncio
from contextlib import asynccontextmanager
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
        """测试 Contextual Retrieval 前缀注入（开关开启时）"""
        mgr = self._make_manager()
        exchanges = [{"role": "user", "content": "测试内容"}]
        parent_summary = "这是一段父摘要"

        # 显式开启 CONTEXTUAL_RETRIEVAL_ENABLED，避免依赖运行环境
        # （.env 可能设为 false，_split_into_children 尊重开关不注入前缀 → 测试误判失败）
        with patch("config.CONTEXTUAL_RETRIEVAL_ENABLED", True):
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

    @pytest.mark.asyncio
    async def test_batch_insert_children_is_atomic_and_returns_ids(self, memory_db):
        mdb, conn = memory_db
        parent_id = await mdb.insert_episodic_memory(summary="父", importance=0.5)

        child_ids = await mdb.insert_child_chunks(parent_id, [
            {"content": "子1", "embed_content": "向量1"},
            {"content": "子2", "embed_content": "向量2"},
        ])

        assert len(child_ids) == 2
        assert [row["id"] for row in await mdb.get_children_by_parent(parent_id)] == child_ids
        fts_count = await (await conn.execute(
            "SELECT COUNT(*) FROM memory_child_chunks_fts WHERE rowid IN (?, ?)", child_ids
        )).fetchone()
        assert fts_count[0] == 2


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

    def test_batch_upsert_children_skips_empty(self):
        """测试空列表时batch_upsert_children跳过"""
        from memory.vector_store import VectorStore
        vs = VectorStore.__new__(VectorStore)
        vs._initialized = False
        vs._closed = False
        vs._vec_conn = None

        asyncio.run(vs.batch_upsert_children([]))

    @pytest.mark.asyncio
    async def test_batch_upsert_children_sqlite_failure_does_not_mutate_brute(self):
        from memory.vector_store import VectorStore

        connection = MagicMock()
        connection.execute.side_effect = [None, None, RuntimeError("second insert failed"), None]
        brute = MagicMock()
        vs = VectorStore.__new__(VectorStore)
        vs._initialized = True
        vs._closed = False
        vs._vec_conn = connection
        vs._lock = __import__("threading").Lock()
        vs._brute = brute
        vs._dimensions = 2
        vs.embed = AsyncMock(return_value=[[1.0, 2.0], [3.0, 4.0]])

        assert not await vs.batch_upsert_children([(11, "first"), (12, "second")])
        brute.upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_upsert_children_brute_failure_rebuilds_from_committed_db(self):
        from memory.vector_store import VectorStore

        connection = MagicMock()
        brute = MagicMock()
        brute.upsert.side_effect = [True, False]
        brute.load_from_db.return_value = True
        vs = VectorStore.__new__(VectorStore)
        vs._initialized = True
        vs._closed = False
        vs._vec_conn = connection
        vs._lock = __import__("threading").Lock()
        vs._brute = brute
        vs._dimensions = 2
        vs.embed = AsyncMock(return_value=[[1.0, 2.0], [3.0, 4.0]])

        assert await vs.batch_upsert_children([(11, "first"), (12, "second")])
        connection.commit.assert_called_once()
        brute.load_from_db.assert_called_once_with(connection)

    @pytest.mark.asyncio
    async def test_brute_restart_rebuilds_stale_parent_and_child_snapshots(self, tmp_path, monkeypatch):
        import sqlite3

        import sqlite_vec

        from memory.vector_store import VectorStore

        monkeypatch.setenv("VECTOR_BRUTE_ENABLED", "1")
        db_path = tmp_path / "vectors.db"
        first = VectorStore(db_path, embed_mode="remote", dimensions=2)
        await first.init()
        first.embed = AsyncMock(side_effect=[[[1.0, 0.0]], [[1.0, 0.0]]])
        assert await first.upsert(1, "parent-one")
        assert await first.batch_upsert_children([(1, "child-one")])
        await first.close()

        conn = sqlite3.connect(db_path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute(
            "INSERT INTO memories_vec(rowid, embedding) VALUES (?, vec_f32(?))",
            (2, "[0.0, 1.0]"),
        )
        conn.execute(
            "INSERT INTO memories_child_vec(rowid, embedding) VALUES (?, vec_f32(?))",
            (2, "[0.0, 1.0]"),
        )
        conn.commit()
        conn.close()

        restarted = VectorStore(db_path, embed_mode="remote", dimensions=2)
        await restarted.init()
        restarted.embed = AsyncMock(return_value=[[0.0, 1.0]])

        assert restarted._brute.stats["tables"]["memories_vec"]["alive"] == 2
        assert restarted._brute.stats["tables"]["memories_child_vec"]["alive"] == 2
        assert [row_id for row_id, _ in await restarted.search("parent-two", top_k=2)] == [2, 1]
        assert [row["id"] for row in await restarted.search_child([0.0, 1.0], top_k=2)] == [2, 1]
        await restarted.close()


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
        mgr.memory.insert_child_chunks = AsyncMock(return_value=[100, 101])
        mgr.memory.insert_consolidation_candidate = AsyncMock(return_value=1)
        mgr.memory.mark_candidate_applied = AsyncMock(return_value=None)
        mgr.memory.update_memory_enrichment = AsyncMock(return_value=None)
        # _do_children 内部调用 self.memory._conn.commit()（批量写入后统一提交）
        # 必须是 AsyncMock 否则 await 会抛 TypeError: object MagicMock can't be used in 'await'
        mgr.memory._conn = MagicMock()
        mgr.memory._conn.commit = AsyncMock(return_value=None)

        mgr.vec = MagicMock()
        mgr.vec.upsert = AsyncMock(return_value=True)
        mgr.vec.batch_upsert_children = AsyncMock(return_value=None)
        mgr.vec.enabled = True

        mgr.kg = None
        mgr._governance = None
        mgr._security_filter = None
        mgr._last_encode_time = 0
        mgr._pending_encode = False
        # distill 必须成功：若 distill 失败会触发 _spawn(_retry_distill_exc) 重试任务
        # （内部 await asyncio.sleep(30)），该任务进入 _bg_tasks 后会让
        # test_encode_creates_child_chunks 的"等待本次 encode 新任务"快照超时。
        # 33d8f8b 起 _spawn 统一跟踪后台任务（旧 create_task 不进入 _bg_tasks）。
        mgr.distiller = MagicMock()
        mgr.distiller.distill = AsyncMock(return_value="提炼后的知识摘要")
        mgr.entity_extractor = None
        mgr.entity_store = None
        # encode_memory 主流程在 line 2604 检查 if self._query_cache:
        # 缺少该属性会抛 AttributeError → encode_failed
        mgr._query_cache = None
        # line 2608 检查 if getattr(self, 'spreading_engine', None)
        mgr.spreading_engine = None
        # line 2607 G13 失效扩散 recall 缓存
        mgr.concept_graph = None
        # _indexing_task 用 self.db.write_transaction() 串行化子chunk写入（事务锁架构）
        @asynccontextmanager
        async def _write_txn():
            yield MagicMock()
        mgr.db = MagicMock()
        mgr.db.write_transaction = _write_txn

        return mgr

    @pytest.mark.asyncio
    async def test_encode_creates_child_chunks(self):
        """测试encode_memory生成子chunk"""
        mgr = self._make_mock_manager()

        mock_security = MagicMock()
        mock_security.scan_threats.return_value.is_safe = True
        mgr._security_filter = mock_security
        mgr._fsrs = MagicMock()
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

            # 记录 encode 前的后台任务快照
            from core.background_tasks import _bg_tasks
            _before = set(_bg_tasks)

            await mgr.encode_memory({"exchanges": exchanges})

            # encode_memory 将 insert_child_chunk 调用放在 fire-and-forget
            # create_task（_indexing_task）中，不阻塞 encode_memory 返回。
            # 测试必须等待后台任务完成才能断言 insert_child_chunk.called。
            # 用快照差集获取本次 encode 创建的新任务，避免等待其他测试的任务。
            _new_tasks = [t for t in _bg_tasks if t not in _before]
            # done_callback 可能已将完成的任务从 _bg_tasks 移除，
            # 但未完成的仍在。补充等待仍在集合中的新任务。
            if _new_tasks:
                # CodeRabbit #C: 移除 return_exceptions=True，让 _indexing_task 失败
                # 直接传播并 fail 测试（而非被静默吞掉，掩盖后台任务异常）
                await asyncio.wait_for(
                    asyncio.gather(*_new_tasks),
                    timeout=5.0,
                )
            # 额外让事件循环空转一轮，确保 done_callback 执行完毕
            await asyncio.sleep(0)

            mgr.memory.insert_child_chunks.assert_awaited_once()

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

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", [False, asyncio.TimeoutError(), RuntimeError("vector failed")])
    async def test_encode_compensates_only_new_children_when_vector_index_fails(self, failure):
        mgr = self._make_mock_manager()
        mock_security = MagicMock()
        mock_security.scan_threats.return_value.is_safe = True
        mgr._security_filter = mock_security
        mgr._fsrs = MagicMock()
        mgr.memory.insert_child_chunks = AsyncMock(return_value=[101, 102])
        mgr.memory.delete_child_chunks = AsyncMock()
        mgr.memory.update_fsrs_state = AsyncMock(return_value=None)
        if isinstance(failure, BaseException):
            mgr.vec.batch_upsert_children = AsyncMock(side_effect=failure)
        else:
            mgr.vec.batch_upsert_children = AsyncMock(return_value=failure)
        mgr._estimate_importance = MagicMock(return_value=0.7)
        mgr._save_state_json = MagicMock()
        mgr.invalidate_memory_count_cache = MagicMock()
        mgr._split_into_children = MagicMock(return_value=[
            {
                "content": "first",
                "embed_content": "first vector",
                "chunk_type": "segment",
                "weight": 1.0,
                "overlap_hash": "",
            },
            {
                "content": "second",
                "embed_content": "second vector",
                "chunk_type": "segment",
                "weight": 0.8,
                "overlap_hash": "",
            },
        ])

        with patch("memory.memory_manager.validate_memory_content", return_value=""), \
             patch("security.security.SecurityFilter", return_value=mock_security), \
             patch("memory.memory_manager.estimate_initial_difficulty", return_value=5.0):
            from core.background_tasks import _bg_tasks
            before = set(_bg_tasks)
            await mgr.encode_memory({"exchanges": [
                {"role": "user", "content": "测试补偿"},
                {"role": "assistant", "content": "开始索引"},
            ]})
            tasks = [task for task in _bg_tasks if task not in before]
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)

        mgr.memory.delete_child_chunks.assert_awaited_once_with([101, 102])

    @pytest.mark.asyncio
    async def test_cancel_after_committed_child_batch_compensates_exact_new_ids(self):
        committed = asyncio.Event()
        return_result = asyncio.Event()

        class FakeMemory:
            def __init__(self):
                self.rows = {7: {"content": "existing"}}

            async def insert_child_chunks(self, parent_id, children, auto_commit=True):
                self.rows.update({101: children[0], 102: children[1]})
                committed.set()
                await return_result.wait()
                return [101, 102]

            async def delete_child_chunks(self, child_ids):
                for child_id in child_ids:
                    self.rows.pop(child_id, None)

        manager = __import__("memory.memory_manager", fromlist=["MemoryManager"]).MemoryManager.__new__(
            __import__("memory.memory_manager", fromlist=["MemoryManager"]).MemoryManager
        )
        manager.memory = FakeMemory()
        manager.vec = MagicMock()
        manager.vec.batch_upsert_children = AsyncMock(return_value=True)
        children = [
            {"content": "first", "embed_content": "v1", "chunk_type": "segment", "weight": 1.0, "overlap_hash": ""},
            {"content": "second", "embed_content": "v2", "chunk_type": "segment", "weight": 0.8, "overlap_hash": ""},
        ]

        task = asyncio.create_task(manager._insert_indexed_children(9, children, 0.8))
        await committed.wait()
        task.cancel()
        await asyncio.sleep(0)
        return_result.set()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.memory.rows == {7: {"content": "existing"}}
        manager.vec.batch_upsert_children.assert_not_awaited()


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
        pid = await mdb.insert_episodic_memory(
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
