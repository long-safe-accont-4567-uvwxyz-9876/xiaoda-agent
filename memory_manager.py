import asyncio
import time
from loguru import logger

from database import DatabaseManager
from db_memory import MemoryDB
from vector_store import VectorStore


class MemoryManager:

    IDLE_THRESHOLD = 30
    ENCODE_COOLDOWN = 60

    def __init__(self, db: DatabaseManager, memory: MemoryDB,
                 vector_store: VectorStore | None = None,
                 router=None, knowledge_graph=None):
        self.db = db
        self.memory = memory
        self.vec = vector_store
        self.router = router
        self.kg = knowledge_graph
        self._last_message_time: float = 0
        self._last_encode_time: float = 0
        self._pending_encode = False

    def set_knowledge_graph(self, kg):
        self.kg = kg

    def signal_new_message(self):
        self._last_message_time = time.time()
        self._pending_encode = True

    async def retrieve_memories(self, query: str, k: int = 5) -> list[dict]:
        results = []

        if self.vec:
            try:
                vec_results = await self.vec.search(query, top_k=k)
                if vec_results:
                    for row_id, distance in vec_results:
                        mem = await self.memory.get_memory_by_id(row_id)
                        if mem:
                            mem["score"] = 1.0 - distance
                            results.append(mem)
            except Exception as e:
                logger.warning("memory.vec_search_failed", error=str(e))

        if not results and self.memory:
            try:
                results = await self.memory.search_memories_by_importance(
                    min_importance=0.4, limit=k
                )
            except Exception as e:
                logger.warning("memory.fallback_search_failed", error=str(e))

        now = time.time()
        for r in results:
            age_hours = (now - r.get("timestamp", 0)) / 3600
            importance = r.get("importance", 0.5)
            r["effective_score"] = importance * max(0.1, 1.0 - age_hours / 168)

        results.sort(key=lambda x: x.get("effective_score", 0), reverse=True)
        results = results[:k]

        if self.kg and results:
            try:
                entity_names = []
                for r in results[:2]:
                    summary = r.get("summary", "")
                    for word in summary.split():
                        if len(word) >= 2 and word not in ("用户", "助手", "人家"):
                            entity_names.append(word)
                entity_names = list(set(entity_names))[:3]
                if entity_names:
                    knowledge = await self.kg.get_related_knowledge(entity_names)
                    if knowledge:
                        kg_context = await self.kg.format_knowledge_context(knowledge)
                        if kg_context and results:
                            results[0]["kg_context"] = kg_context
            except Exception as e:
                logger.debug("memory.kg_expand_failed", error=str(e))

        return results

    async def encode_memory(self, context: dict):
        exchanges = context.get("exchanges", [])
        if not exchanges or len(exchanges) < 2:
            return

        summary = self._generate_summary(exchanges)

        importance = self._estimate_importance(exchanges, context)
        emotion = context.get("emotion", {}).get("primary", "")

        try:
            mem_id = await self.memory.insert_episodic_memory(
                summary=summary,
                importance=importance,
                emotion_label=emotion,
            )

            if self.vec and summary:
                await self.vec.upsert(mem_id, summary)

            self._last_encode_time = time.time()
            self._pending_encode = False
            logger.info("memory.encoded", summary=summary[:80], importance=importance)
        except Exception as e:
            logger.warning("memory.encode_failed", error=str(e))

        if self.kg and summary:
            try:
                await self.kg.auto_extract_and_merge(summary)
            except Exception as e:
                logger.debug("memory.kg_extract_failed", error=str(e))

    def _generate_summary(self, exchanges: list[dict]) -> str:
        parts = []
        for msg in exchanges[-6:]:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user" and content:
                parts.append(f"用户: {content[:100]}")
            elif role == "assistant" and content:
                parts.append(f"助手: {content[:100]}")

        summary = " | ".join(parts)
        return summary[:500]

    def _estimate_importance(self, exchanges: list[dict], context: dict) -> float:
        importance = 0.3

        emotion = context.get("emotion", {})
        if emotion.get("primary") in ("悲伤", "愤怒", "焦虑", "恐惧"):
            importance += 0.3
        elif emotion.get("primary") in ("喜悦", "感激", "期待"):
            importance += 0.1

        total_len = sum(len(m.get("content", "")) for m in exchanges)
        if total_len > 500:
            importance += 0.2

        return min(importance, 1.0)

    async def try_idle_encode(self, context: dict):
        now = time.time()
        if not self._pending_encode:
            return
        if now - self._last_message_time < self.IDLE_THRESHOLD:
            return
        if now - self._last_encode_time < self.ENCODE_COOLDOWN:
            return

        await self.encode_memory(context)

    async def shutdown(self) -> str:
        if self.vec:
            await self.vec.close()
        return "done"
