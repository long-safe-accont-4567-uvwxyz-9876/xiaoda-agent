"""Scope 三级隔离测试：user_id/session_id/agent_id 过滤逻辑"""
import asyncio
import time
from dataclasses import FrozenInstanceError
import pytest
from pathlib import Path

import sys
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
        assert params[-1] == "qq_group:%"
        assert len(params) == 3

    def test_scope_is_immutable(self):
        scope = Scope.group(user_id="alice", group_id="group-a")

        with pytest.raises(FrozenInstanceError):
            scope.session_id = "qq_group:group-b"


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
        import time
        now = time.time()
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

    async def test_same_user_private_and_qq_groups_are_isolated(self, scoped_db):
        """同一用户的私聊、群 A、群 B 互不检索，私聊仍跨 session 共享。"""
        private_one = Scope.personal(
            user_id="alice", session_id="private-session-1", agent_id="xiaoda"
        )
        private_two = Scope.personal(
            user_id="alice", session_id="private-session-2", agent_id="xiaoda"
        )
        group_a = Scope.group(
            user_id="alice", group_id="group-a", agent_id="xiaoda"
        )
        group_b = Scope.group(
            user_id="alice", group_id="group-b", agent_id="xiaoda"
        )

        for summary, scope in (
            ("boundary private one", private_one),
            ("boundary private two", private_two),
            ("boundary group a", group_a),
            ("boundary group b", group_b),
        ):
            await scoped_db.memory.insert_episodic_memory(summary=summary, scope=scope)

        private_results = await scoped_db.memory.search_memories_fts_scoped(
            "boundary", scope=private_two, limit=10
        )
        group_a_results = await scoped_db.memory.search_memories_fts_scoped(
            "boundary", scope=group_a, limit=10
        )
        group_b_results = await scoped_db.memory.search_memories_fts_scoped(
            "boundary", scope=group_b, limit=10
        )

        assert {row["summary"] for row in private_results} == {
            "boundary private one",
            "boundary private two",
        }
        assert [row["summary"] for row in group_a_results] == ["boundary group a"]
        assert [row["summary"] for row in group_b_results] == ["boundary group b"]

    async def test_time_entity_and_child_channels_apply_group_boundary_before_limit(
        self, scoped_db
    ):
        scopes = {
            "private": Scope.personal("alice", "private-session"),
            "group-a": Scope.group("alice", "group-a"),
            "group-b": Scope.group("alice", "group-b"),
        }
        memory_ids = {}
        for label, scope in scopes.items():
            memory_ids[label] = await scoped_db.memory.insert_episodic_memory(
                summary=f"shared evidence {label}", scope=scope
            )
            await scoped_db.memory.insert_child_chunk(
                memory_ids[label], f"child evidence {label}"
            )
        entity_id = await scoped_db.memory.insert_memory_entity("shared-entity")
        for memory_id in memory_ids.values():
            await scoped_db.memory.insert_entity_memory_link(entity_id, memory_id)

        now = time.time()
        for label, scope in scopes.items():
            by_time = await scoped_db.memory.search_memories_by_time_scoped(
                0, now + 1, scope=scope, limit=1
            )
            by_entity = await scoped_db.memory.get_memories_by_entity_names_scoped(
                ["shared-entity"], scope=scope, limit=1, is_raw=None
            )
            child = await scoped_db.memory.search_child_fts(
                "child evidence", limit=1, scope=scope
            )

            assert [row["id"] for row in by_time] == [memory_ids[label]]
            assert [row["id"] for row in by_entity] == [memory_ids[label]]
            assert [row["parent_id"] for row in child] == [memory_ids[label]]
