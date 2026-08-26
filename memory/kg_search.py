"""KGSearchEngine — 混合检索引擎: 语义 + 全文 + 图遍历, RRF 融合。"""
import asyncio
import re
from typing import Any

import aiosqlite
from loguru import logger

from db.db_kg_v2 import KnowledgeDBV2
from db.fts_utils import _build_fts_query
from utils.rank_fusion import reciprocal_rank_fusion

# KG v2 分区键改为 <user_id>::<agent_id> 之前的旧分区名。线上库存量行挂在
# 该分区下但无归属信息（无法证明属于任何当前用户）。
# 隐私契约（2026-08-24 收紧，见 tests/test_kg_v2_legacy_compat.py）：
# scoped 读（personal/group）对 legacy 分区一律 fail-closed 不可见；
# 仅 scope=None 的显式 admin/maintenance 路径可读。只做读隔离，不改写数据。
LEGACY_PARTITION_KEY = "default"


def _scope_query_keys(scope_key: str | None) -> list[str]:
    """Use exact partitions for recall; scope=None is the legacy admin path."""
    if scope_key is None:
        return []
    return [scope_key]


class KGSearchEngine:
    """混合检索引擎，融合语义、全文、图三路搜索结果。"""

    def __init__(
        self,
        db: KnowledgeDBV2,
        vector_store: Any,
        conn: aiosqlite.Connection,
    ) -> None:
        self._db = db
        self._vector_store = vector_store
        self._conn = conn

    async def search(
        self,
        query: str,
        top_k: int = 10,
        as_of: float | None = None,
        scope: Any | None = None,
    ) -> list[dict]:
        """混合检索: 语义 + 全文 + 图遍历, RRF 融合。

        Args:
            query: 查询文本
            top_k: 返回条数
            as_of: None=只返回当前有效; 时间戳=历史快照
        """
        scope_key = self._scope_key(scope)
        is_group_scope = (
            scope is not None
            and getattr(getattr(scope, "boundary", None), "value", None)
            == "conversation"
        )
        # return_exceptions=True：单路检索异常不应阻断整体搜索
        raw = await asyncio.gather(
            self._semantic_search(query, top_k * 2, scope_key),
            self._fulltext_search(query, top_k * 2, scope_key),
            self._graph_search(query, top_k * 2, scope_key),
            return_exceptions=True,
        )
        results: list[list[dict]] = []
        for idx, r in enumerate(raw):
            # CancelledError 是 BaseException，需要单独处理
            if isinstance(r, asyncio.CancelledError):
                logger.debug("kg_search.sub_search_cancelled", idx=idx)
                results.append([])
            elif isinstance(r, Exception):
                logger.warning("kg_search.sub_search_failed", idx=idx, error=str(r))
                results.append([])
            else:
                if is_group_scope:
                    r = [item for item in r if item.get("type") == "relation"]
                results.append(r)
        fused = self._rrf_fuse(results, k=60)

        # 时序过滤
        if as_of is None:
            fused = [r for r in fused if r.get("is_current", 1) == 1]
        else:
            filtered = []
            for r in fused:
                valid_at = r.get("valid_at") or 0
                invalid_at = r.get("invalid_at")
                if valid_at <= as_of and (invalid_at is None or invalid_at > as_of):
                    filtered.append(r)
            fused = filtered

        return fused[:top_k]

    @staticmethod
    def _scope_key(scope: Any | None) -> str | None:
        if scope is None:
            return None
        return scope.kg_partition_key()

    @staticmethod
    def _visible_entities_subsql(scope_keys: list[str]) -> str:
        """兼容读范围内出现过的实体名子查询（供 FTS SQL 内联下推过滤）。"""
        ph = ",".join("?" * len(scope_keys))
        return (
            "SELECT name FROM ("
            "SELECT r.from_entity AS name FROM kg_relations_v2 r "
            "JOIN kg_edge_episode_refs ref ON ref.edge_id = r.id "
            f"JOIN kg_episodes e ON e.id = ref.episode_id WHERE e.group_id IN ({ph}) "
            "UNION "
            "SELECT r.to_entity AS name FROM kg_relations_v2 r "
            "JOIN kg_edge_episode_refs ref ON ref.edge_id = r.id "
            f"JOIN kg_episodes e ON e.id = ref.episode_id WHERE e.group_id IN ({ph}))"
        )

    async def _visible_entity_names(self, scope_keys: list[str]) -> set[str]:
        """兼容读范围内出现过的实体名集合。

        kg_entities_v2 按 name 全局唯一、无物理 scope 列；scoped 实体召回的
        可见性由「实体参与过范围内（新分区 + legacy 兜底）的关系」推导。
        """
        cursor = await self._conn.execute(
            f"{self._visible_entities_subsql(scope_keys)}",
            (*scope_keys, *scope_keys),
        )
        return {row[0] for row in await cursor.fetchall()}

    async def _relation_in_scope(self, relation_id: str, scope_keys: list[str]) -> bool:
        placeholders = ",".join("?" * len(scope_keys))
        cursor = await self._conn.execute(
            f"""SELECT 1
               FROM kg_edge_episode_refs ref
               JOIN kg_episodes e ON e.id = ref.episode_id
               WHERE ref.edge_id = ? AND e.group_id IN ({placeholders})
               LIMIT 1""",
            (relation_id, *scope_keys),
        )
        return await cursor.fetchone() is not None

    async def _scoped_entity_rowids(self, scope_keys: list[str]) -> list[int]:
        names = await self._visible_entity_names(scope_keys)
        if not names:
            return []
        placeholders = ",".join("?" * len(names))
        cursor = await self._conn.execute(
            f"SELECT rowid FROM kg_entities_v2 WHERE name IN ({placeholders})",
            tuple(names),
        )
        return [int(row[0]) for row in await cursor.fetchall()]

    async def _scoped_relation_rowids(self, scope_keys: list[str]) -> list[int]:
        placeholders = ",".join("?" * len(scope_keys))
        cursor = await self._conn.execute(
            f"""SELECT DISTINCT r.rowid
               FROM kg_relations_v2 r
               JOIN kg_edge_episode_refs ref ON ref.edge_id = r.id
               JOIN kg_episodes e ON e.id = ref.episode_id
               WHERE e.group_id IN ({placeholders})""",
            (*scope_keys,),
        )
        return [int(row[0]) for row in await cursor.fetchall()]

    async def _semantic_search(
        self, query: str, k: int, scope_key: str | None = None
    ) -> list[dict]:
        """语义搜索: sqlite-vec KNN。"""
        if not self._vector_store:
            return []
        try:
            scope_keys = _scope_query_keys(scope_key)
            entity_candidates = (
                await self._scoped_entity_rowids(scope_keys)
                if scope_key is not None else None
            )
            entity_hits = await self._vector_store.search_kg_entities(
                query, top_k=k, candidate_ids=entity_candidates
            )
            relation_candidates = (
                await self._scoped_relation_rowids(scope_keys)
                if scope_key is not None else None
            )
            relation_hits = await self._vector_store.search_kg_relations(
                query, top_k=k, candidate_ids=relation_candidates
            )

            results = []
            # 实体检索不再因 scoped 而跳过（修复 scoped 实体召回归零）：
            # kg_entities_v2 无物理 scope，可见性限定为兼容读范围内出现过的实体。
            visible_names: set[str] | None = None
            if scope_key is not None:
                visible_names = await self._visible_entity_names(scope_keys)
            for rowid, distance in entity_hits:
                cursor = await self._conn.execute(
                    "SELECT id, name, kind, summary FROM kg_entities_v2 WHERE rowid=?", (rowid,)
                )
                row = await cursor.fetchone()
                if row and (visible_names is None or row["name"] in visible_names):
                    results.append({
                        "type": "entity",
                        "id": row["id"],
                        "name": row["name"],
                        "kind": row["kind"],
                        "summary": row["summary"],
                        "distance": distance,
                    })
            # 关系命中
            for rowid, distance in relation_hits:
                cursor = await self._conn.execute(
                    "SELECT r.id, r.from_entity, r.relation_type, r.to_entity, r.fact, "
                    "(SELECT json_group_array(DISTINCT ref2.episode_id) "
                    " FROM kg_edge_episode_refs ref2 WHERE ref2.edge_id=r.id) AS episode_ids, "
                    "r.valid_at, r.invalid_at, r.is_current "
                    "FROM kg_relations_v2 r WHERE r.rowid=?", (rowid,)
                )
                row = await cursor.fetchone()
                if row and (
                    scope_key is None
                    or await self._relation_in_scope(row["id"], scope_keys)
                ):
                    results.append({
                        "type": "relation",
                        "id": row["id"],
                        "from_entity": row["from_entity"],
                        "relation_type": row["relation_type"],
                        "to_entity": row["to_entity"],
                        "fact": row["fact"],
                        "episode_ids": row["episode_ids"],
                        "valid_at": row["valid_at"],
                        "invalid_at": row["invalid_at"],
                        "is_current": row["is_current"],
                        "distance": distance,
                    })
            return results
        except Exception as e:
            logger.debug("kg_search.semantic_failed", error=str(e), exc_info=True)
            return []

    async def _fulltext_search(
        self, query: str, k: int, scope_key: str | None = None
    ) -> list[dict]:
        """FTS5 BM25 全文搜索 + CJK LIKE 降级。

        FTS5 默认 unicode61 分词器不拆分连续中文 (如 "用户喜欢篮球" 是单个 token),
        导致 MATCH '"篮球"' 无法命中。补充 LIKE 子串搜索作为降级, 确保中文 fact 可检索。
        """
        fts_query = _build_fts_query(query)
        results: list[dict] = []
        seen_keys: set[str] = set()

        if fts_query:
            try:
                scope_keys = _scope_query_keys(scope_key)
                # 实体: name + summary。scoped 时按兼容读范围推导的可见实体过滤，
                # 不再整体跳过（修复 scoped 实体召回归零）。
                if scope_key is None:
                    entity_sql = """SELECT e.id, e.name, e.kind, e.summary
                           FROM kg_entities_v2_fts
                           JOIN kg_entities_v2 e ON e.id = kg_entities_v2_fts.id
                           WHERE kg_entities_v2_fts MATCH ?
                           ORDER BY rank LIMIT ?"""
                    entity_params: tuple = (fts_query, k)
                else:
                    entity_sql = f"""SELECT e.id, e.name, e.kind, e.summary
                           FROM kg_entities_v2_fts
                           JOIN kg_entities_v2 e ON e.id = kg_entities_v2_fts.id
                           WHERE kg_entities_v2_fts MATCH ?
                             AND e.name IN ({self._visible_entities_subsql(scope_keys)})
                           ORDER BY rank LIMIT ?"""
                    entity_params = (fts_query, *scope_keys, *scope_keys, k)
                cursor = await self._conn.execute(entity_sql, entity_params)
                for row in await cursor.fetchall():
                    key = f"entity:{row['id']}"
                    if key not in seen_keys:
                        seen_keys.add(key)
                        results.append({
                            "type": "entity",
                            "id": row["id"],
                            "name": row["name"],
                            "kind": row["kind"],
                            "summary": row["summary"],
                        })
                # 关系: fact。scope 条件必须在 LIMIT 前下推，避免其他用户占满候选窗。
                if scope_key is None:
                    relation_sql = """SELECT r.id, r.from_entity, r.relation_type, r.to_entity, r.fact,
                                              (SELECT json_group_array(DISTINCT ref2.episode_id)
                                               FROM kg_edge_episode_refs ref2
                                               WHERE ref2.edge_id=r.id) AS episode_ids,
                                              r.valid_at, r.invalid_at, r.is_current
                                       FROM kg_relations_v2_fts
                                       JOIN kg_relations_v2 r ON r.id = kg_relations_v2_fts.id
                                       WHERE kg_relations_v2_fts MATCH ?
                                       ORDER BY rank LIMIT ?"""
                    relation_params = (fts_query, k)
                else:
                    ph = ",".join("?" * len(scope_keys))
                    relation_sql = f"""SELECT DISTINCT r.id, r.from_entity, r.relation_type,
                                              r.to_entity, r.fact,
                                              (SELECT json_group_array(DISTINCT ref2.episode_id)
                                               FROM kg_edge_episode_refs ref2
                                               WHERE ref2.edge_id=r.id) AS episode_ids,
                                              r.valid_at, r.invalid_at,
                                              r.is_current, rank
                                       FROM kg_relations_v2_fts
                                       JOIN kg_relations_v2 r ON r.id = kg_relations_v2_fts.id
                                       JOIN kg_edge_episode_refs ref ON ref.edge_id = r.id
                                       JOIN kg_episodes e ON e.id = ref.episode_id
                                       WHERE kg_relations_v2_fts MATCH ? AND e.group_id IN ({ph})
                                       ORDER BY rank LIMIT ?"""
                    relation_params = (fts_query, *scope_keys, k)
                cursor = await self._conn.execute(relation_sql, relation_params)
                for row in await cursor.fetchall():
                    key = f"relation:{row['id']}"
                    if key not in seen_keys:
                        seen_keys.add(key)
                        results.append({
                            "type": "relation",
                            "id": row["id"],
                            "from_entity": row["from_entity"],
                            "relation_type": row["relation_type"],
                            "to_entity": row["to_entity"],
                            "fact": row["fact"],
                            "episode_ids": row["episode_ids"],
                            "valid_at": row["valid_at"],
                            "invalid_at": row["invalid_at"],
                            "is_current": row["is_current"],
                        })
            except Exception as e:
                logger.debug("kg_search.fulltext_failed", error=str(e))

        # CJK LIKE 降级: 补充 fact 子串搜索
        try:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like_pattern = f"%{escaped}%"
            if scope_key is None:
                like_sql = """SELECT r.id, r.from_entity, r.relation_type, r.to_entity, r.fact,
                                      (SELECT json_group_array(DISTINCT ref2.episode_id)
                                       FROM kg_edge_episode_refs ref2
                                       WHERE ref2.edge_id=r.id) AS episode_ids,
                                      r.valid_at, r.invalid_at, r.is_current
                               FROM kg_relations_v2 r
                               WHERE r.fact LIKE ? ESCAPE '\\' LIMIT ?"""
                like_params = (like_pattern, k)
            else:
                scope_keys = _scope_query_keys(scope_key)
                ph = ",".join("?" * len(scope_keys))
                like_sql = f"""SELECT DISTINCT r.id, r.from_entity, r.relation_type,
                                      r.to_entity, r.fact,
                                      (SELECT json_group_array(DISTINCT ref2.episode_id)
                                       FROM kg_edge_episode_refs ref2
                                       WHERE ref2.edge_id=r.id) AS episode_ids,
                                      r.valid_at, r.invalid_at,
                                      r.is_current
                               FROM kg_relations_v2 r
                               JOIN kg_edge_episode_refs ref ON ref.edge_id = r.id
                               JOIN kg_episodes e ON e.id = ref.episode_id
                               WHERE r.fact LIKE ? ESCAPE '\\' AND e.group_id IN ({ph})
                               LIMIT ?"""
                like_params = (like_pattern, *scope_keys, k)
            cursor = await self._conn.execute(like_sql, like_params)
            for row in await cursor.fetchall():
                key = f"relation:{row['id']}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    results.append({
                        "type": "relation",
                        "id": row["id"],
                        "from_entity": row["from_entity"],
                        "relation_type": row["relation_type"],
                        "to_entity": row["to_entity"],
                        "fact": row["fact"],
                        "episode_ids": row["episode_ids"],
                        "valid_at": row["valid_at"],
                        "invalid_at": row["invalid_at"],
                        "is_current": row["is_current"],
                    })
        except Exception as e:
            logger.debug("kg_search.fulltext_like_failed", error=str(e))
        return results

    async def _graph_search(
        self, query: str, k: int, scope_key: str | None = None
    ) -> list[dict]:
        """图遍历搜索: 递归 CTE BFS。"""
        entities = await self._extract_query_entities(query)
        if not entities:
            return []
        results = []
        for seed in list(entities)[:3]:
            try:
                if scope_key is None:
                    sql = """WITH RECURSIVE bfs(entity, depth) AS (
                        SELECT ?, 0
                        UNION ALL
                        SELECT CASE WHEN r.from_entity = b.entity THEN r.to_entity
                                    ELSE r.from_entity END, b.depth + 1
                        FROM kg_relations_v2 r JOIN bfs b
                          ON (r.from_entity = b.entity OR r.to_entity = b.entity)
                        WHERE b.depth < 2 AND r.is_current = 1
                    )
                    SELECT DISTINCT entity, MIN(depth) as min_depth FROM bfs
                    GROUP BY entity ORDER BY min_depth LIMIT ?"""
                    params = (seed, k)
                else:
                    scope_keys = _scope_query_keys(scope_key)
                    ph = ",".join("?" * len(scope_keys))
                    sql = f"""WITH RECURSIVE bfs(entity, depth) AS (
                        SELECT ?, 0
                        UNION ALL
                        SELECT CASE WHEN r.from_entity = b.entity THEN r.to_entity
                                    ELSE r.from_entity END, b.depth + 1
                        FROM kg_relations_v2 r JOIN bfs b
                          ON (r.from_entity = b.entity OR r.to_entity = b.entity)
                        WHERE b.depth < 2 AND r.is_current = 1
                          AND EXISTS (
                              SELECT 1 FROM kg_edge_episode_refs ref
                              JOIN kg_episodes e ON e.id = ref.episode_id
                              WHERE ref.edge_id = r.id AND e.group_id IN ({ph})
                          )
                    )
                    SELECT DISTINCT entity, MIN(depth) as min_depth FROM bfs
                    GROUP BY entity ORDER BY min_depth LIMIT ?"""
                    params = (seed, *scope_keys, k)
                cursor = await self._conn.execute(sql, params)
                rows = await cursor.fetchall()
                for r in rows:
                    results.append({
                        "type": "entity",
                        "id": r[0],
                        "name": r[0],
                        "graph_distance": r[1],
                    })
            except Exception as e:
                logger.debug("kg_search.graph_failed", seed=seed, error=str(e))
        return results

    async def _extract_query_entities(self, query: str) -> set[str]:
        """从查询中提取实体名 (简单分词, 无 LLM 调用)。"""
        # 简单实现: 按空格和标点分词, 取长度>=2的词
        # 生产环境可注入 KnowledgeGraph.get_query_entities
        tokens = re.split(r'[\s,，。.!！?？、的了吗呢吧]', query)
        return {t.strip() for t in tokens if len(t.strip()) >= 2}

    def _rrf_fuse(self, ranked_lists: list[list[dict]], k: int = 60) -> list[dict]:
        """Reciprocal Rank Fusion: score = Σ 1/(k + rank)。

        内部委托 utils.rank_fusion.reciprocal_rank_fusion（canonical，rank 从 1 起）。
        dict 候选先转 "type:id" 键列表融合，再回填 rrf_score。
        注意：rank 起点已从历史的 0 修正为标准 1，融合分数整体微降
        （如单路 rank1：1/60 → 1/61），相对排序不变（k=60 平滑）。
        """
        items: dict[str, dict] = {}
        key_lists: list[list[str]] = []
        for ranked in ranked_lists:
            keys: list[str] = []
            for item in ranked:
                key = f"{item.get('type', '')}:{item.get('id', '')}"
                if key not in items:
                    items[key] = item
                keys.append(key)
            key_lists.append(keys)
        fused = reciprocal_rank_fusion(key_lists, k=k, limit=len(items) or 1)
        return [{**items[key], "rrf_score": score} for key, score in fused]
