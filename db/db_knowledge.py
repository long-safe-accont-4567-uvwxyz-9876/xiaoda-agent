import json
import time
import uuid

import aiosqlite
from loguru import logger


class KnowledgeDB:
    """管理知识实体与关系数据的持久化。"""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        conn.row_factory = aiosqlite.Row

    async def commit(self) -> None:
        await self._conn.commit()

    async def _sync_entity_fts(self, name: str) -> None:
        """应用层同步实体到 FTS 索引（替代已废弃的触发器）。

        表类型澄清（CodeRabbit finding 误判修正 2026-07-29）：
        knowledge_entities_fts 是 **contentful** FTS5 表（DDL: fts5(id UNINDEXED, name_index)，
        无 content=""），非 contentless。contentful 表支持普通 DELETE。
        历史触发器失败的原因是触发器用了 FTS5 'delete' 命令，而 'delete' 命令在含 UNINDEXED 列的
        表上始终报 SQL logic error（实测 SQLite 3.40.1）。修复：DROP 触发器，改应用层普通 DELETE+INSERT。
        CodeRabbit 误判为 contentless 表建议改用 'delete' 命令，但实测 'delete' 命令在 UNINDEXED 列上报错，
        普通 DELETE 才是正确做法。
        用 name（UNIQUE）查 rowid+id，兼容 insert/upsert 两种路径。FTS rowid 与主表 rowid 对齐。
        """
        cur = await self._conn.execute(
            "SELECT rowid, id FROM knowledge_entities WHERE name=?", (name,))
        row = await cur.fetchone()
        if row is None:
            return
        rowid, entity_id = row[0], row[1]
        await self._conn.execute(
            "DELETE FROM knowledge_entities_fts WHERE rowid=?", (rowid,))
        await self._conn.execute(
            "INSERT INTO knowledge_entities_fts(rowid, id, name_index) VALUES(?, ?, ?)",
            (rowid, entity_id, name),
        )

    async def _delete_entity_fts_by_rowid(self, rowid: int) -> None:
        """按主表 rowid 删除 FTS 索引条目（删除实体前调用）。

        contentful FTS5 表支持普通 DELETE（见 _sync_entity_fts 表类型澄清）。
        """
        await self._conn.execute(
            "DELETE FROM knowledge_entities_fts WHERE rowid=?", (rowid,))

    async def insert_knowledge_entity(self, entity_id: str, name: str,
                                       kind: str = "", observations: list | None = None,
                                       auto_commit: bool = True) -> None:
        obs_json = json.dumps(observations or [], ensure_ascii=False)
        await self._conn.execute(
            """INSERT OR IGNORE INTO knowledge_entities (id, name, kind, observations, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (entity_id, name, kind, obs_json, time.time()),
        )
        # FTS 同步（应用层维护，替代已 DROP 的触发器）
        await self._sync_entity_fts(name)
        if auto_commit:
            await self._conn.commit()

    async def get_knowledge_entity(self, name: str) -> dict | None:
        cursor = await self._conn.execute(
            "SELECT * FROM knowledge_entities WHERE name=?", (name,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def upsert_knowledge_entity(self, name: str, kind: str = "",
                                       observations: list | None = None,
                                       auto_commit: bool = True) -> None:
        obs_json = json.dumps(observations or [], ensure_ascii=False)
        now = time.time()
        entity_id = f"ENT-{uuid.uuid4().hex[:12]}"
        await self._conn.execute(
            """INSERT INTO knowledge_entities (id, name, kind, observations, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   kind=excluded.kind,
                   observations=excluded.observations,
                   updated_at=excluded.updated_at""",
            (entity_id, name, kind, obs_json, now),
        )
        # FTS 同步（应用层维护，替代已 DROP 的触发器；name 是 UNIQUE，按 name 查 rowid）
        await self._sync_entity_fts(name)
        if auto_commit:
            await self._conn.commit()

    async def insert_knowledge_relation(self, relation_id: str, from_entity: str,
                                         relation_type: str, to_entity: str,
                                         auto_commit: bool = True) -> None:
        await self._conn.execute(
            """INSERT OR REPLACE INTO knowledge_relations (id, from_entity, relation_type, to_entity, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (relation_id, from_entity, relation_type, to_entity, time.time()),
        )
        if auto_commit:
            await self._conn.commit()

    async def get_knowledge_relations(self, entity_name: str, direction: str = "both") -> list[dict]:
        if direction == "outgoing":
            cursor = await self._conn.execute(
                "SELECT * FROM knowledge_relations WHERE from_entity=?", (entity_name,)
            )
        elif direction == "incoming":
            cursor = await self._conn.execute(
                "SELECT * FROM knowledge_relations WHERE to_entity=?", (entity_name,)
            )
        else:
            cursor = await self._conn.execute(
                "SELECT * FROM knowledge_relations WHERE from_entity=? OR to_entity=?",
                (entity_name, entity_name),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def search_knowledge_entities(self, query: str, limit: int = 10) -> list[dict]:
        """搜索知识实体（优先 FTS5，降级 LIKE）"""
        from db.fts_utils import _build_fts_query
        fts_query = _build_fts_query(query)

        if fts_query:
            try:
                cursor = await self._conn.execute(
                    """SELECT ke.*, bm25(knowledge_entities_fts) AS score
                       FROM knowledge_entities_fts
                       JOIN knowledge_entities ke ON ke.id = knowledge_entities_fts.id
                       WHERE knowledge_entities_fts MATCH ?
                       ORDER BY score ASC, ke.updated_at DESC
                       LIMIT ?""",
                    (fts_query, limit),
                )
                rows = await cursor.fetchall()
                if rows:
                    return [dict(r) for r in rows]
            except Exception as e:
                # 规则：FTS5 检索失败降级到 LIKE 即 bug 信号，必须 ERROR + degradation_triggered
                logger.error("degradation_triggered knowledge.fts_search_failed "
                             "fallback=LIKE error={}", str(e))

        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        cursor = await self._conn.execute(
            """SELECT * FROM knowledge_entities
               WHERE name LIKE ? ESCAPE '\\'
               ORDER BY updated_at DESC LIMIT ?""",
            (f"%{escaped}%", limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def delete_knowledge_entity(self, name: str, auto_commit: bool = True) -> bool:
        # 先按 rowid 删 FTS（主表删除后查不到 rowid）
        cur = await self._conn.execute(
            "SELECT rowid FROM knowledge_entities WHERE name=?", (name,))
        row = await cur.fetchone()
        if row is not None:
            await self._delete_entity_fts_by_rowid(row[0])
        # 级联清理引用该实体的关系
        await self._conn.execute(
            "DELETE FROM knowledge_relations WHERE from_entity=? OR to_entity=?",
            (name, name),
        )
        cursor = await self._conn.execute(
            "DELETE FROM knowledge_entities WHERE name=?", (name,)
        )
        if auto_commit:
            await self._conn.commit()
        return cursor.rowcount > 0

    async def delete_knowledge_relation(self, relation_id: str, auto_commit: bool = True) -> bool:
        cursor = await self._conn.execute(
            "DELETE FROM knowledge_relations WHERE id=?", (relation_id,)
        )
        if auto_commit:
            await self._conn.commit()
        return cursor.rowcount > 0

    async def get_all_entities(self, limit: int = 500) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM knowledge_entities ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_all_relations(self, limit: int = 500) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM knowledge_relations ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def update_knowledge_entity(self, name: str, kind: str = "",
                                       observations: list | None = None,
                                       auto_commit: bool = True) -> None:
        """更新知识实体（公开方法，避免路由绕过封装）"""
        obs_json = json.dumps(observations or [], ensure_ascii=False)
        await self._conn.execute(
            "UPDATE knowledge_entities SET kind=?, observations=?, updated_at=? WHERE name=?",
            (kind, obs_json, time.time(), name),
        )
        if auto_commit:
            await self._conn.commit()

    async def update_knowledge_relation(self, relation_id: str, from_entity: str | None = None,
                                         relation_type: str | None = None, to_entity: str | None = None,
                                         auto_commit: bool = True) -> None:
        """更新知识关系（公开方法）"""
        sets = []
        params = []
        if from_entity is not None:
            sets.append("from_entity=?")
            params.append(from_entity)
        if relation_type is not None:
            sets.append("relation_type=?")
            params.append(relation_type)
        if to_entity is not None:
            sets.append("to_entity=?")
            params.append(to_entity)
        if not sets:
            return
        sets.append("updated_at=?")
        params.append(time.time())
        params.append(relation_id)
        await self._conn.execute(
            f"UPDATE knowledge_relations SET {', '.join(sets)} WHERE id=?",
            params,
        )
        if auto_commit:
            await self._conn.commit()

    async def merge_entity(self, entity: dict, auto_commit: bool = True) -> None:
        name = entity.get("name", "")
        if not name:
            return
        kind = entity.get("kind", "")
        new_obs = entity.get("observations", [])
        existing = await self.get_knowledge_entity(name)
        if existing:
            old_obs = existing.get("observations", [])
            if isinstance(old_obs, str):
                try:
                    old_obs = json.loads(old_obs)
                except (json.JSONDecodeError, TypeError):
                    old_obs = []
            merged = list(old_obs)
            for obs in new_obs:
                if obs not in merged:
                    merged.append(obs)
            try:
                await self._conn.execute(
                    "UPDATE knowledge_entities SET kind=?, observations=?, updated_at=? WHERE name=?",
                    (kind or existing.get("kind", ""), json.dumps(merged, ensure_ascii=False), time.time(), name),
                )
                if auto_commit:
                    await self._conn.commit()
            except Exception as update_err:
                # CodeRabbit 修复：auto_commit=False 表示调用方拥有事务（如 write_transaction
                # 内的批量写入）。此时 rollback 会回滚调用方整个事务（破坏其他已写入数据），
                # retry/lite fallback 同样在调用方事务上执行未授权写入。正确行为：直接传播
                # 原始失败，由调用方的 write_transaction 统一 rollback。
                if not auto_commit:
                    logger.error("kg.merge_entity_update_failed_caller_txn "
                                 "name={} error={} action=propagate_to_caller_txn",
                                 name, str(update_err))
                    raise
                # auto_commit=True：内部 owned 事务，可安全 rollback + retry + lite fallback
                # 根因：aiosqlite 单连接共享事务状态，并发的 auto_commit=False 长事务
                # （如 _do_children 批量写入）超时/异常时未 rollback，脏事务残留在连接上，
                # 导致本 UPDATE 在脏事务中执行 → "SQL logic error"。
                # 修复：先 rollback 清理脏事务，再重试一次。rollback 后连接恢复干净状态。
                # 规则：触发重试/降级即视为 bug，必须 ERROR + degradation_triggered 告警
                logger.error("degradation_triggered kg.merge_entity_update_failed "
                             "name={} error={} action=rollback_and_retry",
                             name, str(update_err))
                try:
                    await self._conn.rollback()
                except Exception:
                    logger.warning("db_knowledge.rollback_failed", exc_info=True)
                # 重试：连接已清理，应该能成功
                _retry_ok = False
                try:
                    await self._conn.execute(
                        "UPDATE knowledge_entities SET kind=?, observations=?, updated_at=? WHERE name=?",
                        (kind or existing.get("kind", ""), json.dumps(merged, ensure_ascii=False), time.time(), name),
                    )
                    if auto_commit:
                        await self._conn.commit()
                    _retry_ok = True
                except Exception as retry_err:
                    logger.error("degradation_triggered kg.merge_entity_retry_failed "
                                 "name={} error={}", name, str(retry_err))
                if not _retry_ok:
                    # 最终降级：只更新 updated_at（不触碰 observations）
                    # 规则：降级触发即 bug，记录 degradation_triggered
                    logger.error("degradation_triggered kg.merge_entity_fallback_to_lite_update "
                                 "name={} reason=retry_failed", name)
                    try:
                        await self._conn.execute(
                            "UPDATE knowledge_entities SET updated_at=? WHERE name=?",
                            (time.time(), name),
                        )
                        if auto_commit:
                            await self._conn.commit()
                    except Exception as lite_err:
                        logger.error("degradation_triggered kg.merge_entity_lite_update_failed "
                                     "name={} error={}", name, str(lite_err))
        else:
            entity_id = entity.get("id", f"ENT-{uuid.uuid4().hex[:12]}")
            await self.insert_knowledge_entity(entity_id, name, kind, new_obs,
                                                auto_commit=auto_commit)

    async def merge_relation(self, relation: dict, auto_commit: bool = True) -> None:
        from_entity = relation.get("from_entity", relation.get("from", relation.get("source", "")))
        relation_type = relation.get("relation_type", relation.get("relation", relation.get("type", "")))
        to_entity = relation.get("to_entity", relation.get("to", relation.get("target", "")))
        if not from_entity or not relation_type or not to_entity:
            return
        rel_id = relation.get("id", f"REL-{uuid.uuid4().hex[:12]}")
        await self._conn.execute(
            """INSERT OR IGNORE INTO knowledge_relations (id, from_entity, relation_type, to_entity, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (rel_id, from_entity, relation_type, to_entity, time.time()),
        )
        if auto_commit:
            await self._conn.commit()

    async def get_related_knowledge(self, entity_names: list[str], depth: int = 1) -> dict:
        all_entities = {}
        all_relations = []
        seen_rel_keys = set()
        visited = set(entity_names)
        frontier = list(entity_names)
        for _ in range(depth):
            next_frontier = []
            if not frontier:
                break
            # Batch fetch entities for current frontier
            placeholders = ",".join("?" * len(frontier))
            cursor = await self._conn.execute(
                f"SELECT * FROM knowledge_entities WHERE name IN ({placeholders})",
                frontier,
            )
            entity_rows = await cursor.fetchall()
            entity_map = {r["name"]: dict(r) for r in entity_rows}
            # Batch fetch relations for current frontier
            cursor = await self._conn.execute(
                f"SELECT * FROM knowledge_relations WHERE from_entity IN ({placeholders}) OR to_entity IN ({placeholders})",
                frontier + frontier,
            )
            rel_rows = await cursor.fetchall()
            rel_map = {}
            for r in rel_rows:
                rel = dict(r)
                rel_key = (rel.get("from_entity", ""), rel.get("relation_type", ""), rel.get("to_entity", ""))
                if rel_key not in seen_rel_keys:
                    seen_rel_keys.add(rel_key)
                    all_relations.append(rel)
                # Index by both endpoints for frontier expansion
                rel_map.setdefault(rel.get("from_entity", ""), []).append(rel)
                rel_map.setdefault(rel.get("to_entity", ""), []).append(rel)
            for name in frontier:
                if name in all_entities:
                    continue
                ent = entity_map.get(name)
                if ent:
                    all_entities[name] = ent
                for rel in rel_map.get(name, []):
                    other = rel.get("to_entity") if rel.get("from_entity") == name else rel.get("from_entity")
                    if other and other not in visited:
                        visited.add(other)
                        next_frontier.append(other)
            frontier = next_frontier
        return {"entities": list(all_entities.values()), "relations": all_relations}

    async def cleanup_stale(self, days: int = 30, auto_commit: bool = True) -> int:
        cutoff = time.time() - days * 86400
        # 先批量删 FTS（按主表 rowid，触发器已废弃由应用层维护）
        await self._conn.execute(
            "DELETE FROM knowledge_entities_fts WHERE rowid IN "
            "(SELECT rowid FROM knowledge_entities WHERE updated_at < ?)",
            (cutoff,),
        )
        cursor = await self._conn.execute(
            "DELETE FROM knowledge_entities WHERE updated_at < ?", (cutoff,)
        )
        await self._conn.execute(
            "DELETE FROM knowledge_relations WHERE updated_at < ?", (cutoff,)
        )
        if auto_commit:
            await self._conn.commit()
        return cursor.rowcount

    async def get_entity_count(self) -> int:
        cursor = await self._conn.execute("SELECT COUNT(*) as cnt FROM knowledge_entities")
        row = await cursor.fetchone()
        return row["cnt"] if row else 0
