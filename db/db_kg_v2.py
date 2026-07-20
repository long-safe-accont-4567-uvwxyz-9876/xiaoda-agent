"""KnowledgeDBV2 — KG v2 表的 CRUD 操作。

覆盖: kg_episodes, kg_entities_v2, kg_relations_v2, kg_communities, kg_edge_episode_refs。
所有写方法默认 auto_commit=True，返回 rowid 的方法用于向量表同步。
"""
import json
import time

import aiosqlite


class KnowledgeDBV2:
    """KG v2 表的持久化操作。"""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        conn.row_factory = aiosqlite.Row

    # ── Episode ──────────────────────────────────────────────

    async def insert_episode(
        self,
        episode_id: str,
        content: str,
        source_type: str,
        valid_at: float,
        created_at: float,
        source_description: str = "",
        group_id: str = "default",
        auto_commit: bool = True,
    ) -> None:
        await self._conn.execute(
            """INSERT OR REPLACE INTO kg_episodes
               (id, content, source_type, source_description, valid_at, created_at, group_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (episode_id, content, source_type, source_description, valid_at, created_at, group_id),
        )
        if auto_commit:
            await self._conn.commit()

    async def get_episode(self, episode_id: str) -> dict | None:
        cursor = await self._conn.execute(
            "SELECT * FROM kg_episodes WHERE id=?", (episode_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # ── Entity v2 ────────────────────────────────────────────

    async def insert_entity_v2(
        self,
        entity_id: str,
        name: str,
        kind: str,
        observations: list,
        summary: str,
        auto_commit: bool = True,
    ) -> int:
        """插入实体，返回 rowid（用于向量表同步）。"""
        obs_json = json.dumps(observations or [], ensure_ascii=False)
        now = time.time()
        cursor = await self._conn.execute(
            """INSERT OR IGNORE INTO kg_entities_v2
               (id, name, kind, observations, summary, summary_version, updated_at, created_at)
               VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
            (entity_id, name, kind, obs_json, summary, now, now),
        )
        if auto_commit:
            await self._conn.commit()
        rowid = cursor.lastrowid
        if rowid == 0:
            cur = await self._conn.execute(
                "SELECT rowid FROM kg_entities_v2 WHERE name=?", (name,)
            )
            row = await cur.fetchone()
            rowid = row[0] if row else 0
        return rowid

    async def get_entity_v2(self, name: str) -> dict | None:
        cursor = await self._conn.execute(
            "SELECT * FROM kg_entities_v2 WHERE name=?", (name,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_entity_summary_v2(
        self,
        name: str,
        summary: str,
        summary_version: int,
        auto_commit: bool = True,
    ) -> int:
        """更新实体摘要，返回 rowid。"""
        await self._conn.execute(
            "UPDATE kg_entities_v2 SET summary=?, summary_version=?, updated_at=? WHERE name=?",
            (summary, summary_version, time.time(), name),
        )
        if auto_commit:
            await self._conn.commit()
        cur = await self._conn.execute(
            "SELECT rowid FROM kg_entities_v2 WHERE name=?", (name,)
        )
        row = await cur.fetchone()
        return row[0] if row else 0

    # ── Relation v2 ──────────────────────────────────────────

    async def insert_relation_v2(
        self,
        rel_id: str,
        from_entity: str,
        relation_type: str,
        to_entity: str,
        fact: str,
        episode_id: str,
        valid_at: float,
        auto_commit: bool = True,
    ) -> int:
        """插入关系，返回 rowid。同时写入 episode_ref。"""
        now = time.time()
        episode_ids = json.dumps([episode_id], ensure_ascii=False)
        cursor = await self._conn.execute(
            """INSERT OR IGNORE INTO kg_relations_v2
               (id, from_entity, relation_type, to_entity, fact, fact_embedding,
                episode_ids, valid_at, invalid_at, expired_at, is_current,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, NULL, ?, ?, NULL, NULL, 1, ?, ?)""",
            (rel_id, from_entity, relation_type, to_entity, fact,
             episode_ids, valid_at, now, now),
        )
        # 写入 episode ref
        await self._conn.execute(
            "INSERT OR IGNORE INTO kg_edge_episode_refs (edge_id, episode_id) VALUES (?, ?)",
            (rel_id, episode_id),
        )
        if auto_commit:
            await self._conn.commit()
        rowid = cursor.lastrowid
        if rowid == 0:
            cur = await self._conn.execute(
                "SELECT rowid FROM kg_relations_v2 WHERE id=?", (rel_id,)
            )
            row = await cur.fetchone()
            rowid = row[0] if row else 0
        return rowid

    async def get_active_relations_between(
        self, from_entity: str, to_entity: str
    ) -> list[dict]:
        """获取两个实体之间当前有效的关系（双向）。"""
        cursor = await self._conn.execute(
            """SELECT * FROM kg_relations_v2
               WHERE ((from_entity=? AND to_entity=?) OR (from_entity=? AND to_entity=?))
               AND is_current=1""",
            (from_entity, to_entity, to_entity, from_entity),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_active_relations_by_subject_and_type(
        self, from_entity: str, relation_type: str
    ) -> list[dict]:
        """获取同一主体 + 关系类型的当前有效关系（to_entity 可能不同，用于超驰检测）。"""
        cursor = await self._conn.execute(
            """SELECT * FROM kg_relations_v2
               WHERE from_entity=? AND relation_type=? AND is_current=1""",
            (from_entity, relation_type),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def invalidate_relation(
        self,
        rel_id: str,
        invalid_at: float,
        expired_at: float,
        auto_commit: bool = True,
    ) -> None:
        """标记关系失效。"""
        await self._conn.execute(
            """UPDATE kg_relations_v2
               SET invalid_at=?, expired_at=?, is_current=0, updated_at=?
               WHERE id=?""",
            (invalid_at, expired_at, time.time(), rel_id),
        )
        if auto_commit:
            await self._conn.commit()

    async def append_episode_ref(
        self, edge_id: str, episode_id: str, auto_commit: bool = True
    ) -> None:
        """追加 episode 引用（去重）。同时更新关系的 episode_ids JSON。

        原子化: 用 SQLite JSON1 的 json_insert + json_each 去重，
        避免 read-modify-write 竞态导致丢失更新。
        """
        await self._conn.execute(
            "INSERT OR IGNORE INTO kg_edge_episode_refs (edge_id, episode_id) VALUES (?, ?)",
            (edge_id, episode_id),
        )
        # 原子追加 episode_id 到 episode_ids JSON 数组（已存在则不修改）
        # json_insert(j, '$[#]', v) 在数组末尾追加；NOT EXISTS json_each 去重
        # COALESCE 兜底 NULL（旧数据可能为 NULL，原实现 if row["episode_ids"] else []）
        await self._conn.execute(
            """UPDATE kg_relations_v2
               SET episode_ids = json_insert(COALESCE(episode_ids, '[]'), '$[#]', ?),
                   updated_at = ?
               WHERE id=?
                 AND NOT EXISTS (
                     SELECT 1 FROM json_each(COALESCE(episode_ids, '[]')) WHERE value = ?
                 )""",
            (episode_id, time.time(), edge_id, episode_id),
        )
        if auto_commit:
            await self._conn.commit()

    # ── Community ────────────────────────────────────────────

    async def insert_community(
        self,
        community_id: str,
        name: str,
        summary: str,
        member_entities: list,
        auto_commit: bool = True,
    ) -> None:
        members_json = json.dumps(member_entities, ensure_ascii=False)
        now = time.time()
        await self._conn.execute(
            """INSERT OR REPLACE INTO kg_communities
               (id, name, summary, member_entities, name_embedding, created_at, updated_at)
               VALUES (?, ?, ?, ?, NULL, ?, ?)""",
            (community_id, name, summary, members_json, now, now),
        )
        # 更新成员实体的社区归属
        for entity_name in member_entities:
            await self._conn.execute(
                "UPDATE kg_entities_v2 SET community_id=?, updated_at=? WHERE name=?",
                (community_id, now, entity_name),
            )
        if auto_commit:
            await self._conn.commit()

    async def get_entity_community(self, entity_name: str) -> str | None:
        """查询实体所属社区 ID。"""
        cursor = await self._conn.execute(
            "SELECT community_id FROM kg_entities_v2 WHERE name=?", (entity_name,)
        )
        row = await cursor.fetchone()
        if row and row["community_id"]:
            return row["community_id"]
        return None

    async def add_entity_to_community(
        self, entity_name: str, community_id: str, auto_commit: bool = True
    ) -> None:
        """将实体加入社区（设置 community_id 专用列）。

        原子化: 用 SQLite JSON1 的 json_insert + json_each 去重，
        避免 member_entities 列 read-modify-write 竞态导致丢失更新。
        """
        now = time.time()
        # 原子更新实体的 community_id 专用列
        await self._conn.execute(
            "UPDATE kg_entities_v2 SET community_id=?, updated_at=? WHERE name=?",
            (community_id, now, entity_name),
        )
        # 原子追加 entity_name 到社区的 member_entities JSON 数组（已存在则不修改）
        # COALESCE 兜底 NULL（与原实现 if row["member_entities"] else [] 等价）
        await self._conn.execute(
            """UPDATE kg_communities
               SET member_entities = json_insert(COALESCE(member_entities, '[]'), '$[#]', ?),
                   updated_at = ?
               WHERE id=?
                 AND NOT EXISTS (
                     SELECT 1 FROM json_each(COALESCE(member_entities, '[]')) WHERE value = ?
                 )""",
            (entity_name, now, community_id, entity_name),
        )
        if auto_commit:
            await self._conn.commit()

    # ── 双向溯源查询 ─────────────────────────────────────────

    async def get_facts_from_episode(self, episode_id: str) -> list[dict]:
        """前向查询: episode → facts (relations)。"""
        cursor = await self._conn.execute(
            """SELECT r.* FROM kg_relations_v2 r
               JOIN kg_edge_episode_refs ref ON ref.edge_id = r.id
               WHERE ref.episode_id=?""",
            (episode_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_episodes_for_fact(self, edge_id: str) -> list[dict]:
        """反向查询: fact → episodes。"""
        cursor = await self._conn.execute(
            """SELECT e.* FROM kg_episodes e
               JOIN kg_edge_episode_refs ref ON ref.episode_id = e.id
               WHERE ref.edge_id=?""",
            (edge_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
