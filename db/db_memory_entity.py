"""MemoryDB 的实体方法组 —— 拆分自 db/db_memory.py。

Mixin 组合：MemoryDB 继承 EntityMixin 获得 memory_entities 表 CRUD
与实体检索方法。仅依赖 self._conn/_read_conn() + db_memory_utils 纯函数。
"""
from __future__ import annotations

import time
from typing import Any

from loguru import logger

from db.db_memory_utils import (
    _entity_like_conditions,
    _rows_to_entity_results,
    _scope_where,
    _sql_placeholders,
)


class EntityMixin:
    """memory_entities 表 CRUD + 实体检索方法组。"""

    # ── mem0 SPEC: memory_entities 表 CRUD ──

    async def insert_memory_entity(self, name: str, entity_type: str = "TOPIC",
                                    kind: str = "", observations: str = "[]",
                                    metadata_json: str = "{}",
                                    auto_commit: bool = True) -> int | None:
        """插入实体记录。重复 (name, entity_type) 返回 None。

        Args:
            name: 实体名称
            entity_type: PROPER/QUOTED/TOPIC/IDENTIFIER
            kind: 人物/地点/组织/概念/技术
            observations: JSON 数组字符串
        Returns:
            新建实体 ID，重复时返回 None
        """
        now = time.time()
        try:
            # 一次性降级：v13 迁移创建的 FTS5 触发器使用 'delete' 命令时
            # 把实体 id 当作 rowid，但 INSERT 触发器未设置 rowid，
            # 导致 memory_entities 上的 UPDATE/DELETE 全部失败。
            # 这里幂等地删除触发器，改为手动管理 FTS（与 episodic_memory_fts 模式一致）。
            for trig in ("memory_entities_fts_ai", "memory_entities_fts_ad", "memory_entities_fts_au"):
                await self._conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
            cursor = await self._conn.execute(
                """INSERT OR IGNORE INTO memory_entities
                   (name, entity_type, kind, observations, memory_count,
                    first_seen, last_seen, metadata_json)
                   VALUES (?, ?, ?, ?, 0, ?, ?, ?)""",
                (name, entity_type, kind, observations, now, now, metadata_json),
            )
            if cursor.rowcount == 0:
                if auto_commit:
                    await self._conn.commit()
                return None  # 重复插入
            entity_id = cursor.lastrowid
            # 手动写入 FTS 索引（预分词，与 episodic_memory_fts 一致）
            try:
                from db.fts_utils import _tokenize_for_fts
                tokenized = _tokenize_for_fts(name)
                if tokenized.strip():
                    await self._conn.execute(
                        "INSERT INTO memory_entities_fts(id, name_index) VALUES(?, ?)",
                        (entity_id, tokenized),
                    )
            except Exception as e:
                logger.debug("db_memory.entity_fts_insert_failed", error=str(e))
            if auto_commit:
                await self._conn.commit()
            return entity_id
        except Exception as e:
            logger.debug("db_memory.insert_entity_failed", error=str(e))
            return None

    async def find_memory_entity_by_name(self, name: str,
                                          entity_type: str = "TOPIC") -> dict | None:
        """按名称+类型查找实体"""
        try:
            cursor = await self._read_conn().execute(
                "SELECT * FROM memory_entities WHERE name=? AND entity_type=?",
                (name, entity_type),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.debug("db_memory.find_entity_failed", error=str(e))
            return None

    async def find_memory_entity_by_id(self, entity_id: int) -> dict | None:
        """按 ID 查找实体"""
        try:
            cursor = await self._read_conn().execute(
                "SELECT * FROM memory_entities WHERE id=?", (entity_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.debug("db_memory.find_entity_by_id_failed", error=str(e))
            return None

    async def search_entities_by_fts(self, query: str, limit: int = 10) -> list[dict]:
        """通过 FTS5 模糊搜索实体名称"""
        from db.fts_utils import _build_fts_query
        fts_query = _build_fts_query(query)
        if not fts_query:
            return []
        try:
            cursor = await self._read_conn().execute(
                """SELECT DISTINCT me.* FROM memory_entities_fts
                   JOIN memory_entities me ON me.id = memory_entities_fts.id
                   WHERE memory_entities_fts MATCH ?
                   LIMIT ?""",
                (fts_query, limit),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.debug("db_memory.search_entities_fts_failed", error=str(e))
            return []

    async def increment_entity_memory_count(self, entity_id: int,
                                             auto_commit: bool = True) -> None:
        """递增实体链接的记忆数"""
        try:
            await self._conn.execute(
                "UPDATE memory_entities SET memory_count = memory_count + 1 WHERE id=?",
                (entity_id,),
            )
            if auto_commit:
                await self._conn.commit()
        except Exception as e:
            logger.debug("db_memory.increment_entity_count_failed", error=str(e))

    async def update_entity_last_seen(self, entity_id: int, ts: float | None = None,
                                       auto_commit: bool = True) -> None:
        """更新实体最后出现时间"""
        if ts is None:
            ts = time.time()
        try:
            await self._conn.execute(
                "UPDATE memory_entities SET last_seen=? WHERE id=?",
                (ts, entity_id),
            )
            if auto_commit:
                await self._conn.commit()
        except Exception as e:
            logger.debug("db_memory.update_entity_last_seen_failed", error=str(e))

    async def update_memory_entity(self, entity_id: int, kind: str = "",
                                    observations: str = "",
                                    metadata_json: str = "",
                                    auto_commit: bool = True) -> bool:
        """更新实体字段"""
        try:
            sets = []
            params = []
            if kind:
                sets.append("kind = ?")
                params.append(kind)
            if observations:
                sets.append("observations = ?")
                params.append(observations)
            if metadata_json:
                sets.append("metadata_json = ?")
                params.append(metadata_json)
            if not sets:
                return False
            params.append(entity_id)
            await self._conn.execute(
                f"UPDATE memory_entities SET {', '.join(sets)} WHERE id=?",
                params,
            )
            if auto_commit:
                await self._conn.commit()
            return True
        except Exception as e:
            logger.debug("db_memory.update_entity_failed", error=str(e))
            return False

    # ── mem0 SPEC: entity_memory_links 表 CRUD ──

    async def insert_entity_memory_link(self, entity_id: int, memory_id: int,
                                         confidence: float = 1.0,
                                         auto_commit: bool = True) -> int | None:
        """插入实体↔记忆反向链接。重复 (entity_id, memory_id) 返回 None。"""
        try:
            cursor = await self._conn.execute(
                """INSERT OR IGNORE INTO entity_memory_links
                   (entity_id, memory_id, confidence, created_at)
                   VALUES (?, ?, ?, ?)""",
                (entity_id, memory_id, confidence, time.time()),
            )
            if cursor.rowcount == 0:
                return None
            link_id = cursor.lastrowid
            if auto_commit:
                await self._conn.commit()
            return link_id
        except Exception as e:
            logger.debug("db_memory.insert_link_failed", error=str(e))
            return None

    async def get_entity_memory_links(self, entity_id: int) -> list[dict]:
        """按实体 ID 查询反向链接的记忆 ID 列表"""
        try:
            cursor = await self._read_conn().execute(
                "SELECT * FROM entity_memory_links WHERE entity_id=? ORDER BY created_at DESC",
                (entity_id,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.debug("db_memory.get_links_failed", error=str(e))
            return []

    async def get_memories_by_entity_names_scoped(self, entity_names: list[str],
                                                   scope: Any,
                                                   limit: int = 10,
                                                   is_raw: int | None = 0) -> list[dict]:
        """按实体名列表 + scope 反查记忆（第5路召回核心查询）。

        Args:
            entity_names: 实体名列表
            scope: Scope 对象
            limit: 返回条数上限
            is_raw: None=不限, 0=只查提炼知识（默认）, 1=只查原始记录
        """
        if not entity_names:
            return []
        try:
            placeholders = _sql_placeholders(entity_names)
            scope_where, scope_params = _scope_where(
                scope, is_raw=is_raw, table="em", include_archived_filter=False)
            params: list = [*entity_names, *scope_params, limit]
            cursor = await self._read_conn().execute(
                f"""SELECT DISTINCT em.* FROM entity_memory_links eml
                   JOIN memory_entities me ON me.id = eml.entity_id
                   JOIN episodic_memories em ON em.id = eml.memory_id
                   WHERE me.name IN ({placeholders})
                     {scope_where}
                   ORDER BY em.importance DESC, em.timestamp DESC LIMIT ?""",
                params,
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("db_memory.entity_names_scoped_search_failed", error=str(e))
            return []

    async def get_entities_by_memory_id(self, memory_id: int) -> list[dict]:
        """按记忆 ID 查询关联的实体列表"""
        try:
            cursor = await self._read_conn().execute(
                """SELECT me.* FROM entity_memory_links eml
                   JOIN memory_entities me ON me.id = eml.entity_id
                   WHERE eml.memory_id=?""",
                (memory_id,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.debug("db_memory.get_entities_by_memory_failed", error=str(e))
            return []

    async def _search_entities_impl(self, entity_names: list[str], limit: int,
                                    user_id: str | None, agent_id: str | None,
                                    event_label: str) -> list[dict]:
        """实体反查共享实现：可选 user_id/agent_id scope 过滤。失败记 warning 返回 []。"""
        if not entity_names:
            return []
        try:
            conditions, params = _entity_like_conditions(entity_names)
            where = "session_id != 'archived' AND (" + conditions + ")"
            if user_id is not None and agent_id is not None:
                where += " AND user_id = ? AND agent_id = ?"
                params = [*params, user_id, agent_id]
            cursor = await self._read_conn().execute(
                f"""SELECT * FROM episodic_memories
                    WHERE {where}
                    ORDER BY importance DESC, timestamp DESC LIMIT ?""",
                [*params, limit],
            )
            rows = await cursor.fetchall()
            return _rows_to_entity_results(rows)
        except Exception as e:
            logger.warning(event_label, error=str(e))
            return []

    async def search_memories_by_entities(self, entity_names: list[str],
                                            limit: int = 5) -> list[dict]:
        """按实体反查情景记忆（entities 字段为 JSON 数组字符串）。

        I6: KG 召回通道 — 让 KG 关联的实体能反查到对应记忆，参与 RAG 候选池。
        """
        return await self._search_entities_impl(
            entity_names, limit, None, None, "db_memory.entity_search_failed")

    async def search_memories_by_entities_scoped(self, entity_names: list[str],
                                                   limit: int = 5,
                                                   scope: Any | None = None) -> list[dict]:
        """按实体反查情景记忆 + scope 过滤（mem0 SPEC 优化）。

        Args:
            entity_names: 实体名列表
            limit: 返回条数上限
            scope: Scope 对象。None 时退回无 scope 版本。
        """
        if scope is None:
            return await self.search_memories_by_entities(entity_names, limit)
        return await self._search_entities_impl(
            entity_names, limit, scope.user_id, scope.agent_id,
            "db_memory.entity_search_scoped_failed")
