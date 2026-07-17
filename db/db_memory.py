from typing import Any
import time
import aiosqlite
from loguru import logger


class MemoryDB:
    """管理情景记忆、画像等记忆数据的读写。"""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        conn.row_factory = aiosqlite.Row

    async def commit(self) -> None:
        await self._conn.commit()

    async def migrate_add_source_column(self) -> None:
        """迁移：为旧库的 episodic_memories 表添加 source 列（已存在则忽略）"""
        try:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN source TEXT DEFAULT 'user'"
            )
            await self._conn.commit()
        except Exception as e:
            # 列已存在时忽略
            logger.debug(f"db_memory.migrate_add_source_column skipped: {e}")

    async def insert_episodic_memory(self, summary: str, importance: float = 0.5,
                                      emotion_label: str = "", session_id: str = "user",
                                      embedding_id: int = -1, auto_commit: bool = True,
                                      source: str = "user",
                                      scope: Any | None = None,
                                      is_raw: int = 0) -> Any:
        """插入情景记忆。

        Args:
            scope: Scope 对象（mem0 SPEC 优化）。传入时使用 scope 的 user_id/session_id/agent_id。
            is_raw: 0=提炼知识（允许 UPDATE/DELETE），1=原始记录（append-only）。
        """
        # scope 优先级高于单独的 session_id 参数
        if scope is not None:
            user_id = scope.user_id
            agent_id = scope.agent_id
            session_id = scope.session_id
        else:
            user_id = "default"
            agent_id = "xiaoda"
        cursor = await self._conn.execute(
            """INSERT INTO episodic_memories
               (timestamp, summary, importance, emotion_label, session_id,
                embedding_id, source, user_id, agent_id, is_raw)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), summary, importance, emotion_label, session_id,
             embedding_id, source, user_id, agent_id, is_raw),
        )
        mem_id = cursor.lastrowid
        if auto_commit:
            await self._conn.commit()
        # 同步写入 FTS 索引
        try:
            from db.fts_utils import _tokenize_for_fts
            tokenized = _tokenize_for_fts(summary)
            if tokenized.strip():
                await self._conn.execute(
                    "INSERT INTO episodic_memory_fts(id, summary_index) VALUES(?, ?)",
                    (mem_id, tokenized),
                )
                if auto_commit:
                    await self._conn.commit()
        except Exception as e:
            from loguru import logger
            logger.debug("db_memory.fts_insert_failed", error=str(e))
        return mem_id

    async def get_memory_by_id(self, memory_id: int) -> dict | None:
        cursor = await self._conn.execute(
            "SELECT * FROM episodic_memories WHERE id=?", (memory_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_memories_by_ids(self, ids: list[int]) -> list[dict]:
        """批量获取记忆记录（向量检索后批量 JOIN 主表，消除 N 次逐条查询）"""
        if not ids:
            return []
        # 参数化占位符，防止 SQL 注入
        placeholders = ",".join("?" * len(ids))
        cursor = await self._conn.execute(
            f"SELECT * FROM episodic_memories WHERE id IN ({placeholders})",
            ids,
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def update_emotion_label(self, mem_id: int, label: str) -> None:
        await self._conn.execute(
            "UPDATE episodic_memories SET emotion_label = ? WHERE id = ?",
            (label, mem_id),
        )
        await self._conn.commit()

    async def update_distill_status(self, mem_id: int, status: str) -> None:
        """更新蒸馏状态字段（不污染 emotion_label）。"""
        await self._conn.execute(
            "UPDATE episodic_memories SET distill_status = ? WHERE id = ?",
            (status, mem_id),
        )
        await self._conn.commit()

    async def mark_permanent(self, mem_id: int) -> None:
        """将记忆标记为永久牢记。"""
        await self._conn.execute(
            "UPDATE episodic_memories SET is_permanent = 1 WHERE id = ?",
            (mem_id,),
        )
        await self._conn.commit()

    async def update_memory_summary(self, mem_id: int, new_summary: str) -> None:
        await self._conn.execute(
            "UPDATE episodic_memories SET summary = ? WHERE id = ?",
            (new_summary, mem_id),
        )
        try:
            from db.fts_utils import _tokenize_for_fts
            tokenized = _tokenize_for_fts(new_summary)
            if tokenized.strip():
                await self._conn.execute(
                    "DELETE FROM episodic_memory_fts WHERE id = ?",
                    (mem_id,),
                )
                await self._conn.execute(
                    "INSERT INTO episodic_memory_fts(id, summary_index) VALUES(?, ?)",
                    (mem_id, tokenized),
                )
        except Exception as e:
            from loguru import logger
            logger.debug("db_memory.fts_sync_on_summary_update_failed", error=str(e))
        await self._conn.commit()

    async def update_fallback_raw(self, mem_id: int, new_summary: str, label: str,
                                    distill_status: str = "") -> None:
        if distill_status:
            await self._conn.execute(
                "UPDATE episodic_memories SET summary = ?, emotion_label = ?, distill_status = ? WHERE id = ?",
                (new_summary, label, distill_status, mem_id),
            )
        else:
            await self._conn.execute(
                "UPDATE episodic_memories SET summary = ?, emotion_label = ? WHERE id = ?",
                (new_summary, label, mem_id),
            )
        try:
            from db.fts_utils import _tokenize_for_fts
            tokenized = _tokenize_for_fts(new_summary)
            if tokenized.strip():
                await self._conn.execute(
                    "DELETE FROM episodic_memory_fts WHERE id = ?",
                    (mem_id,),
                )
                await self._conn.execute(
                    "INSERT INTO episodic_memory_fts(id, summary_index) VALUES(?, ?)",
                    (mem_id, tokenized),
                )
        except Exception as e:
            from loguru import logger
            logger.debug("db_memory.fts_sync_on_fallback_failed", error=str(e))
        await self._conn.commit()

    async def increment_access_count(self, memory_id: int, auto_commit: bool = True) -> None:
        """递增记忆访问计数（检索强化）"""
        await self._conn.execute(
            "UPDATE episodic_memories SET access_count = access_count + 1 WHERE id = ?",
            (memory_id,),
        )
        if auto_commit:
            await self._conn.commit()

    async def batch_increment_access_count(self, memory_ids: list[int],
                                            auto_commit: bool = True) -> None:
        """批量递增记忆访问计数（消除 N+1：单条 UPDATE + IN 子句）。

        行为等价于对每个 id 调用 increment_access_count(id, auto_commit=False)，
        但只发一次 SQL。所有 id 统一 +1（与单条版本语义一致）。
        """
        if not memory_ids:
            return
        placeholders = ",".join("?" * len(memory_ids))
        await self._conn.execute(
            f"UPDATE episodic_memories SET access_count = access_count + 1 "
            f"WHERE id IN ({placeholders})",
            memory_ids,
        )
        if auto_commit:
            await self._conn.commit()

    async def archive_memory(self, memory_id: int) -> None:
        """归档记忆（标记为已归档，不删除）"""
        await self._conn.execute(
            "UPDATE episodic_memories SET session_id = 'archived' WHERE id = ?",
            (memory_id,),
        )
        await self._conn.commit()

    async def archive_memories_batch(self, memory_ids: list[int]) -> None:
        """批量归档记忆（标记为已归档，不删除）。

        使用 IN 子句一次 UPDATE，消除 N+1 查询。
        与 archive_memory 行为等价（仅 DB UPDATE，无其他副作用）。
        """
        if not memory_ids:
            return
        placeholders = ",".join("?" * len(memory_ids))
        await self._conn.execute(
            f"UPDATE episodic_memories SET session_id = 'archived' WHERE id IN ({placeholders})",
            memory_ids,
        )
        await self._conn.commit()

    async def get_recent_conversations(self, limit: int = 20, user_id: str = "") -> Any:
        """获取最近的对话记录。支持按 user_id 过滤（群聊场景下隔离不同用户的历史）。"""
        if user_id:
            cursor = await self._conn.execute(
                """SELECT * FROM conversation_logs
                   WHERE user_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (user_id, limit),
            )
        else:
            cursor = await self._conn.execute(
                """SELECT * FROM conversation_logs
                   ORDER BY id DESC LIMIT ?""",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in reversed(rows)]

    async def get_conversations_by_time_range(self, start_ts: float, end_ts: float,
                                               user_id: str = "", limit: int = 50) -> list[dict]:
        """按时间范围查询 conversation_logs 原始对话。用于时间型回忆查询。"""
        params: list = [start_ts, end_ts]
        where = "WHERE timestamp >= ? AND timestamp <= ?"
        if user_id:
            where += " AND user_id = ?"
            params.append(user_id)
        params.append(limit)
        cursor = await self._conn.execute(
            f"""SELECT timestamp, user_message, assistant_reply FROM conversation_logs
                {where} ORDER BY timestamp ASC LIMIT ?""",
            params,
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def search_memories_by_importance(self, min_importance: float = 0.3, limit: int = 10) -> Any:
        cursor = await self._conn.execute(
            """SELECT * FROM episodic_memories
               WHERE importance >= ?
               ORDER BY timestamp DESC LIMIT ?""",
            (min_importance, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def search_memories_by_importance_scoped(self, min_importance: float = 0.3,
                                                     limit: int = 10,
                                                     scope: Any | None = None) -> list[dict]:
        """按重要性排序检索 + scope 过滤（mem0 SPEC 优化）。

        Args:
            scope: Scope 对象。None 时退回无 scope 版本。
        """
        if scope is None:
            return await self.search_memories_by_importance(min_importance, limit)
        cursor = await self._conn.execute(
            """SELECT * FROM episodic_memories
               WHERE importance >= ?
                 AND user_id = ? AND agent_id = ?
                 AND session_id != 'archived'
               ORDER BY timestamp DESC LIMIT ?""",
            (min_importance, scope.user_id, scope.agent_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def search_memories_fts(self, query: str, limit: int = 20) -> list[dict]:
        """FTS5 BM25 全文检索"""
        from db.fts_utils import _build_fts_query
        fts_query = _build_fts_query(query)
        if not fts_query:
            return []
        try:
            cursor = await self._conn.execute(
                """SELECT em.*, bm25(episodic_memory_fts) AS score
                   FROM episodic_memory_fts
                   JOIN episodic_memories em ON em.id = episodic_memory_fts.id
                   WHERE episodic_memory_fts MATCH ?
                   ORDER BY score ASC, em.importance DESC, em.timestamp DESC
                   LIMIT ?""",
                (fts_query, limit),
            )
            rows = await cursor.fetchall()
            results = []
            for r in rows:
                d = dict(r)
                # bm25 returns negative values, convert to positive score
                d["score"] = -d.get("score", 0)
                results.append(d)
            return results
        except Exception as e:
            from loguru import logger
            logger.warning("db_memory.fts_search_failed", error=str(e))
            return []

    async def search_memories_fts_scoped(self, query: str, scope: Any,
                                          limit: int = 20,
                                          is_raw: int | None = None) -> list[dict]:
        """FTS5 全文检索 + scope 过滤（mem0 SPEC 优化）。

        Args:
            scope: Scope 对象
            limit: 返回条数上限
            is_raw: None=不限, 0=只查提炼知识, 1=只查原始记录
        """
        from db.fts_utils import _build_fts_query
        fts_query = _build_fts_query(query)
        if not fts_query:
            return []
        try:
            where_extra = ""
            params: list = [fts_query, scope.user_id, scope.agent_id]
            if is_raw is not None:
                where_extra = " AND em.is_raw = ?"
                params.append(is_raw)
            params.append(limit)
            cursor = await self._conn.execute(
                f"""SELECT em.*, bm25(episodic_memory_fts) AS score
                   FROM episodic_memory_fts
                   JOIN episodic_memories em ON em.id = episodic_memory_fts.id
                   WHERE episodic_memory_fts MATCH ?
                     AND em.user_id = ? AND em.agent_id = ?
                     AND em.session_id != 'archived'{where_extra}
                   ORDER BY score ASC, em.importance DESC, em.timestamp DESC
                   LIMIT ?""",
                params,
            )
            rows = await cursor.fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d["score"] = -d.get("score", 0)
                results.append(d)
            return results
        except Exception as e:
            logger.warning("db_memory.fts_scoped_search_failed", error=str(e))
            return []

    async def search_memories_by_time_scoped(self, start_ts: float, end_ts: float,
                                              scope: Any, limit: int = 20,
                                              is_raw: int | None = None) -> list[dict]:
        """按时间范围检索记忆 + scope 过滤（mem0 SPEC 优化）。

        Args:
            scope: Scope 对象
            is_raw: None=不限, 0=只查提炼知识, 1=只查原始记录
        """
        try:
            where_extra = ""
            params: list = [start_ts, end_ts, scope.user_id, scope.agent_id]
            if is_raw is not None:
                where_extra = " AND is_raw = ?"
                params.append(is_raw)
            params.append(limit)
            cursor = await self._conn.execute(
                f"""SELECT * FROM episodic_memories
                   WHERE timestamp >= ? AND timestamp < ?
                     AND user_id = ? AND agent_id = ?
                     AND session_id != 'archived'{where_extra}
                   ORDER BY timestamp DESC LIMIT ?""",
                params,
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("db_memory.time_scoped_search_failed", error=str(e))
            return []

    async def search_memories_vec_scoped(self, memory_ids: list[int], scope: Any,
                                          limit: int = 50,
                                          is_raw: int | None = None) -> list[dict]:
        """向量检索结果 + scope 过滤（从 memory_ids 中筛选符合 scope 的记录）。

        Args:
            memory_ids: 向量检索返回的 memory_id 列表
            scope: Scope 对象
            is_raw: None=不限, 0=只查提炼知识, 1=只查原始记录
        """
        if not memory_ids:
            return []
        try:
            placeholders = ",".join("?" * len(memory_ids))
            where_extra = ""
            params: list = list(memory_ids) + [scope.user_id, scope.agent_id]
            if is_raw is not None:
                where_extra = " AND is_raw = ?"
                params.append(is_raw)
            params.append(limit)
            cursor = await self._conn.execute(
                f"""SELECT * FROM episodic_memories
                   WHERE id IN ({placeholders})
                     AND user_id = ? AND agent_id = ?
                     AND session_id != 'archived'{where_extra}
                   LIMIT ?""",
                params,
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("db_memory.vec_scoped_search_failed", error=str(e))
            return []

    async def get_episodic_count_scoped(self, scope: Any, is_raw: int | None = None) -> int:
        """获取 scope 内的记忆总数（用于冷启动档位判断）"""
        try:
            where_extra = ""
            params: list = [scope.user_id, scope.agent_id]
            if is_raw is not None:
                where_extra = " AND is_raw = ?"
                params.append(is_raw)
            cursor = await self._conn.execute(
                f"SELECT COUNT(*) as cnt FROM episodic_memories "
                f"WHERE user_id = ? AND agent_id = ? "
                f"AND session_id != 'archived'{where_extra}",
                params,
            )
            row = await cursor.fetchone()
            return row["cnt"] if row else 0
        except Exception as e:
            logger.warning("db_memory.count_scoped_failed", error=str(e))
            return 0

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
            cursor = await self._conn.execute(
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
            cursor = await self._conn.execute(
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
            cursor = await self._conn.execute(
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
            cursor = await self._conn.execute(
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
            placeholders = ",".join("?" * len(entity_names))
            where_raw = ""
            params: list = list(entity_names) + [scope.user_id, scope.agent_id]
            if is_raw is not None:
                where_raw = " AND em.is_raw = ?"
                params.append(is_raw)
            params.append(limit)
            cursor = await self._conn.execute(
                f"""SELECT DISTINCT em.* FROM entity_memory_links eml
                   JOIN memory_entities me ON me.id = eml.entity_id
                   JOIN episodic_memories em ON em.id = eml.memory_id
                   WHERE me.name IN ({placeholders})
                     AND em.user_id = ? AND em.agent_id = ?{where_raw}
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
            cursor = await self._conn.execute(
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

    async def search_memories_by_entities(self, entity_names: list[str],
                                            limit: int = 5) -> list[dict]:
        """按实体反查情景记忆（entities 字段为 JSON 数组字符串）。

        I6: KG 召回通道 — 让 KG 关联的实体能反查到对应记忆，参与 RAG 候选池。
        """
        if not entity_names:
            return []
        try:
            import json
            conditions = " OR ".join(["entities LIKE ?" for _ in entity_names])
            params = [f'%"{e}"%' for e in entity_names]
            cursor = await self._conn.execute(
                f"""SELECT * FROM episodic_memories
                   WHERE session_id != 'archived' AND ({conditions})
                   ORDER BY importance DESC, timestamp DESC LIMIT ?""",
                [*params, limit],
            )
            rows = await cursor.fetchall()
            results = []
            for r in rows:
                d = dict(r)
                # 解析 entities JSON 字符串为列表，供后续 KG 评分复用
                raw = d.get("entities", "")
                if isinstance(raw, str) and raw:
                    try:
                        d["entity_list"] = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        d["entity_list"] = []
                else:
                    d["entity_list"] = raw if isinstance(raw, list) else []
                results.append(d)
            return results
        except Exception as e:
            logger.warning("db_memory.entity_search_failed", error=str(e))
            return []

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
        if not entity_names:
            return []
        try:
            import json
            conditions = " OR ".join(["entities LIKE ?" for _ in entity_names])
            params = [f'%"{e}"%' for e in entity_names]
            params.extend([scope.user_id, scope.agent_id])
            cursor = await self._conn.execute(
                f"""SELECT * FROM episodic_memories
                   WHERE session_id != 'archived'
                     AND ({conditions})
                     AND user_id = ? AND agent_id = ?
                   ORDER BY importance DESC, timestamp DESC LIMIT ?""",
                [*params, limit],
            )
            rows = await cursor.fetchall()
            results = []
            for r in rows:
                d = dict(r)
                raw = d.get("entities", "")
                if isinstance(raw, str) and raw:
                    try:
                        d["entity_list"] = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        d["entity_list"] = []
                else:
                    d["entity_list"] = raw if isinstance(raw, list) else []
                results.append(d)
            return results
        except Exception as e:
            logger.warning("db_memory.entity_search_scoped_failed", error=str(e))
            return []

    async def get_all_memories(self, limit: int = 100) -> Any:
        """获取所有活跃记忆（排除已归档）"""
        cursor = await self._conn.execute(
            "SELECT * FROM episodic_memories WHERE session_id != 'archived' ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def delete_memory(self, memory_id: int, auto_commit: bool = True) -> None:
        await self._conn.execute("DELETE FROM episodic_memories WHERE id=?", (memory_id,))
        # 同步删除 FTS 记录
        try:
            await self._conn.execute("DELETE FROM episodic_memory_fts WHERE id=?", (memory_id,))
        except Exception as e:
            from loguru import logger
            logger.debug("db_memory.fts_delete_failed", error=str(e))
        if auto_commit:
            await self._conn.commit()

    async def delete_memories_batch(self, memory_ids: list[int], auto_commit: bool = True) -> None:
        """批量删除记忆，同步批量删除 FTS 索引（消除 N+1 查询）。

        保留 delete_memory 的 FTS 副作用：批量删除主表后批量删除 FTS 记录。
        """
        if not memory_ids:
            return
        placeholders = ",".join("?" * len(memory_ids))
        await self._conn.execute(
            f"DELETE FROM episodic_memories WHERE id IN ({placeholders})",
            memory_ids,
        )
        # 同步批量删除 FTS 记录（保留 delete_memory 的副作用）
        try:
            await self._conn.execute(
                f"DELETE FROM episodic_memory_fts WHERE id IN ({placeholders})",
                memory_ids,
            )
        except Exception as e:
            logger.debug("db_memory.fts_delete_batch_failed", error=str(e))
        if auto_commit:
            await self._conn.commit()

    async def delete_memory_with_vector(self, memory_id: int, vector_store: Any=None, auto_commit: bool = True) -> None:
        """统一删除：先删向量，再删记忆"""
        if vector_store:
            try:
                await vector_store.delete(memory_id)
            except Exception as e:
                logger.error("db_memory.vec_delete_failed", memory_id=memory_id, error=str(e))
                raise
        await self.delete_memory(memory_id, auto_commit=auto_commit)

    async def get_episodic_recent(self, limit: int = 50) -> Any:
        cursor = await self._conn.execute(
            """SELECT * FROM episodic_memories
               ORDER BY timestamp DESC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_episodic_count(self) -> int:
        cursor = await self._conn.execute("SELECT COUNT(*) as cnt FROM episodic_memories")
        row = await cursor.fetchone()
        return row["cnt"] if row else 0

    async def get_unmigrated_memories(self, limit: int = 50) -> list[dict]:
        """获取未迁移到 concept_nodes 的记忆"""
        async with self._conn.execute(
            """SELECT em.id, em.summary FROM episodic_memories em
               WHERE em.id NOT IN (SELECT source_mem_id FROM concept_nodes
                                   WHERE source_mem_id IS NOT NULL)
               ORDER BY em.timestamp ASC LIMIT ?""",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [{"id": r["id"], "summary": r["summary"]} for r in rows]

    async def search_memories_by_time(self, start_ts: float, end_ts: float, limit: int = 20) -> list[dict]:
        """按时间范围检索记忆（用于"昨天/上周发生了什么"这类查询）。

        Args:
            start_ts: 起始时间戳（秒）
            end_ts: 结束时间戳（秒）
            limit: 返回条数上限
        """
        cursor = await self._conn.execute(
            """SELECT * FROM episodic_memories
               WHERE timestamp >= ? AND timestamp < ?
               ORDER BY timestamp DESC LIMIT ?""",
            (start_ts, end_ts, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def search_memories_fts_with_time(self, query: str, start_ts: float,
                                             end_ts: float, limit: int = 10) -> list[dict]:
        """FTS 全文检索 + 时间范围过滤（混合查询）。"""
        from db.fts_utils import _build_fts_query
        fts_query = _build_fts_query(query)
        if not fts_query:
            return []
        try:
            cursor = await self._conn.execute(
                """SELECT em.*, bm25(episodic_memory_fts) AS score
                   FROM episodic_memory_fts
                   JOIN episodic_memories em ON em.id = episodic_memory_fts.id
                   WHERE episodic_memory_fts MATCH ?
                     AND em.timestamp >= ? AND em.timestamp < ?
                   ORDER BY score ASC, em.importance DESC, em.timestamp DESC
                   LIMIT ?""",
                (fts_query, start_ts, end_ts, limit),
            )
            rows = await cursor.fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d["score"] = -d.get("score", 0)
                results.append(d)
            return results
        except Exception as e:
            from loguru import logger
            logger.warning("db_memory.fts_time_search_failed", error=str(e))
            return []

    async def update_memory_enrichment(self, memory_id: int, summary: str = "",
                                        entities: str = "", event_type: str = "",
                                        metadata_json: str = "", auto_commit: bool = True) -> bool:
        """后台 LLM 提取完成后，更新记忆条目的结构化字段。

        Args:
            memory_id: 记忆 ID
            summary: LLM 提取的更高质量摘要（可选，空则不更新）
            entities: 实体列表（JSON 字符串，如 '["小妲", "爸爸", "QQ"]'）
            event_type: 事件类型（如 '对话/决策/偏好/事件'）
            metadata_json: 元数据 JSON（如 '{"decision": "重启服务", "mood": "开心"}'）
        """
        try:
            sets = []
            params = []
            if summary:
                sets.append("summary = ?")
                params.append(summary)
            if entities:
                sets.append("entities = ?")
                params.append(entities)
            if event_type:
                sets.append("event_type = ?")
                params.append(event_type)
            if metadata_json:
                sets.append("metadata_json = ?")
                params.append(metadata_json)
            if not sets:
                return False
            params.append(memory_id)
            await self._conn.execute(
                f"UPDATE episodic_memories SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            if auto_commit:
                await self._conn.commit()
            # 如果 summary 更新了，同步更新 FTS 索引
            if summary:
                try:
                    from db.fts_utils import _tokenize_for_fts
                    tokens = _tokenize_for_fts(summary)
                    if tokens.strip():
                        await self._conn.execute(
                            "DELETE FROM episodic_memory_fts WHERE id = ?",
                            (memory_id,),
                        )
                        await self._conn.execute(
                            "INSERT INTO episodic_memory_fts(id, summary_index) VALUES(?, ?)",
                            (memory_id, tokens),
                        )
                        if auto_commit:
                            await self._conn.commit()
                except Exception as e:
                    from loguru import logger
                    logger.debug("db_memory.fts_update_failed", error=str(e))
            return True
        except Exception as e:
            from loguru import logger
            logger.warning("db_memory.enrichment_update_failed", error=str(e))
            return False

    async def insert_portrait(self, content: str, version: int = 1,
                               source_ids: str = "", change_log: str = "",
                               auto_commit: bool = True) -> int:
        cursor = await self._conn.execute(
            """INSERT INTO user_portrait (content, version, source_ids, change_log, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (content, version, source_ids, change_log, time.time()),
        )
        if auto_commit:
            await self._conn.commit()
        return cursor.lastrowid

    async def get_latest_portrait(self) -> dict | None:
        cursor = await self._conn.execute(
            "SELECT * FROM user_portrait ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def insert_consolidation_candidate(self, source: str, kind: str, summary: str,
                                              confidence: float = 0.5, importance: float = 0.5,
                                              metadata_json: str = "{}",
                                              auto_commit: bool = True) -> int:
        cursor = await self._conn.execute(
            """INSERT INTO consolidation_candidates
               (timestamp, source, kind, summary, confidence, importance, status, metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (time.time(), source, kind, summary, confidence, importance, metadata_json, time.time()),
        )
        if auto_commit:
            await self._conn.commit()
        return cursor.lastrowid

    async def mark_candidate_applied(self, candidate_id: int, target_memory_id: int,
                                      auto_commit: bool = True) -> None:
        await self._conn.execute(
            "UPDATE consolidation_candidates SET status='applied', target_memory_id=? WHERE id=?",
            (target_memory_id, candidate_id),
        )
        if auto_commit:
            await self._conn.commit()

    async def update_rag_status(self, memory_id: int, rag_status: str, rag_synced_at: float | None = None) -> None:
        """更新记忆的 RAG 索引状态"""
        valid_statuses = ('pending', 'indexed', 'failed', 'excluded')
        if rag_status not in valid_statuses:
            raise ValueError(f"rag_status must be one of {valid_statuses}, got '{rag_status}'")
        if rag_status == 'indexed' and rag_synced_at is None:
            rag_synced_at = time.time()
        if rag_synced_at is not None:
            await self._conn.execute(
                "UPDATE episodic_memories SET rag_status=?, rag_synced_at=? WHERE id=?",
                (rag_status, rag_synced_at, memory_id),
            )
        else:
            await self._conn.execute(
                "UPDATE episodic_memories SET rag_status=? WHERE id=?",
                (rag_status, memory_id),
            )
        await self._conn.commit()

    async def update_doc_id(self, memory_id: int, doc_id: str) -> None:
        """更新记忆关联的文档 ID"""
        await self._conn.execute(
            "UPDATE episodic_memories SET doc_id=? WHERE id=?",
            (doc_id, memory_id),
        )
        await self._conn.commit()

    async def get_pending_memories(self, limit: int = 100) -> list[dict]:
        """查询待索引的 RAG 记忆（rag_status='pending'），按时间升序"""
        cursor = await self._conn.execute(
            """SELECT id, timestamp, summary, importance FROM episodic_memories
               WHERE rag_status='pending'
               ORDER BY timestamp ASC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── P3 记忆蒸馏相关 ──────────────────────────────────────

    async def get_episodic_count_undistilled(self) -> int:
        """统计未蒸馏的情景记忆数量（distilled=0）"""
        try:
            cursor = await self._conn.execute(
                "SELECT COUNT(*) as cnt FROM episodic_memories WHERE distilled=0"
            )
            row = await cursor.fetchone()
            return row["cnt"] if row else 0
        except Exception as e:
            # 旧库可能没有 distilled 列，降级返回总计数
            logger.debug("db_memory.undistilled_count_failed", error=str(e))
            return await self.get_episodic_count()

    async def get_distill_candidates(self, limit: int = 30) -> list[dict]:
        """查询最旧的未蒸馏记忆（按时间升序），用于蒸馏压缩"""
        try:
            cursor = await self._conn.execute(
                """SELECT id, timestamp, summary, importance FROM episodic_memories
                   WHERE distilled=0
                   ORDER BY timestamp ASC LIMIT ?""",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("db_memory.distill_candidates_failed", error=str(e))
            return []

    async def mark_memories_distilled(self, memory_ids: list[int],
                                       auto_commit: bool = True) -> None:
        """将指定记忆标记为已蒸馏（distilled=1），保留不删除"""
        if not memory_ids:
            return
        placeholders = ",".join("?" * len(memory_ids))
        try:
            await self._conn.execute(
                f"UPDATE episodic_memories SET distilled=1 WHERE id IN ({placeholders})",
                memory_ids,
            )
            if auto_commit:
                await self._conn.commit()
        except Exception as e:
            logger.warning("db_memory.mark_distilled_failed", error=str(e))

    async def insert_memory_summary(self, summary_text: str, memory_count: int,
                                     auto_commit: bool = True) -> int:
        """写入一条蒸馏摘要记录，返回摘要 id"""
        cursor = await self._conn.execute(
            """INSERT INTO memory_summaries (summary_text, created_at, memory_count)
               VALUES (?, ?, ?)""",
            (summary_text, time.time(), memory_count),
        )
        if auto_commit:
            await self._conn.commit()
        return cursor.lastrowid

    async def get_memory_summaries(self, limit: int = 5) -> list[dict]:
        """获取最近的蒸馏摘要（按时间降序）"""
        try:
            cursor = await self._conn.execute(
                """SELECT id, summary_text, created_at, memory_count
                   FROM memory_summaries
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("db_memory.get_summaries_failed", error=str(e))
            return []

    async def get_recent_undistilled(self, limit: int = 20) -> list[dict]:
        """获取最近的未蒸馏记忆（按时间降序），用于构建记忆提示"""
        try:
            cursor = await self._conn.execute(
                """SELECT id, timestamp, summary, importance FROM episodic_memories
                   WHERE distilled=0
                   ORDER BY timestamp DESC LIMIT ?""",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            # 旧库可能没有 distilled 列，降级返回所有最近记忆
            logger.debug("db_memory.recent_undistilled_failed", error=str(e))
            return await self.get_episodic_recent(limit=limit)

    # ── 主动检索 B/C：定时回忆笔记 + 情绪触发检索 ────────────────

    async def search_memories_by_emotion(self, emotion_labels: list[str],
                                          limit: int = 5) -> list[dict]:
        """按情绪标签检索记忆（用于情绪触发主动检索）。

        Args:
            emotion_labels: 目标情绪标签列表（如 ["喜悦", "happy"]）。
                            DB 中 emotion_label 列可能存中文或英文，调用方应同时传入两种。
            limit: 返回条数上限

        Returns:
            匹配的记忆列表，按 importance DESC, timestamp DESC 排序
        """
        if not emotion_labels:
            return []
        # 防注入：标签是有限集合，但仍做白名单校验
        clean_labels = [str(line).strip() for line in emotion_labels if str(line).strip()]
        if not clean_labels:
            return []
        try:
            placeholders = ",".join("?" * len(clean_labels))
            cursor = await self._conn.execute(
                f"""SELECT * FROM episodic_memories
                    WHERE emotion_label IN ({placeholders})
                      AND session_id != 'archived'
                    ORDER BY importance DESC, timestamp DESC LIMIT ?""",
                (*clean_labels, limit),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("db_memory.search_by_emotion_failed", error=str(e))
            return []

    async def search_memories_by_emotion_scoped(self, emotion_labels: list[str],
                                                  limit: int = 5,
                                                  scope: Any | None = None) -> list[dict]:
        """按情绪标签检索记忆 + scope 过滤（mem0 SPEC 优化）。

        Args:
            emotion_labels: 目标情绪标签列表
            limit: 返回条数上限
            scope: Scope 对象。None 时退回无 scope 版本。
        """
        if scope is None:
            return await self.search_memories_by_emotion(emotion_labels, limit)
        if not emotion_labels:
            return []
        clean_labels = [str(line).strip() for line in emotion_labels if str(line).strip()]
        if not clean_labels:
            return []
        try:
            placeholders = ",".join("?" * len(clean_labels))
            cursor = await self._conn.execute(
                f"""SELECT * FROM episodic_memories
                    WHERE emotion_label IN ({placeholders})
                      AND session_id != 'archived'
                      AND user_id = ? AND agent_id = ?
                    ORDER BY importance DESC, timestamp DESC LIMIT ?""",
                (*clean_labels, scope.user_id, scope.agent_id, limit),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("db_memory.search_by_emotion_scoped_failed", error=str(e))
            return []

    async def get_high_importance_since(self, start_ts: float,
                                         min_importance: float = 0.6,
                                         limit: int = 50) -> list[dict]:
        """获取自 start_ts 起、重要性 >= min_importance 的记忆（按重要性降序）。

        供定时回忆任务筛选用：单次 SQL 完成时间窗 + 重要性组合查询，
        避免在 Python 层二次过滤。
        """
        try:
            cursor = await self._conn.execute(
                """SELECT * FROM episodic_memories
                   WHERE timestamp >= ? AND importance >= ?
                   ORDER BY importance DESC, timestamp DESC LIMIT ?""",
                (start_ts, min_importance, limit),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("db_memory.get_high_importance_since_failed", error=str(e))
            return []

    async def insert_recall_note(self, *, window_start: float, window_end: float,
                                  summary: str, memory_count: int,
                                  min_importance: float = 0.6,
                                  source_memory_ids: str = "",
                                  title: str = "", tags: str = "",
                                  auto_commit: bool = True) -> int:
        """写入一条定时回忆笔记。

        Args:
            window_start/end: 该笔记覆盖的时间窗（秒级时间戳）
            summary: LLM 蒸馏后的回忆摘要
            memory_count: 参与整理的源记忆条数
            source_memory_ids: 逗号分隔的源记忆 ID 列表（便于追溯）
            title/tags: 可选的标题和标签（便于检索）
        """
        try:
            cursor = await self._conn.execute(
                """INSERT INTO memory_recall_notes
                   (created_at, window_start, window_end, min_importance,
                    source_memory_ids, memory_count, title, summary, tags)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (time.time(), window_start, window_end, min_importance,
                 source_memory_ids, memory_count, title, summary, tags),
            )
            note_id = cursor.lastrowid
            if auto_commit:
                await self._conn.commit()
            return note_id or 0
        except Exception as e:
            logger.warning("db_memory.insert_recall_note_failed", error=str(e))
            return 0

    async def get_recent_recall_notes(self, limit: int = 5,
                                       since_ts: float = 0.0) -> list[dict]:
        """获取最近的回忆笔记（按 created_at 降序）。

        Args:
            limit: 返回条数上限
            since_ts: 若 >0，仅返回 created_at >= since_ts 的笔记（用于"最近 N 小时"）
        """
        try:
            if since_ts > 0:
                cursor = await self._conn.execute(
                    """SELECT * FROM memory_recall_notes
                       WHERE created_at >= ?
                       ORDER BY created_at DESC LIMIT ?""",
                    (since_ts, limit),
                )
            else:
                cursor = await self._conn.execute(
                    """SELECT * FROM memory_recall_notes
                       ORDER BY created_at DESC LIMIT ?""",
                    (limit,),
                )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("db_memory.get_recent_recall_notes_failed", error=str(e))
            return []

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

    async def search_child_fts(self, query: str, limit: int = 20) -> list[dict]:
        """子chunk FTS5全文检索，返回包含 parent_id 的记录列表。"""
        from db.fts_utils import _build_fts_query
        fts_query = _build_fts_query(query)
        if not fts_query:
            return []
        try:
            cursor = await self._conn.execute(
                """SELECT mc.id, mc.parent_id, mc.content, mc.chunk_type, mc.importance,
                          bm25(memory_child_chunks_fts) as score
                   FROM memory_child_chunks_fts fts
                   JOIN memory_child_chunks mc ON fts.rowid = mc.id
                   WHERE memory_child_chunks_fts MATCH ?
                   ORDER BY score
                   LIMIT ?""",
                (fts_query, limit),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            from loguru import logger
            logger.warning("db_memory.child_fts_search_failed", error=str(e))
            return []

    async def get_child_parent_ids(self, child_ids: list[int]) -> list[int]:
        """根据子chunk ID列表获取去重后的父chunk ID列表。"""
        if not child_ids:
            return []
        placeholders = ",".join("?" * len(child_ids))
        cursor = await self._conn.execute(
            f"SELECT DISTINCT parent_id FROM memory_child_chunks WHERE id IN ({placeholders})",
            child_ids,
        )
        rows = await cursor.fetchall()
        return [r["parent_id"] for r in rows]

    async def get_children_by_parent(self, parent_id: int) -> list[dict]:
        """获取指定父chunk的所有子chunk。"""
        cursor = await self._conn.execute(
            "SELECT * FROM memory_child_chunks WHERE parent_id=? ORDER BY id",
            (parent_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def delete_children_by_parent(self, parent_id: int) -> int:
        """删除指定父chunk的所有子chunk（含FTS索引）。返回删除数量。"""
        # 先删FTS
        cursor = await self._conn.execute(
            "SELECT id FROM memory_child_chunks WHERE parent_id=?", (parent_id,)
        )
        rows = await cursor.fetchall()
        child_ids = [r["id"] for r in rows]
        if child_ids:
            placeholders = ",".join("?" * len(child_ids))
            await self._conn.execute(
                f"DELETE FROM memory_child_chunks_fts WHERE rowid IN ({placeholders})",
                child_ids,
            )
            await self._conn.execute(
                "DELETE FROM memory_child_chunks WHERE parent_id=?", (parent_id,)
            )
            await self._conn.commit()
        return len(child_ids)

    async def update_fsrs_state(self, memory_id: int, difficulty: float,
                                 stability: float, phase: str,
                                 last_review: float,
                                 reinforcement_count: int,
                                 auto_commit: bool = True) -> None:
        await self._conn.execute(
            """UPDATE episodic_memories
               SET difficulty=?, stability=?, phase=?, last_review=?,
                   reinforcement_count=?
               WHERE id=?""",
            (difficulty, stability, phase, last_review, reinforcement_count, memory_id),
        )
        if auto_commit:
            await self._conn.commit()

    async def get_memories_since(self, since_ts: float,
                                  limit: int = 200) -> list[dict]:
        cursor = await self._conn.execute(
            """SELECT * FROM episodic_memories
               WHERE timestamp >= ? AND session_id != 'archived'
               ORDER BY timestamp DESC LIMIT ?""",
            (since_ts, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]