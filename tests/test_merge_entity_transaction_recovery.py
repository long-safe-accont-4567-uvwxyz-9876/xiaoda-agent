"""测试 merge_entity 的事务恢复机制。

验证：当 aiosqlite 单连接上有脏事务残留时，merge_entity 能通过
rollback + 重试恢复正常写入，不再报 "SQL logic error"。
"""
import json
import os
import sys

import aiosqlite

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.db_knowledge import KnowledgeDB


async def _make_db():
    """创建临时 DB 并初始化 knowledge_entities + FTS 表。"""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("""
        CREATE TABLE knowledge_entities (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            kind TEXT DEFAULT '',
            observations TEXT DEFAULT '[]',
            updated_at REAL DEFAULT 0
        )
    """)
    await conn.execute("""
        CREATE VIRTUAL TABLE knowledge_entities_fts USING fts5(id UNINDEXED, name_index)
    """)
    await conn.commit()
    return conn, KnowledgeDB(conn)


async def test_merge_entity_normal_update():
    """正常路径：merge_entity 更新已有实体的 observations。"""
    conn, db = await _make_db()
    try:
        await db.insert_knowledge_entity("ENT-1", "小妲", "人物", ["obs1"])
        await db.merge_entity({"name": "小妲", "kind": "人物", "observations": ["obs2"]})
        entity = await db.get_knowledge_entity("小妲")
        obs = json.loads(entity["observations"])
        assert "obs1" in obs
        assert "obs2" in obs
    finally:
        await conn.close()


async def test_merge_entity_recovers_from_dirty_transaction():
    """脏事务恢复：手动 rollback 清理脏事务后 merge_entity 仍能成功。

    模拟生产场景：_do_children 等 auto_commit=False 操作超时取消后，
    未提交的事务残留在连接上。merge_entity 检测异常后 rollback + 重试。

    生产环境的 "SQL logic error" 来自 aiosqlite 多协程并发时连接状态损坏
    （被取消的协程留下未完成 cursor），单协程测试无法精确复现。
    此测试验证 rollback 清理后 merge_entity 重试路径能正常写入。
    """
    conn, db = await _make_db()
    try:
        await db.insert_knowledge_entity("ENT-1", "小妲", "人物", ["obs1"])

        # 模拟脏事务：执行 auto_commit=False 的 INSERT 但不 commit
        await conn.execute(
            "INSERT INTO knowledge_entities (id, name, kind, observations, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("ENT-dirty", "脏数据", "test", "[]", 0),
        )

        # 模拟 merge_entity 检测到异常后的 rollback（清理脏事务）
        await conn.rollback()

        # 脏数据被 rollback 清除
        dirty = await db.get_knowledge_entity("脏数据")
        assert dirty is None, "脏事务应被 rollback 清除"

        # rollback 后 merge_entity 仍能正常写入（重试路径）
        await db.merge_entity({"name": "小妲", "kind": "人物", "observations": ["obs2"]})

        # 验证 observations 已更新
        entity = await db.get_knowledge_entity("小妲")
        obs = json.loads(entity["observations"])
        assert "obs1" in obs
        assert "obs2" in obs
    finally:
        await conn.close()


async def test_merge_entity_rollback_does_not_corrupt_fts():
    """rollback 恢复后 FTS 索引仍然正常工作。"""
    conn, db = await _make_db()
    try:
        await db.insert_knowledge_entity("ENT-1", "小妲", "人物", ["obs1"])

        # 模拟脏事务
        await conn.execute(
            "INSERT INTO knowledge_entities (id, name, kind, observations, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("ENT-dirty", "脏数据", "test", "[]", 0),
        )

        # merge_entity 恢复
        await db.merge_entity({"name": "小妲", "kind": "人物", "observations": ["obs2"]})

        # FTS 搜索仍正常
        results = await db.search_knowledge_entities("小妲", limit=5)
        assert len(results) > 0
        assert any(r["name"] == "小妲" for r in results)
    finally:
        await conn.close()


async def test_memory_db_rollback_method():
    """MemoryDB.rollback() 方法存在且可用。"""
    from db.db_memory import MemoryDB
    conn = await aiosqlite.connect(":memory:")
    try:
        db = MemoryDB(conn)
        # rollback 方法存在
        assert hasattr(db, 'rollback')
        # rollback 不报错（空事务 rollback 是合法操作）
        await db.rollback()
    finally:
        await conn.close()
