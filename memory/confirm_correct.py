"""Confirm/Correct 机制 — 记忆强化与纠正

confirm: 确认强化（access_count+1, weight+0.15, edges+0.25）
correct: 纠正超驰（创建新节点，关闭旧节点，迁移边，建立 supersedes 链）
"""
import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from loguru import logger

_SH_TZ = ZoneInfo("Asia/Shanghai")


class ConfirmCorrect:
    """confirm: 确认强化 / correct: 纠正超驰"""

    BOOST_PER_ACCESS = 0.15    # 每次确认的节点权重增量
    EDGE_BOOST = 0.25          # 确认时边权重增量

    def __init__(self, concept_db, spreading_engine, memory_db,
                 key_extractor):
        self.db = concept_db
        self.engine = spreading_engine
        self.memory = memory_db  # Database 实例（用于同步 episodic_memories）
        self.ke = key_extractor

    def _now_iso(self) -> str:
        return datetime.now(_SH_TZ).isoformat()

    def _clean_text(self, text: str) -> str:
        return text.strip()

    def _make_node_id(self, text: str) -> str:
        cleaned = self._clean_text(text)
        return hashlib.md5(cleaned.encode("utf-8")).hexdigest()[:12]

    async def confirm(self, node_ids: list[str]) -> dict:
        """确认强化

        1. access_count += 1
        2. weight = min(1.0, weight + 0.15)
        3. peak_weight = max(peak_weight, weight)
        4. last_accessed = now
        5. 所有关联边 weight += 0.25 (双向同步)
        6. 同步 episodic_memories.access_count
        """
        now = self._now_iso()
        reinforced = 0
        unknown = 0

        for nid in node_ids:
            node = await self.db.get_node(nid)
            if node is None:
                unknown += 1
                continue

            new_access = node["access_count"] + 1
            new_weight = min(1.0, node["weight"] + self.BOOST_PER_ACCESS)
            new_peak = max(node["peak_weight"], new_weight)

            await self.db.update_node(
                nid, access_count=new_access, weight=new_weight,
                peak_weight=new_peak, last_accessed=now)

            # 强化所有关联边（双向同步）
            edges = await self.db.get_edges(nid)
            for target_id, edge in edges.items():
                new_edge_w = min(1.0, edge["weight"] + self.EDGE_BOOST)
                await self.db.update_edge(nid, target_id, weight=new_edge_w)
                await self.db.update_edge(target_id, nid, weight=new_edge_w)

            # 同步 episodic_memories
            if node.get("source_mem_id"):
                try:
                    await self.memory.increment_access_count(
                        node["source_mem_id"])
                except Exception as e:
                    logger.debug("confirm.sync_episodic_failed",
                                 error=str(e))

            reinforced += 1

        return {"reinforced": reinforced, "unknown": unknown}

    async def correct(self, old_hint: str, new_text: str) -> dict:
        """纠正超驰（融合而非擦除）

        1. recall 找到最匹配旧记忆
        2. 验证匹配质量（共享 ≥2 token 或覆盖 ≥50%）
        3. 创建新节点（继承权重, confidence×0.7）
        4. 迁移旧节点的知识边到新节点（不迁移 supersedes 边）
        5. 建立双向 supersedes/superseded-by 边 (weight=0.5)
        6. 关闭旧节点 (valid_to = now, superseded_by = new_id)
        7. 保留 history 溯源链
        """
        # 1. 找到旧记忆
        results = await self.engine.recall(old_hint, top_k=1)
        if not results:
            return {"error": "no match"}

        old_id = results[0]["id"]
        old_node = results[0]
        old_text = old_node["text"]

        # 2. 验证匹配质量
        hint_tokens = set(self.ke.extract(old_hint))
        node_tokens = set(self.ke.extract(old_text))
        shared = hint_tokens & node_tokens
        if not (len(shared) >= 2 or
                (node_tokens and len(shared) / len(node_tokens) >= 0.5)):
            return {"error": "insufficient match quality"}

        # 3. 创建新节点
        now = self._now_iso()
        new_id = self._make_node_id(new_text)
        lowered_conf = round(old_node.get("confidence", 1.0) * 0.7, 3)

        history = json.loads(old_node.get("history", "[]"))
        history.append({"text": old_text, "replaced": now})

        new_keys = self.ke.extract(new_text, is_query=False)

        await self.db.insert_node(
            id=new_id, text=self._clean_text(new_text),
            weight=old_node.get("weight", 1.0),
            peak_weight=old_node.get("peak_weight", 1.0),
            confidence=lowered_conf, access_count=0,
            keys=json.dumps(new_keys, ensure_ascii=False),
            layer="hippocampus",
            created=now, last_accessed=now,
            valid_from=now, valid_to=None,
            superseded_by=None, history=json.dumps(history, ensure_ascii=False),
            origin=json.dumps({"via": "correct"}),
        )

        # 4. 迁移旧节点的知识边（不迁移 supersedes 边）
        old_edges = await self.db.get_edges(old_id)
        for target_id, edge in old_edges.items():
            if edge["relation"] in ("supersedes", "superseded-by"):
                continue
            if target_id == new_id:
                continue
            await self.db.create_edge(new_id, target_id,
                                       edge["relation"], edge["weight"], now)
            await self.db.create_edge(target_id, new_id,
                                       edge["relation"], edge["weight"], now)

        # 5. supersedes 双向边
        await self.db.create_edge(new_id, old_id, "supersedes", 0.5, now)
        await self.db.create_edge(old_id, new_id, "superseded-by", 0.5, now)

        # 6. 关闭旧节点
        await self.db.update_node(old_id, valid_to=now,
                                   superseded_by=new_id)

        logger.info("correct.applied", old_id=old_id, new_id=new_id)
        return {
            "old_text": old_text, "new_text": new_text,
            "old_id": old_id, "new_id": new_id,
        }
