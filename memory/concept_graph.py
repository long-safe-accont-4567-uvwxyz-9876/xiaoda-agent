"""概念图管理器 — Hippocampus 层节点/边管理 + auto_link + 懒迁移"""
import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from loguru import logger

from db.db_concept import ConceptDB
from memory.key_extractor import KeyExtractor

_SH_TZ = ZoneInfo("Asia/Shanghai")


class ConceptGraph:
    """概念图管理器（Hippocampus 层）

    职责：
    1. remember(): 新记忆写入 concept_nodes + auto_link
    2. lazy_migrate(): 旧 episodic_memories 懒迁移到 concept_nodes
    """

    def __init__(self, concept_db: ConceptDB, key_extractor: KeyExtractor):
        self.db = concept_db
        self.ke = key_extractor

    def _clean_text(self, text: str) -> str:
        """清理文本：去首尾空白"""
        return text.strip()

    def _make_node_id(self, text: str) -> str:
        """生成节点 ID：md5(cleaned_text)[:12]"""
        cleaned = self._clean_text(text)
        return hashlib.md5(cleaned.encode("utf-8")).hexdigest()[:12]

    async def remember(self, text: str,
                        source_mem_id: int | None = None) -> str:
        """新记忆写入概念图

        1. 清理文本，生成 node_id
        2. 提取 keys
        3. 插入 concept_nodes（若已存在则跳过）
        4. auto_link：与共享 ≥3 keys 的节点建边

        Returns:
            node_id
        """
        cleaned = self._clean_text(text)
        if not cleaned:
            return ""

        node_id = self._make_node_id(cleaned)
        # 检查是否已存在
        existing = await self.db.get_node(node_id)
        if existing:
            return node_id

        keys = self.ke.extract(cleaned, is_query=False)
        now = datetime.now(_SH_TZ).isoformat()

        await self.db.insert_node(
            id=node_id, text=cleaned,
            keys=json.dumps(keys, ensure_ascii=False),
            weight=1.0, peak_weight=1.0, confidence=1.0,
            access_count=0, layer="hippocampus",
            created=now, last_accessed=now,
            valid_from=now, valid_to=None,
            source_mem_id=source_mem_id,
        )

        # auto_link
        if keys:
            link_count = await self.db.auto_link(node_id, keys, min_shared=3)
            if link_count:
                logger.debug("concept_graph.auto_linked",
                             node=node_id, links=link_count)

        return node_id

    async def lazy_migrate(self, episodic_memories: list[dict],
                            limit: int = 50) -> int:
        """懒迁移：将旧 episodic_memories 迁移到 concept_nodes

        已迁移的（source_mem_id 已存在）跳过。

        Args:
            episodic_memories: [{"id": int, "summary": str}, ...]
            limit: 最多迁移数量

        Returns:
            实际迁移数量
        """
        count = 0
        for mem in episodic_memories[:limit]:
            mem_id = mem.get("id")
            summary = mem.get("summary", "")
            if not summary:
                continue
            # 检查是否已迁移
            existing = await self.db.get_node_by_source_mem(mem_id)
            if existing:
                continue
            await self.remember(summary, source_mem_id=mem_id)
            count += 1
        if count:
            logger.info("concept_graph.lazy_migrated", count=count)
        return count

    async def get_node(self, node_id: str) -> dict | None:
        return await self.db.get_node(node_id)

    async def get_node_by_source_mem(self, mem_id: int) -> dict | None:
        return await self.db.get_node_by_source_mem(mem_id)

    async def get_edges(self, node_id: str) -> dict[str, dict]:
        return await self.db.get_edges(node_id)
