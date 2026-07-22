"""Scope 三级隔离测试：user_id/session_id/agent_id 过滤逻辑"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.scope import Scope


class TestScopeDataclass:
    """Scope dataclass 基础功能"""

    def test_default_scope(self):
        """默认 scope: user='default', session='user', agent='xiaoda'"""
        scope = Scope()
        assert scope.user_id == "default"
        assert scope.session_id == "user"
        assert scope.agent_id == "xiaoda"

    def test_custom_scope(self):
        """自定义 scope"""
        scope = Scope(user_id="alice", session_id="sess-123", agent_id="xiaoli")
        assert scope.user_id == "alice"
        assert scope.session_id == "sess-123"
        assert scope.agent_id == "xiaoli"

    def test_to_sql_filter_default_table(self):
        """SQL WHERE 子句生成（默认表名 episodic_memories）"""
        scope = Scope(user_id="alice", agent_id="xiaoli")
        where = scope.to_sql_filter()
        assert "episodic_memories.user_id" in where
        assert "episodic_memories.agent_id" in where
        assert "alice" in where
        assert "xiaoli" in where

    def test_to_sql_filter_custom_table(self):
        """SQL WHERE 子句生成（自定义表名）"""
        scope = Scope(user_id="bob", agent_id="xiaoke")
        where = scope.to_sql_filter(table="em")
        assert "em.user_id" in where
        assert "em.agent_id" in where

    def test_to_sql_params(self):
        """参数化 SQL 返回参数列表"""
        scope = Scope(user_id="alice", agent_id="xiaoli")
        params = scope.to_sql_params()
        assert "alice" in params
        assert "xiaoli" in params
        assert len(params) == 2


class TestScopeDBIntegration:
    """Scope 与 DB 集成：验证 scope 过滤的检索"""

    @pytest.fixture
    async def scoped_db(self, tmp_path):
        """创建带 scope 数据的测试数据库"""
        from db.database import DatabaseManager
        from db.fts_utils import _tokenize_for_fts
        db_path = tmp_path / "test_scope.db"
        db = DatabaseManager(db_path)
        await db.init()
        # 插入不同 scope 的记忆 + 同步写入 FTS 索引
        import time as _time
        now = _time.time()
        test_data = [
            (now, "alice的记忆", "alice", "xiaoli", 0),
            (now, "bob的记忆", "bob", "xiaoke", 0),
            (now, "default的记忆", "default", "xiaoda", 0),
        ]
        for ts, summary, user_id, agent_id, is_raw in test_data:
            cursor = await db._conn.execute(
                "INSERT INTO episodic_memories (timestamp, summary, user_id, agent_id, is_raw) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts, summary, user_id, agent_id, is_raw),
            )
            mem_id = cursor.lastrowid
            # 同步写入 FTS 索引（模拟 insert_episodic_memory 的行为）
            tokenized = _tokenize_for_fts(summary)
            if tokenized.strip():
                await db._conn.execute(
                    "INSERT INTO episodic_memory_fts(id, summary_index) VALUES(?, ?)",
                    (mem_id, tokenized),
                )
        await db._conn.commit()
        yield db
        await db.close()

    async def test_search_scoped_alice(self, scoped_db):
        """alice scope 只查到 alice 的记忆"""
        scope = Scope(user_id="alice", agent_id="xiaoli")
        results = await scoped_db.memory.search_memories_fts_scoped(
            "记忆", scope=scope, limit=10
        )
        assert len(results) == 1
        assert results[0]["summary"] == "alice的记忆"

    async def test_search_scoped_bob(self, scoped_db):
        """bob scope 只查到 bob 的记忆"""
        scope = Scope(user_id="bob", agent_id="xiaoke")
        results = await scoped_db.memory.search_memories_fts_scoped(
            "记忆", scope=scope, limit=10
        )
        assert len(results) == 1
        assert results[0]["summary"] == "bob的记忆"

    async def test_search_scoped_default(self, scoped_db):
        """default scope 只查到 default 的记忆"""
        scope = Scope()
        results = await scoped_db.memory.search_memories_fts_scoped(
            "记忆", scope=scope, limit=10
        )
        assert len(results) == 1
        assert results[0]["summary"] == "default的记忆"

    async def test_insert_with_scope(self, tmp_path):
        """通过 insert_episodic_memory 传入 scope，验证字段写入正确"""
        from db.database import DatabaseManager
        db_path = tmp_path / "test_insert_scope.db"
        db = DatabaseManager(db_path)
        await db.init()
        scope = Scope(user_id="charlie", session_id="sess-456", agent_id="xiaolian")
        mem_id = await db.memory.insert_episodic_memory(
            summary="charlie的新记忆", scope=scope
        )
        # 查询验证
        mem = await db.memory.get_memory_by_id(mem_id)
        assert mem["user_id"] == "charlie"
        assert mem["session_id"] == "sess-456"
        assert mem["agent_id"] == "xiaolian"
        assert mem["is_raw"] == 0  # 默认 is_raw=0
        await db.close()

    async def test_insert_raw_with_scope(self, tmp_path):
        """插入 is_raw=1 的原始记忆"""
        from db.database import DatabaseManager
        db_path = tmp_path / "test_insert_raw.db"
        db = DatabaseManager(db_path)
        await db.init()
        scope = Scope(user_id="charlie", agent_id="xiaolian")
        mem_id = await db.memory.insert_episodic_memory(
            summary="原始记录", scope=scope, is_raw=1
        )
        mem = await db.memory.get_memory_by_id(mem_id)
        assert mem["is_raw"] == 1
        assert mem["user_id"] == "charlie"
        await db.close()
