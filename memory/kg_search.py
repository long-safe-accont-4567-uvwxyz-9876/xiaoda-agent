"""KGSearchEngine — 混合检索引擎: 语义 + 全文 + 图遍历, RRF 融合。"""
import asyncio
import re
from typing import Any

import aiosqlite
from loguru import logger

from db.db_kg_v2 import KnowledgeDBV2
from db.fts_utils import _build_fts_query


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
    ) -> list[dict]:
        """混合检索: 语义 + 全文 + 图遍历, RRF 融合。

        Args:
            query: 查询文本
            top_k: 返回条数
            as_of: None=只返回当前有效; 时间戳=历史快照
        """
        # return_exceptions=True：单路检索异常不应阻断整体搜索
        raw = await asyncio.gather(
            self._semantic_search(query, top_k * 2),
            self._fulltext_search(query, top_k * 2),
            self._graph_search(query, top_k * 2),
            return_exceptions=True,
        )
        results: list[list[dict]] = []
        for idx, r in enumerate(raw):
            if isinstance(r, Exception):
                logger.warning("kg_search.sub_search_failed", idx=idx, error=str(r))
                results.append([])
            else:
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

    async def _semantic_search(self, query: str, k: int) -> list[dict]:
        """语义搜索: sqlite-vec KNN。"""
        if not self._vector_store:
            return []
        try:
            entity_hits = await self._vector_store.search_kg_entities(query, top_k=k)
            relation_hits = await self._vector_store.search_kg_relations(query, top_k=k)

            results = []
            # 实体命中
            for rowid, distance in entity_hits:
                cursor = await self._conn.execute(
                    "SELECT id, name, kind, summary FROM kg_entities_v2 WHERE rowid=?", (rowid,)
                )
                row = await cursor.fetchone()
                if row:
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
                    "SELECT id, from_entity, relation_type, to_entity, fact, valid_at, invalid_at, is_current "
                    "FROM kg_relations_v2 WHERE rowid=?", (rowid,)
                )
                row = await cursor.fetchone()
                if row:
                    results.append({
                        "type": "relation",
                        "id": row["id"],
                        "from_entity": row["from_entity"],
                        "relation_type": row["relation_type"],
                        "to_entity": row["to_entity"],
                        "fact": row["fact"],
                        "valid_at": row["valid_at"],
                        "invalid_at": row["invalid_at"],
                        "is_current": row["is_current"],
                        "distance": distance,
                    })
            return results
        except Exception as e:
            logger.debug("kg_search.semantic_failed", error=str(e))
            return []

    async def _fulltext_search(self, query: str, k: int) -> list[dict]:
        """FTS5 BM25 全文搜索 + CJK LIKE 降级。

        FTS5 默认 unicode61 分词器不拆分连续中文 (如 "用户喜欢篮球" 是单个 token),
        导致 MATCH '"篮球"' 无法命中。补充 LIKE 子串搜索作为降级, 确保中文 fact 可检索。
        """
        fts_query = _build_fts_query(query)
        results: list[dict] = []
        seen_keys: set[str] = set()

        if fts_query:
            try:
                # 实体: name + summary
                cursor = await self._conn.execute(
                    """SELECT e.id, e.name, e.kind, e.summary
                       FROM kg_entities_v2_fts
                       JOIN kg_entities_v2 e ON e.id = kg_entities_v2_fts.id
                       WHERE kg_entities_v2_fts MATCH ?
                       ORDER BY rank LIMIT ?""",
                    (fts_query, k),
                )
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
                # 关系: fact
                cursor = await self._conn.execute(
                    """SELECT r.id, r.from_entity, r.relation_type, r.to_entity, r.fact,
                              r.valid_at, r.invalid_at, r.is_current
                       FROM kg_relations_v2_fts
                       JOIN kg_relations_v2 r ON r.id = kg_relations_v2_fts.id
                       WHERE kg_relations_v2_fts MATCH ?
                       ORDER BY rank LIMIT ?""",
                    (fts_query, k),
                )
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
            cursor = await self._conn.execute(
                """SELECT id, from_entity, relation_type, to_entity, fact,
                          valid_at, invalid_at, is_current
                   FROM kg_relations_v2
                   WHERE fact LIKE ? ESCAPE '\\' LIMIT ?""",
                (like_pattern, k),
            )
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
                        "valid_at": row["valid_at"],
                        "invalid_at": row["invalid_at"],
                        "is_current": row["is_current"],
                    })
        except Exception as e:
            logger.debug("kg_search.fulltext_like_failed", error=str(e))
        return results

    async def _graph_search(self, query: str, k: int) -> list[dict]:
        """图遍历搜索: 递归 CTE BFS。"""
        entities = await self._extract_query_entities(query)
        if not entities:
            return []
        results = []
        for seed in list(entities)[:3]:
            try:
                cursor = await self._conn.execute(
                    """WITH RECURSIVE bfs(entity, depth) AS (
                        SELECT ?, 0
                        UNION ALL
                        SELECT CASE WHEN r.from_entity = b.entity THEN r.to_entity
                                    ELSE r.from_entity END, b.depth + 1
                        FROM kg_relations_v2 r JOIN bfs b
                          ON (r.from_entity = b.entity OR r.to_entity = b.entity)
                        WHERE b.depth < 2 AND r.is_current = 1
                    )
                    SELECT DISTINCT entity, MIN(depth) as min_depth FROM bfs
                    GROUP BY entity ORDER BY min_depth LIMIT ?""",
                    (seed, k),
                )
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
        """Reciprocal Rank Fusion: score = Σ 1/(k + rank)。"""
        scores: dict[str, float] = {}
        items: dict[str, dict] = {}
        for ranked in ranked_lists:
            for rank, item in enumerate(ranked):
                key = f"{item.get('type', '')}:{item.get('id', '')}"
                scores[key] = scores.get(key, 0) + 1.0 / (k + rank)
                if key not in items:
                    items[key] = item
        sorted_keys = sorted(scores.keys(), key=lambda x: -scores[x])
        return [{**items[key], "rrf_score": scores[key]} for key in sorted_keys]
