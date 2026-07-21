import time
import json
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

    async def insert_knowledge_entity(self, entity_id: str, name: str,
                                       kind: str = "", observations: list | None = None,
                                       auto_commit: bool = True) -> None:
        obs_json = json.dumps(observations or [], ensure_ascii=False)
        await self._conn.execute(
            """INSERT OR IGNORE INTO knowledge_entities (id, name, kind, observations, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (entity_id, name, kind, obs_json, time.time()),
        )
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
                logger.warning(f"knowledge.fts_search_failed, fallback to LIKE: {e}")

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
                # FTS5 触发器或并发写锁可能引发 "SQL logic error"
                # 降级 1：只更新 updated_at，跳过 observations（避免触发 FTS5 触发器）
                logger.warning("kg.merge_entity_update_failed name={} error={} → 降级为轻量更新",
                              name, str(update_err))
                try:
                    await self._conn.execute(
                        "UPDATE knowledge_entities SET updated_at=? WHERE name=?",
                        (time.time(), name),
                    )
                    if auto_commit:
                        await self._conn.commit()
                except Exception as lite_err:
                    # 降级 2：连轻量更新也失败，记录但不抛出（merge_entities 已包 try/except）
                    logger.error("kg.merge_entity_lite_update_failed name={} error={}",
                                 name, str(lite_err))
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
