"""EntityStore — 实体存储管理与反向链接。

职责：
1. link_entities: 将提取的实体链接到记忆（FTS5 名称匹配 + 反向链接）
2. recall_by_entities: 通过实体名反向查询记忆（第5路召回核心）
3. compute_entity_boost: 计算 Entity Boost（精排加分）

参考 mem0 原版 scoring.py 的 Entity Boost 公式。
"""
import time
from typing import Any
from loguru import logger

from memory.entity_extractor import Entity
from memory.scope import Scope


# Entity Boost 权重（与 mem0 原版一致）
ENTITY_BOOST_WEIGHT = 0.5


def compute_entity_boost(entity: dict, query_entities: set[str],
                          now: float | None = None) -> float:
    """计算实体对查询的 boost 值。

    参考 mem0 原版 scoring.py：
    boost = similarity × ENTITY_BOOST_WEIGHT × memory_count_weight × recency

    Args:
        entity: 实体 dict，包含 name/memory_count/last_seen
        query_entities: 查询中提取的实体名集合
        now: 当前时间戳（测试可注入），None 则用 time.time()
    Returns:
        Entity Boost 值 [0, ENTITY_BOOST_WEIGHT]
    """
    if now is None:
        now = time.time()

    # 1. 实体与查询实体的匹配度
    similarity = 1.0 if entity.get("name", "") in query_entities else 0.0
    if similarity == 0.0:
        return 0.0

    # 2. 记忆数权重：链接记忆越多越重要，但边际递减
    #    采用线性衰减 1/(1 + 0.001*(count-1))，保证 count=100 时权重仍 >= count=5 的 90%
    count = max(1, entity.get("memory_count", 1))
    memory_count_weight = 1.0 / (1.0 + 0.001 * (count - 1))

    # 3. 时间衰减因子（天级）
    last_seen = entity.get("last_seen", now)
    recency = 1.0 / (1.0 + max(0, now - last_seen) / 86400.0)

    # 4. Entity Boost
    return similarity * ENTITY_BOOST_WEIGHT * memory_count_weight * recency


class EntityStore:
    """实体存储管理：CRUD + 反向链接查询。

    与 KG 的 knowledge_graph.py 职责分离：
    - KG: 知识图谱（实体+关系+观察），用于推理
    - EntityStore: 记忆实体链接（实体↔记忆反向链接），用于检索召回
    """

    def __init__(self, memory_db: Any) -> None:
        """
        Args:
            memory_db: MemoryDB 实例（提供实体 CRUD 方法）
        """
        self.db = memory_db
        logger.info("entity_store.ready")

    async def link_entities(self, memory_id: int, entities: list[Entity],
                             scope: Scope | None = None) -> int:
        """将提取的实体链接到记忆。

        流程：
        1. FTS5/精确匹配查找已有实体
        2. 找到 → 建立反向链接 + memory_count++
        3. 未找到 → 创建新实体 + 建立反向链接
        4. 更新实体 last_seen（时间感知）

        Args:
            memory_id: 记忆 ID
            entities: Entity 列表
            scope: Scope 对象（用于未来扩展，当前链接不按 scope 隔离实体）
        Returns:
            成功链接的实体数
        """
        if not entities:
            return 0

        linked = 0
        for entity in entities:
            try:
                # 1. 查找已有实体（精确匹配 name + entity_type）
                existing = await self.db.find_memory_entity_by_name(
                    entity.name, entity.entity_type
                )

                if existing:
                    # 2a. 找到匹配 → 建立反向链接
                    link_id = await self.db.insert_entity_memory_link(
                        entity_id=existing["id"],
                        memory_id=memory_id,
                        confidence=entity.confidence,
                    )
                    if link_id is not None:
                        # 新链接才递增 count
                        await self.db.increment_entity_memory_count(existing["id"])
                    # 更新 last_seen
                    await self.db.update_entity_last_seen(existing["id"])
                else:
                    # 2b. 无匹配 → 创建新实体
                    new_id = await self.db.insert_memory_entity(
                        name=entity.name,
                        entity_type=entity.entity_type,
                        kind=entity.kind,
                    )
                    if new_id is not None:
                        await self.db.insert_entity_memory_link(
                            entity_id=new_id,
                            memory_id=memory_id,
                            confidence=entity.confidence,
                        )
                        # 新实体 memory_count 从 0 → 1
                        await self.db.increment_entity_memory_count(new_id)
                linked += 1
            except Exception as e:
                logger.debug("entity_store.link_failed",
                             entity=entity.name, error=str(e))
        return linked

    async def recall_by_entities(self, entity_names: list[str],
                                  scope: Scope, limit: int = 10,
                                  is_raw: int | None = 0) -> list[dict]:
        """通过实体名反向查询关联的记忆（检索时第5路召回）。

        Args:
            entity_names: 实体名列表
            scope: Scope 对象（scope 隔离）
            limit: 返回条数上限
            is_raw: None=不限, 0=只查提炼知识（默认）, 1=只查原始记录
        Returns:
            记忆 dict 列表
        """
        if not entity_names:
            return []
        try:
            return await self.db.get_memories_by_entity_names_scoped(
                entity_names, scope=scope, limit=limit, is_raw=is_raw
            )
        except Exception as e:
            logger.debug("entity_store.recall_failed", error=str(e))
            return []

    async def get_query_entities_boost(self, memory_id: int,
                                        query_entities: set[str],
                                        now: float | None = None) -> float:
        """计算指定记忆关联的所有实体对查询的总 boost 值。

        用于精排阶段：对每个候选记忆，计算其关联实体与查询实体的 boost 之和。

        Args:
            memory_id: 记忆 ID
            query_entities: 查询中提取的实体名集合
            now: 当前时间戳
        Returns:
            总 Entity Boost 值（0 表示无匹配）
        """
        try:
            entities = await self.db.get_entities_by_memory_id(memory_id)
            if not entities:
                return 0.0
            total_boost = 0.0
            for entity in entities:
                total_boost += compute_entity_boost(entity, query_entities, now)
            return total_boost
        except Exception as e:
            logger.debug("entity_store.get_boost_failed", error=str(e))
            return 0.0
