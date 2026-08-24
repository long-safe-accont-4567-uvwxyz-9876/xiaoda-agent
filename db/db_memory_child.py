"""MemoryDB 的父子 chunk 方法组 —— 拆分自 db/db_memory.py。

Mixin 组合：MemoryDB 继承 ChildChunkMixin 获得父子 chunk RAG 优化方法。
仅依赖实例属性 self._conn / self._read_conn() 与 db_memory_utils 纯函数，
无循环依赖。
"""
from __future__ import annotations

import asyncio

from loguru import logger

from db.db_memory_utils import _scope_where, _sql_placeholders


class ChildChunkMixin:
    """父子 chunk RAG 优化方法组（MemoryDB 经 Mixin 组合）。"""

    # ── 父子Chunk RAG优化 ──────────────────────────────────────

    async def insert_child_chunk(self, parent_id: int, content: str, embed_content: str = "",
                                 chunk_type: str = "segment", importance: float = 0.5,
                                 overlap_hash: str = "", auto_commit: bool = True) -> int:
        """插入子chunk记录，同时写入FTS索引。返回子chunk ID。"""
        import time as _time
        cursor = await self._conn.execute(
            """INSERT INTO memory_child_chunks
               (parent_id, content, embed_content, chunk_type, importance, overlap_hash, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (parent_id, content, embed_content, chunk_type, importance, overlap_hash, _time.time()),
        )
        child_id = cursor.lastrowid
        # FTS 索引
        await self._conn.execute(
            "INSERT INTO memory_child_chunks_fts (rowid, content) VALUES (?, ?)",
            (child_id, content),
        )
        if auto_commit:
            await self._conn.commit()
        return child_id

    async def insert_child_chunks(
        self,
        parent_id: int,
        children: list[dict],
        auto_commit: bool = True,
    ) -> list[int]:
        import time as _time

        child_ids = []
        try:
            if auto_commit:
                await self._conn.execute("BEGIN")
            for child in children:
                cursor = await self._conn.execute(
                    """INSERT INTO memory_child_chunks
                       (parent_id, content, embed_content, chunk_type, importance, overlap_hash, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        parent_id,
                        child["content"],
                        child.get("embed_content", ""),
                        child.get("chunk_type", "segment"),
                        child.get("importance", 0.5),
                        child.get("overlap_hash", ""),
                        _time.time(),
                    ),
                )
                child_id = cursor.lastrowid
                child_ids.append(child_id)
                await self._conn.execute(
                    "INSERT INTO memory_child_chunks_fts (rowid, content) VALUES (?, ?)",
                    (child_id, child["content"]),
                )
            if auto_commit:
                await self._conn.commit()
            return child_ids
        except BaseException:
            if auto_commit:
                await asyncio.shield(self._conn.rollback())
            raise

    async def search_child_fts(
        self, query: str, limit: int = 20, scope: object | None = None
    ) -> list[dict]:
        """子chunk FTS5全文检索，返回包含 parent_id 的记录列表。

        2026-08-08 阻塞根因修复：原用主写连接 _conn 执行 SELECT，后台写事务
        （instinct 提取 / 记忆索引等 write_transaction）长时间占用写连接时，
        child 通道 FTS 查询排队 7-8s → 检索整体超时。改走读连接池，
        WAL 模式下读不被写阻塞，与 fts/vec 通道行为一致。
        """
        from db.fts_utils import _build_fts_query
        fts_query = _build_fts_query(query)
        if not fts_query:
            return []
        try:
            where = "memory_child_chunks_fts MATCH ?"
            params: list = [fts_query]
            join = ""
            if scope is not None:
                join = " JOIN episodic_memories em ON em.id = mc.parent_id"
                scope_where, scope_params = _scope_where(
                    scope, table="em", include_archived_filter=True
                )
                where += scope_where
                params.extend(scope_params)
            params.append(limit)
            cursor = await self._read_conn().execute(
                f"""SELECT mc.id, mc.parent_id, mc.content, mc.chunk_type, mc.importance,
                          bm25(memory_child_chunks_fts) as score
                   FROM memory_child_chunks_fts fts
                   JOIN memory_child_chunks mc ON fts.rowid = mc.id
                   {join}
                   WHERE {where}
                   ORDER BY score
                   LIMIT ?""",
                params,
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("db_memory.child_fts_search_failed", error=str(e))
            return []

    async def get_child_parent_ids(self, child_ids: list[int]) -> list[int]:
        """根据子chunk ID列表获取去重后的父chunk ID列表。"""
        if not child_ids:
            return []
        placeholders = _sql_placeholders(child_ids)
        cursor = await self._read_conn().execute(
            f"SELECT DISTINCT parent_id FROM memory_child_chunks WHERE id IN ({placeholders})",
            child_ids,
        )
        rows = await cursor.fetchall()
        return [r["parent_id"] for r in rows]

    async def get_children_by_parent(self, parent_id: int) -> list[dict]:
        """获取指定父chunk的所有子chunk。"""
        cursor = await self._read_conn().execute(
            "SELECT * FROM memory_child_chunks WHERE parent_id=? ORDER BY id",
            (parent_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def delete_child_chunks(self, child_ids: list[int], auto_commit: bool = True) -> None:
        if not child_ids:
            return
        placeholders = _sql_placeholders(child_ids)
        await self._conn.execute(
            f"DELETE FROM memory_child_chunks_fts WHERE rowid IN ({placeholders})",
            child_ids,
        )
        await self._conn.execute(
            f"DELETE FROM memory_child_chunks WHERE id IN ({placeholders})",
            child_ids,
        )
        if auto_commit:
            await self._conn.commit()

    async def delete_children_by_parent(self, parent_id: int) -> int:
        """删除指定父chunk的所有子chunk（含FTS索引）。返回删除数量。"""
        # 先删FTS
        cursor = await self._read_conn().execute(
            "SELECT id FROM memory_child_chunks WHERE parent_id=?", (parent_id,)
        )
        rows = await cursor.fetchall()
        child_ids = [r["id"] for r in rows]
        if child_ids:
            placeholders = _sql_placeholders(child_ids)
            await self._conn.execute(
                f"DELETE FROM memory_child_chunks_fts WHERE rowid IN ({placeholders})",
                child_ids,
            )
            await self._conn.execute(
                "DELETE FROM memory_child_chunks WHERE parent_id=?", (parent_id,)
            )
            await self._conn.commit()
        return len(child_ids)
