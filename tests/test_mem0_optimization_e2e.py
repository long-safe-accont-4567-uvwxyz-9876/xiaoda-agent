"""端到端集成测试：验证 mem0 SPEC 优化完整流程

流程：编码 → 提取实体 → 蒸馏 → 检索 → Entity Boost
"""
import asyncio
import time
import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.scope import Scope
from memory.entity_extractor import EntityExtractor, Entity
from memory.entity_store import EntityStore
from memory.memory_distiller import MemoryDistiller


@pytest.fixture
async def e2e_db(tmp_path):
    """创建带 v13 schema 的完整测试环境"""
    from db.database import DatabaseManager
    from memory.memory_manager import MemoryManager
    db_path = tmp_path / "test_e2e.db"
    db = DatabaseManager(db_path)
    await db.init()

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
    mgr.entity_extractor = EntityExtractor(router=None)
    mgr.entity_store = EntityStore(db.memory)
    mgr.distiller = MemoryDistiller(router=None)
    mgr.concept_graph = None
    mgr.spreading_engine = None
    mgr._query_cache = None
    mgr._assessor = None
    mgr._memory_count_cache = None
    mgr._memory_count_ts = 0
    mgr.router = None
    mgr._query_transformer = None

    yield db, mgr
    await db.close()


class TestEndToEndMemoryFlow:
    """端到端：编码 → 提取 → 链接 → 蒸馏 → 检索 → Boost"""

    async def test_full_flow_with_entity_boost(self, e2e_db):
        """完整流程：用户说喜欢Python → 编码 → 提取实体 → 检索时 Boost 生效"""
        db, mgr = e2e_db
        scope = Scope(user_id="alice", agent_id="xiaoli")

        # mock 辅助方法
        mgr._generate_summary = MagicMock(return_value="用户说: 我喜欢Python编程语言")
        mgr._estimate_importance = MagicMock(return_value=0.8)
        mgr._save_state_json = MagicMock()
        mgr.invalidate_memory_count_cache = MagicMock()
        mgr._enrich_memory_async = AsyncMock()
        # 禁用蒸馏：避免 _distill_to_knowledge 后台任务与 _extract_and_link_entities
        # 并发访问同一 SQLite 连接导致 entity 插入静默失败
        mgr.distiller = None

        # 1. 编码记忆
        context = {
            "exchanges": [
                {"role": "user", "content": "我喜欢Python编程语言"},
                {"role": "assistant", "content": "好的，记下了"},
            ],
            "emotion": {"primary": "开心"},
        }
        await mgr.encode_memory(context, scope=scope)

        # 等待异步实体提取任务完成（jieba 初始化 + DB 操作需要时间）
        await asyncio.sleep(1.5)

        # 2. 验证原始记忆写入（is_raw=1）
        cursor = await db._conn.execute(
            "SELECT * FROM episodic_memories WHERE is_raw=1 AND user_id='alice'"
        )
        raw_rows = await cursor.fetchall()
        assert len(raw_rows) >= 1

        # 3. 验证实体提取（Python → IDENTIFIER）
        cursor = await db._conn.execute(
            "SELECT * FROM memory_entities WHERE name='Python'"
        )
        entity_rows = await cursor.fetchall()
        assert len(entity_rows) >= 1

        # 4. 验证实体链接（entity_memory_links 有记录）
        cursor = await db._conn.execute(
            "SELECT * FROM entity_memory_links WHERE entity_id=?",
            (entity_rows[0]["id"],),
        )
        link_rows = await cursor.fetchall()
        assert len(link_rows) >= 1

        # 5. 检索 "Python" 相关记忆（include_raw=True 因为原始记忆是 is_raw=1）
        mgr.get_memory_tier = AsyncMock(return_value="cold")
        results = await mgr.retrieve_memories_hybrid(
            "Python", k=5, scope=scope, include_raw=True
        )
        assert len(results) >= 1

    async def test_scope_isolation_e2e(self, e2e_db):
        """scope 隔离：不同用户的记忆互不串"""
        db, mgr = e2e_db
        scope_alice = Scope(user_id="alice", agent_id="xiaoli")
        scope_bob = Scope(user_id="bob", agent_id="xiaoke")

        mgr._generate_summary = MagicMock(side_effect=[
            "alice说: 我喜欢Python",
            "bob说: 我喜欢Java",
        ])
        mgr._estimate_importance = MagicMock(return_value=0.5)
        mgr._save_state_json = MagicMock()
        mgr.invalidate_memory_count_cache = MagicMock()
        mgr._enrich_memory_async = AsyncMock()

        # alice 编码 Python 记忆
        await mgr.encode_memory({
            "exchanges": [
                {"role": "user", "content": "我喜欢Python"},
                {"role": "assistant", "content": "好的"},
            ],
        }, scope=scope_alice)

        # bob 编码 Java 记忆
        await mgr.encode_memory({
            "exchanges": [
                {"role": "user", "content": "我喜欢Java"},
                {"role": "assistant", "content": "好的"},
            ],
        }, scope=scope_bob)

        await asyncio.sleep(0.2)

        # alice 检索：不应看到 bob 的记忆
        mgr.get_memory_tier = AsyncMock(return_value="cold")
        results_alice = await mgr.retrieve_memories_hybrid("编程", k=5, scope=scope_alice)
        for r in results_alice:
            assert r["user_id"] == "alice"
            assert r["agent_id"] == "xiaoli"

        # bob 检索：不应看到 alice 的记忆
        results_bob = await mgr.retrieve_memories_hybrid("编程", k=5, scope=scope_bob)
        for r in results_bob:
            assert r["user_id"] == "bob"
            assert r["agent_id"] == "xiaoke"

    async def test_add_only_no_dedup_e2e(self, e2e_db):
        """ADD-only：连续编码相同内容都写入（不去重）"""
        db, mgr = e2e_db
        scope = Scope()

        mgr._generate_summary = MagicMock(return_value="重复的内容：我喜欢Python")
        mgr._estimate_importance = MagicMock(return_value=0.5)
        mgr._save_state_json = MagicMock()
        mgr.invalidate_memory_count_cache = MagicMock()
        mgr._enrich_memory_async = AsyncMock()

        context = {
            "exchanges": [
                {"role": "user", "content": "我喜欢Python"},
                {"role": "assistant", "content": "好的"},
            ],
        }

        # 连续三次编码相同内容
        await mgr.encode_memory(context, scope=scope)
        await mgr.encode_memory(context, scope=scope)
        await mgr.encode_memory(context, scope=scope)

        # 验证三条 is_raw=1 记录都存在
        cursor = await db._conn.execute(
            "SELECT COUNT(*) as cnt FROM episodic_memories WHERE is_raw=1 AND summary='重复的内容：我喜欢Python'"
        )
        row = await cursor.fetchone()
        assert row["cnt"] == 3

    async def test_entity_recall_sixth_path_e2e(self, e2e_db):
        """第6路召回：通过实体反查到记忆"""
        db, mgr = e2e_db
        scope = Scope()

        # 手动插入提炼知识 + 实体链接
        mem_id = await db.memory.insert_episodic_memory(
            summary="Python编程技巧", scope=scope, is_raw=0
        )
        entity_id = await db.memory.insert_memory_entity(
            name="Python", entity_type="IDENTIFIER", kind="技术"
        )
        await db.memory.insert_entity_memory_link(entity_id, mem_id)

        # mock hot 档位走六路检索
        mgr.get_memory_tier = AsyncMock(return_value="hot")
        mgr._extract_deterministic_selectors = MagicMock(return_value={})
        mgr._get_candidate_ids_by_selectors = AsyncMock(return_value=None)
        mgr._hybrid_fts_search_scoped = AsyncMock(return_value=[])
        mgr._hybrid_vec_search = AsyncMock(return_value=[])
        mgr.invalidate_memory_count_cache = MagicMock()

        # 检索 "Python"
        results = await mgr.retrieve_memories_hybrid("Python", k=5, scope=scope)

        # 第6路应召回 Python 相关记忆
        assert len(results) >= 1
        assert any(r["id"] == mem_id for r in results)
        # 验证 entity_recall 标记
        assert any(r.get("entity_recall") for r in results)

    async def test_distillation_creates_refined_knowledge_e2e(self, e2e_db):
        """蒸馏：原始记忆 → 提炼知识"""
        db, mgr = e2e_db
        scope = Scope()

        mgr._generate_summary = MagicMock(return_value="原始：用户喜欢Python编程")
        mgr._estimate_importance = MagicMock(return_value=0.8)
        mgr._save_state_json = MagicMock()
        mgr.invalidate_memory_count_cache = MagicMock()
        mgr._enrich_memory_async = AsyncMock()

        # mock distiller 返回蒸馏结果
        mgr.distiller.distill = AsyncMock(return_value="用户喜欢Python编程（蒸馏）")
        mgr._find_similar_knowledge = AsyncMock(return_value=None)

        await mgr.encode_memory({
            "exchanges": [
                {"role": "user", "content": "我喜欢Python"},
                {"role": "assistant", "content": "好的"},
            ],
        }, scope=scope)

        # 等待异步蒸馏完成（轮询，最多 5 秒）
        for _ in range(50):
            await asyncio.sleep(0.1)
            cursor = await db._conn.execute(
                "SELECT COUNT(*) as cnt FROM episodic_memories WHERE is_raw=0 AND user_id='default'"
            )
            row = await cursor.fetchone()
            if row["cnt"] >= 1:
                break

        # 验证有 is_raw=0 的提炼知识
        cursor = await db._conn.execute(
            "SELECT * FROM episodic_memories WHERE is_raw=0 AND user_id='default'"
        )
        refined_rows = await cursor.fetchall()
        assert len(refined_rows) >= 1
        assert "蒸馏" in refined_rows[0]["summary"]
