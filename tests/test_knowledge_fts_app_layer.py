"""回归测试：knowledge_entities_fts 应用层维护（替代坏触发器）。

根因：contentless FTS5 的 'delete' 命令在 SQLite 3.40 始终报 SQL logic error，
原触发器 knowledge_entities_fts_au/ad 用 TEXT id 当 rowid 调 delete 全部失败，
导致 merge_entity UPDATE 失败、observations 写不进、称呼错乱。
修复：DROP 触发器，改由 db_knowledge._sync_entity_fts 用普通 DELETE+INSERT 维护。

本测试验证应用层 FTS 维护在 insert / merge(UPDATE) / delete / cleanup 四条路径下
保持 FTS 与主表一致，且 merge_entity 不再报 SQL logic error。
"""
import asyncio
import json
import os
import sys
import tempfile

import aiosqlite

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.db_knowledge import KnowledgeDB  # noqa: E402

DDL = """
CREATE TABLE knowledge_entities (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE,
    kind TEXT DEFAULT '',
    observations TEXT DEFAULT '[]',
    updated_at REAL NOT NULL
);
CREATE VIRTUAL TABLE knowledge_entities_fts USING fts5(id UNINDEXED, name_index);
CREATE TABLE knowledge_relations (
    id TEXT PRIMARY KEY, from_entity TEXT, relation_type TEXT, to_entity TEXT,
    created_at REAL DEFAULT 0, updated_at REAL NOT NULL
);
"""


async def _make_db() -> tuple[aiosqlite.Connection, KnowledgeDB]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(DDL)
    await conn.commit()
    return conn, KnowledgeDB(conn)


async def _fts_names(db: KnowledgeDB, keyword: str) -> list[str]:
    cur = await db._conn.execute(
        "SELECT ke.name FROM knowledge_entities_fts "
        "JOIN knowledge_entities ke ON ke.id = knowledge_entities_fts.id "
        "WHERE knowledge_entities_fts MATCH ?",
        (f'"{keyword}"',),
    )
    return [r[0] for r in await cur.fetchall()]


async def _fts_count(db: KnowledgeDB) -> int:
    cur = await db._conn.execute("SELECT count(*) FROM knowledge_entities_fts")
    return (await cur.fetchone())[0]


async def test_insert_syncs_fts():
    conn, db = await _make_db()
    try:
        await db.insert_knowledge_entity("ENT-1", "小妲", "人物", ["会暖被窝"])
        assert await _fts_count(db) == 1, "insert 后 FTS 应有 1 条"
        names = await _fts_names(db, "小妲")
        assert "小妲" in names, "FTS 应能搜到小妲"
    finally:
        await conn.close()


async def test_merge_update_no_error_and_fts_intact():
    """merge_entity 更新 observations 不应报 SQL logic error，FTS 仍可搜。"""
    conn, db = await _make_db()
    try:
        await db.insert_knowledge_entity("ENT-1", "小妲", "人物", ["obs1"])
        # merge 已存在实体：追加 observations
        await db.merge_entity({"name": "小妲", "kind": "人物",
                               "observations": ["obs2", "obs1"]})
        # 关键断言：未抛 SQL logic error（走到这里即通过）
        obs = json.loads((await db.get_knowledge_entity("小妲"))["observations"])
        assert "obs2" in obs, "merge 后新 observation 应写入"
        assert await _fts_count(db) == 1, "merge 不改 name，FTS 条目数不变"
        assert "小妲" in await _fts_names(db, "小妲"), "merge 后 FTS 仍可搜"
    finally:
        await conn.close()


async def test_merge_new_entity_syncs_fts():
    """merge_entity 对新实体走 insert 路径，FTS 应同步。"""
    conn, db = await _make_db()
    try:
        await db.merge_entity({"name": "纳西妲", "kind": "神明",
                               "observations": ["草神"]})
        assert await _fts_count(db) == 1
        assert "纳西妲" in await _fts_names(db, "纳西妲")
    finally:
        await conn.close()


async def test_delete_removes_fts():
    conn, db = await _make_db()
    try:
        await db.insert_knowledge_entity("ENT-1", "小妲", "人物", [])
        await db.insert_knowledge_entity("ENT-2", "爸爸", "人物", [])
        assert await _fts_count(db) == 2
        await db.delete_knowledge_entity("小妲")
        assert await _fts_count(db) == 1, "delete 后 FTS 应剩 1 条"
        assert "小妲" not in await _fts_names(db, "小妲"), "FTS 不应再搜到小妲"
        assert "爸爸" in await _fts_names(db, "爸爸"), "爸爸应仍在"
    finally:
        await conn.close()


async def test_cleanup_stale_removes_fts():
    conn, db = await _make_db()
    try:
        import time as _t
        await db.insert_knowledge_entity("ENT-old", "旧实体", "", [])
        # 把旧实体 updated_at 改成 100 天前
        await db._conn.execute(
            "UPDATE knowledge_entities SET updated_at=? WHERE name='旧实体'",
            (_t.time() - 100 * 86400,),
        )
        await db.insert_knowledge_entity("ENT-new", "新实体", "", [])
        await db._conn.commit()
        assert await _fts_count(db) == 2
        removed = await db.cleanup_stale(days=30)
        assert removed == 1, "应清理 1 条旧实体"
        assert await _fts_count(db) == 1, "cleanup 后 FTS 应剩 1 条"
        assert "新实体" in await _fts_names(db, "新实体")
        assert "旧实体" not in await _fts_names(db, "旧实体")
    finally:
        await conn.close()


async def test_upsert_syncs_fts():
    conn, db = await _make_db()
    try:
        await db.upsert_knowledge_entity("小妲", "人物", ["a"])
        assert await _fts_count(db) == 1
        # 再次 upsert（冲突更新）FTS 仍一致，不重复
        await db.upsert_knowledge_entity("小妲", "人物", ["b"])
        assert await _fts_count(db) == 1, "upsert 冲突更新后 FTS 不应重复"
        assert "小妲" in await _fts_names(db, "小妲")
    finally:
        await conn.close()


async def main():
    tests = [
        test_insert_syncs_fts,
        test_merge_update_no_error_and_fts_intact,
        test_merge_new_entity_syncs_fts,
        test_delete_removes_fts,
        test_cleanup_stale_removes_fts,
        test_upsert_syncs_fts,
    ]
    passed = 0
    for t in tests:
        try:
            await t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} 通过")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
