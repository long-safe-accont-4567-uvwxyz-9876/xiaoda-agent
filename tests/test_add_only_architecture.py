"""ADD-only 编码流程测试：原始记忆 append-only + 异步实体提取"""
import asyncio
import time
import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.scope import Scope


@pytest.fixture
async def add_only_db(tmp_path):
    """创建带 v13 schema 的测试数据库 + MemoryManager"""
    from db.database import DatabaseManager
    from memory.memory_manager import MemoryManager
    db_path = tmp_path / "test_add_only.db"
    db = DatabaseManager(db_path)
    await db.init()

    # 创建最小化的 MemoryManager（mock 依赖）
    mgr = MemoryManager.__new__(MemoryManager)
    mgr.db = db
    mgr.memory = db.memory
    mgr.vec = None  # 测试不用向量
    mgr.kg = None
    mgr._security_filter = None
    mgr._reranker = None
    mgr._governance = None
    mgr._last_encode_time = 0
    mgr._pending_encode = False
    mgr._last_message_time = time.time()
    mgr.entity_extractor = None
    mgr.entity_store = None
    mgr.concept_graph = None
    mgr.spreading_engine = None
    mgr.distiller = None
    mgr._memory_count_cache = None
    mgr._memory_count_ts = 0
    mgr._query_cache = None
    mgr._assessor = None
    mgr.router = None
    mgr._query_transformer = None

    yield db, mgr
    await db.close()


class TestHasDuplicateScoped:
    """_has_duplicate 改为只对 is_raw=0 生效"""

    async def test_has_duplicate_checks_refined_only(self, add_only_db):
        """原始记忆（is_raw=1）不去重，提炼知识（is_raw=0）去重"""
        db, mgr = add_only_db
        scope = Scope()

        # 插入一条提炼知识
        await db.memory.insert_episodic_memory(
            summary="用户喜欢Python编程语言", scope=scope, is_raw=0
        )

        # 检查重复（应返回 True，因为有 is_raw=0 的相同记忆）
        is_dup = await mgr._has_duplicate("用户喜欢Python编程语言", scope=scope)
        assert is_dup is True

    async def test_has_duplicate_ignores_raw(self, add_only_db):
        """is_raw=1 的原始记忆不参与去重判断"""
        db, mgr = add_only_db
        scope = Scope()

        # 只插入原始记忆（is_raw=1）
        await db.memory.insert_episodic_memory(
            summary="这是一条原始记录", scope=scope, is_raw=1
        )

        # 检查重复（应返回 False，因为 is_raw=1 不参与去重）
        is_dup = await mgr._has_duplicate("这是一条原始记录", scope=scope)
        assert is_dup is False

    async def test_has_duplicate_scope_isolated(self, add_only_db):
        """不同 scope 的记忆不互相去重"""
        db, mgr = add_only_db
        scope_alice = Scope(user_id="alice", agent_id="xiaoli")
        scope_bob = Scope(user_id="bob", agent_id="xiaoke")

        # alice 的提炼知识
        await db.memory.insert_episodic_memory(
            summary="相同的记忆内容", scope=scope_alice, is_raw=0
        )

        # bob 检查相同内容（应返回 False，不同 scope）
        is_dup = await mgr._has_duplicate("相同的记忆内容", scope=scope_bob)
        assert is_dup is False


class TestEncodeMemoryAddOnly:
    """encode_memory: ADD-only 原始记忆写入"""

    async def test_encode_writes_raw_memory(self, add_only_db):
        """encode_memory 写入 is_raw=1 的原始记忆"""
        db, mgr = add_only_db
        scope = Scope(user_id="test_user", agent_id="test_agent")

        # mock _generate_summary 返回固定文本
        mgr._generate_summary = MagicMock(return_value="用户说: 我喜欢Python")
        mgr._estimate_importance = MagicMock(return_value=0.8)
        mgr._save_state_json = MagicMock()
        mgr.invalidate_memory_count_cache = MagicMock()

        context = {
            "exchanges": [
                {"role": "user", "content": "我喜欢Python"},
                {"role": "assistant", "content": "好的，记下了"},
            ],
            "emotion": {"primary": "开心"},
        }

        await mgr.encode_memory(context, scope=scope)

        # 验证写入了 is_raw=1 的原始记忆
        cursor = await db._conn.execute(
            "SELECT * FROM episodic_memories WHERE user_id=? AND agent_id=? AND is_raw=1",
            (scope.user_id, scope.agent_id),
        )
        rows = await cursor.fetchall()
        assert len(rows) >= 1
        assert "我喜欢Python" in rows[0]["summary"]

    async def test_encode_does_not_dedup_raw(self, add_only_db):
        """encode_memory 对原始记忆不去重（连续两次编码相同内容都写入）"""
        db, mgr = add_only_db
        scope = Scope()

        mgr._generate_summary = MagicMock(return_value="重复的记忆内容")
        mgr._estimate_importance = MagicMock(return_value=0.5)
        mgr._save_state_json = MagicMock()
        mgr.invalidate_memory_count_cache = MagicMock()

        context = {
            "exchanges": [
                {"role": "user", "content": "测试"},
                {"role": "assistant", "content": "回复"},
            ],
        }

        # 连续两次编码相同内容
        await mgr.encode_memory(context, scope=scope)
        await mgr.encode_memory(context, scope=scope)

        # 验证两条 is_raw=1 记录都存在
        cursor = await db._conn.execute(
            "SELECT COUNT(*) as cnt FROM episodic_memories WHERE is_raw=1 AND summary='重复的记忆内容'"
        )
        row = await cursor.fetchone()
        assert row["cnt"] == 2

    async def test_encode_triggers_entity_extraction(self, add_only_db):
        """encode_memory 异步触发实体提取+链接"""
        db, mgr = add_only_db
        scope = Scope()

        mgr._generate_summary = MagicMock(return_value="用户说: 我喜欢Python和React")
        mgr._estimate_importance = MagicMock(return_value=0.5)
        mgr._save_state_json = MagicMock()
        mgr.invalidate_memory_count_cache = MagicMock()

        # mock entity_extractor 和 entity_store
        from memory.entity_extractor import Entity
        mgr.entity_extractor = MagicMock()
        mgr.entity_extractor.extract = AsyncMock(return_value=[
            Entity(name="Python", entity_type="IDENTIFIER"),
            Entity(name="React", entity_type="IDENTIFIER"),
        ])
        mgr.entity_store = MagicMock()
        mgr.entity_store.link_entities = AsyncMock(return_value=2)

        context = {
            "exchanges": [
                {"role": "user", "content": "我喜欢Python和React"},
                {"role": "assistant", "content": "好的"},
            ],
        }

        await mgr.encode_memory(context, scope=scope)

        # 等待异步任务完成
        await asyncio.sleep(0.1)

        # 验证 entity_extractor.extract 被调用
        mgr.entity_extractor.extract.assert_awaited_once()
        # 验证 entity_store.link_entities 被调用
        mgr.entity_store.link_entities.assert_awaited_once()

    async def test_encode_triggers_distill(self, add_only_db):
        """encode_memory 异步触发蒸馏（_distill_to_knowledge）"""
        db, mgr = add_only_db
        scope = Scope()

        mgr._generate_summary = MagicMock(return_value="用户说: 我喜欢Python")
        mgr._estimate_importance = MagicMock(return_value=0.7)
        mgr._save_state_json = MagicMock()
        mgr.invalidate_memory_count_cache = MagicMock()

        # mock distiller — encode_memory 应异步调用 _distill_to_knowledge
        mgr.distiller = MagicMock()
        mgr._distill_to_knowledge = AsyncMock(return_value=None)

        context = {
            "exchanges": [
                {"role": "user", "content": "我喜欢Python"},
                {"role": "assistant", "content": "好的，记下了"},
            ],
            "emotion": {"primary": "开心"},
        }

        await mgr.encode_memory(context, scope=scope)

        # 等待异步任务完成
        await asyncio.sleep(0.1)

        # 验证 _distill_to_knowledge 被调用
        mgr._distill_to_knowledge.assert_awaited_once()
        call_args = mgr._distill_to_knowledge.call_args
        # 验证传入参数包含 scope、importance、emotion
        assert call_args.kwargs.get("scope") == scope or call_args.args[2] == scope
        assert call_args.kwargs.get("importance") == 0.7 or call_args.args[3] == 0.7

    async def test_encode_without_scope_uses_default(self, add_only_db):
        """encode_memory 不传 scope 时使用默认 Scope()"""
        db, mgr = add_only_db

        mgr._generate_summary = MagicMock(return_value="默认scope测试")
        mgr._estimate_importance = MagicMock(return_value=0.5)
        mgr._save_state_json = MagicMock()
        mgr.invalidate_memory_count_cache = MagicMock()

        context = {
            "exchanges": [
                {"role": "user", "content": "测试"},
                {"role": "assistant", "content": "回复"},
            ],
        }

        await mgr.encode_memory(context)  # 不传 scope

        # 验证写入了默认 scope 的记忆
        cursor = await db._conn.execute(
            "SELECT * FROM episodic_memories WHERE user_id='default' AND agent_id='xiaoda' AND is_raw=1"
        )
        rows = await cursor.fetchall()
        assert len(rows) >= 1
