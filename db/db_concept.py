"""概念图数据库 CRUD — concept_nodes / concept_edges / concept_meta 表操作"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from loguru import logger

_SH_TZ = ZoneInfo("Asia/Shanghai")


def _now_iso() -> str:
    """返回 Asia/Shanghai 时区的 ISO 时间戳"""
    return datetime.now(_SH_TZ).isoformat()


class ConceptDB:
    """概念图数据库访问层（异步 aiosqlite）"""

    def __init__(self, conn):
        self._conn = conn

    async def insert_node(self, id: str, text: str, keys: str,
                          weight: float = 1.0, peak_weight: float = 1.0,
                          confidence: float = 1.0, access_count: int = 0,
                          layer: str = "hippocampus",
                          created: str | None = None,
                          last_accessed: str | None = None,
                          valid_from: str | None = None,
                          valid_to: str | None = None,
                          superseded_by: str | None = None,
                          history: str = "[]",
                          origin: str = "{}",
                          source_mem_id: int | None = None,
                          embedding=None,
                          difficulty: float = 5.0,
                          stability: float = 3.0,
                          phase: str = "buffer",
                          last_review: float = 0.0,
                          reinforcement_count: int = 0) -> None:
        """插入概念节点。keys 为 JSON 字符串。"""
        now = created or _now_iso()
        await self._conn.execute(
            """INSERT OR REPLACE INTO concept_nodes
               (id, text, weight, peak_weight, confidence, access_count, keys,
                layer, created, last_accessed, valid_from, valid_to,
                superseded_by, history, origin, source_mem_id, embedding,
                difficulty, stability, phase, last_review, reinforcement_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?)""",
            (id, text, weight, peak_weight, confidence, access_count, keys,
             layer, now, last_accessed or now, valid_from or now, valid_to,
             superseded_by, history, origin, source_mem_id, embedding,
             difficulty, stability, phase, last_review, reinforcement_count),
        )
        await self._conn.commit()

    async def get_node(self, node_id: str) -> dict | None:
        async with self._conn.execute(
            "SELECT * FROM concept_nodes WHERE id = ?", (node_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_node_by_source_mem(self, mem_id: int) -> dict | None:
        async with self._conn.execute(
            "SELECT * FROM concept_nodes WHERE source_mem_id = ?", (mem_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def update_node(self, node_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [node_id]
        await self._conn.execute(
            f"UPDATE concept_nodes SET {cols} WHERE id = ?", vals
        )
        await self._conn.commit()

    async def get_alive_nodes(self) -> dict[str, dict]:
        """返回所有有效节点（valid_to IS NULL）"""
        async with self._conn.execute(
            "SELECT * FROM concept_nodes WHERE valid_to IS NULL"
        ) as cur:
            rows = await cur.fetchall()
            return {row["id"]: dict(row) for row in rows}

    async def get_node_count(self) -> int:
        async with self._conn.execute(
            "SELECT COUNT(*) FROM concept_nodes WHERE valid_to IS NULL"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def create_edge(self, source_id: str, target_id: str,
                           relation: str = "related", weight: float = 1.0,
                           created: str | None = None) -> None:
        now = created or _now_iso()
        await self._conn.execute(
            """INSERT OR REPLACE INTO concept_edges
               (source_id, target_id, relation, weight, created)
               VALUES (?, ?, ?, ?, ?)""",
            (source_id, target_id, relation, weight, now),
        )
        await self._conn.commit()

    async def get_edges(self, node_id: str) -> dict[str, dict]:
        async with self._conn.execute(
            "SELECT * FROM concept_edges WHERE source_id = ?", (node_id,)
        ) as cur:
            rows = await cur.fetchall()
            return {row["target_id"]: dict(row) for row in rows}

    async def update_edge(self, source_id: str, target_id: str,
                           weight: float | None = None,
                           relation: str | None = None) -> None:
        fields = {}
        if weight is not None:
            fields["weight"] = weight
        if relation is not None:
            fields["relation"] = relation
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [source_id, target_id]
        await self._conn.execute(
            f"UPDATE concept_edges SET {cols} WHERE source_id = ? AND target_id = ?",
            vals,
        )
        await self._conn.commit()

    async def auto_link(self, node_id: str, keys: list[str],
                         min_shared: int = 3) -> int:
        """与共享 ≥ min_shared 个 keys 的存活节点自动建边。返回建边数。"""
        if not keys:
            return 0
        alive = await self.get_alive_nodes()
        count = 0
        key_set = set(keys)
        now = _now_iso()
        for nid, node in alive.items():
            if nid == node_id:
                continue
            try:
                node_keys = set(json.loads(node.get("keys", "[]")))
            except (json.JSONDecodeError, TypeError):
                continue
            shared = key_set & node_keys
            if len(shared) >= min_shared:
                # 双向建边
                await self.create_edge(node_id, nid, "co-occurrence", 1.0, now)
                await self.create_edge(nid, node_id, "co-occurrence", 1.0, now)
                count += 1
        return count

    async def get_meta(self, key: str) -> str | None:
        async with self._conn.execute(
            "SELECT value FROM concept_meta WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO concept_meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        await self._conn.commit()