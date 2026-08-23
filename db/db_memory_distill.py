"""MemoryDB 的 enrichment + 画像/蒸馏方法组 —— 拆分自 db/db_memory.py。

Mixin 组合：MemoryDB 继承 DistillPortraitMixin 获得记忆 enrichment、
用户画像与记忆蒸馏方法。依赖 self._conn/_read_conn()/self._sync_fts()
（MRO）+ db_memory_utils 纯函数。
"""
from __future__ import annotations

import asyncio
import time

from loguru import logger

from db.db_memory_utils import _sql_placeholders


class DistillPortraitMixin:
    """enrichment + 画像/蒸馏方法组。"""

    async def update_memory_enrichment(self, memory_id: int, summary: str = "",
                                        entities: str = "", event_type: str = "",
                                        metadata_json: str = "", auto_commit: bool = True) -> bool:
        """后台 LLM 提取完成后，更新记忆条目的结构化字段。

        Args:
            memory_id: 记忆 ID
            summary: LLM 提取的更高质量摘要（可选，空则不更新）
            entities: 实体列表（JSON 字符串，如 '["小妲", "爸爸", "QQ"]'）
            event_type: 事件类型（如 '对话/决策/偏好/事件'）
            metadata_json: 元数据 JSON（如 '{"decision": "重启服务", "mood": "开心"}'）
        """
        try:
            sets = []
            params = []
            if summary:
                sets.append("summary = ?")
                params.append(summary)
            if entities:
                sets.append("entities = ?")
                params.append(entities)
            if event_type:
                sets.append("event_type = ?")
                params.append(event_type)
            if metadata_json:
                sets.append("metadata_json = ?")
                params.append(metadata_json)
            if not sets:
                return False
            params.append(memory_id)
            await self._conn.execute(
                f"UPDATE episodic_memories SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            if auto_commit:
                await self._conn.commit()
            # 如果 summary 更新了，同步更新 FTS 索引
            if summary:
                await self._sync_fts(memory_id, summary, "db_memory.fts_update_failed",
                                     auto_commit=auto_commit)
            return True
        except Exception as e:
            logger.warning("db_memory.enrichment_update_failed", error=str(e))
            return False

    async def merge_memory_knowledge_state(
        self,
        memory_id: int,
        *,
        summary: str,
        metadata_json: str,
        memory_type: str,
        importance: float,
        phase: str,
        stability: float,
        reinforcement_count: int,
        auto_commit: bool = True,
        strict: bool = False,
    ) -> bool:
        try:
            cursor = await self._conn.execute(
                "UPDATE episodic_memories SET summary=?, metadata_json=?, "
                "memory_type=?, importance=?, phase=?, stability=?, "
                "reinforcement_count=? WHERE id=? AND is_raw=0",
                (
                    summary,
                    metadata_json,
                    memory_type,
                    importance,
                    phase,
                    stability,
                    reinforcement_count,
                    memory_id,
                ),
            )
            if cursor.rowcount <= 0:
                if auto_commit:
                    await self._conn.commit()
                return False
            await self._sync_fts(
                memory_id,
                summary,
                "db_memory.knowledge_merge_fts_failed",
                auto_commit=False,
                strict=strict,
            )
            if auto_commit:
                await self._conn.commit()
            return True
        except asyncio.CancelledError:
            if auto_commit:
                await asyncio.shield(self._conn.rollback())
            raise
        except Exception as e:
            logger.warning("db_memory.knowledge_merge_failed", error=str(e))
            if auto_commit:
                await self._conn.rollback()
            if strict:
                raise
            return False

    async def insert_portrait(self, content: str, version: int = 1,
                               source_ids: str = "", change_log: str = "",
                               auto_commit: bool = True) -> int:
        cursor = await self._conn.execute(
            """INSERT INTO user_portrait (content, version, source_ids, change_log, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (content, version, source_ids, change_log, time.time()),
        )
        if auto_commit:
            await self._conn.commit()
        return cursor.lastrowid

    async def get_latest_portrait(self) -> dict | None:
        cursor = await self._read_conn().execute(
            "SELECT * FROM user_portrait ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def insert_consolidation_candidate(self, source: str, kind: str, summary: str,
                                              confidence: float = 0.5, importance: float = 0.5,
                                              metadata_json: str = "{}",
                                              auto_commit: bool = True) -> int:
        cursor = await self._conn.execute(
            """INSERT INTO consolidation_candidates
               (timestamp, source, kind, summary, confidence, importance, status, metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (time.time(), source, kind, summary, confidence, importance, metadata_json, time.time()),
        )
        if auto_commit:
            await self._conn.commit()
        return cursor.lastrowid

    async def mark_candidate_applied(self, candidate_id: int, target_memory_id: int,
                                      auto_commit: bool = True) -> None:
        await self._conn.execute(
            "UPDATE consolidation_candidates SET status='applied', target_memory_id=? WHERE id=?",
            (target_memory_id, candidate_id),
        )
        if auto_commit:
            await self._conn.commit()

    async def update_rag_status(self, memory_id: int, rag_status: str, rag_synced_at: float | None = None) -> None:
        """更新记忆的 RAG 索引状态"""
        valid_statuses = ('pending', 'indexed', 'failed', 'excluded')
        if rag_status not in valid_statuses:
            raise ValueError(f"rag_status must be one of {valid_statuses}, got '{rag_status}'")
        if rag_status == 'indexed' and rag_synced_at is None:
            rag_synced_at = time.time()
        if rag_synced_at is not None:
            await self._conn.execute(
                "UPDATE episodic_memories SET rag_status=?, rag_synced_at=? WHERE id=?",
                (rag_status, rag_synced_at, memory_id),
            )
        else:
            await self._conn.execute(
                "UPDATE episodic_memories SET rag_status=? WHERE id=?",
                (rag_status, memory_id),
            )
        await self._conn.commit()

    async def update_doc_id(self, memory_id: int, doc_id: str) -> None:
        """更新记忆关联的文档 ID"""
        await self._conn.execute(
            "UPDATE episodic_memories SET doc_id=? WHERE id=?",
            (doc_id, memory_id),
        )
        await self._conn.commit()

    async def get_pending_memories(self, limit: int = 100) -> list[dict]:
        """查询待索引的 RAG 记忆（rag_status='pending'），按时间升序"""
        cursor = await self._read_conn().execute(
            """SELECT id, timestamp, summary, importance FROM episodic_memories
               WHERE rag_status='pending'
               ORDER BY timestamp ASC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── P3 记忆蒸馏相关 ──────────────────────────────────────

    async def get_episodic_count_undistilled(self) -> int:
        """统计未蒸馏的情景记忆数量（distilled=0）"""
        try:
            cursor = await self._read_conn().execute(
                "SELECT COUNT(*) as cnt FROM episodic_memories WHERE distilled=0"
            )
            row = await cursor.fetchone()
            return row["cnt"] if row else 0
        except Exception as e:
            # 旧库可能没有 distilled 列，降级返回总计数
            logger.debug("db_memory.undistilled_count_failed", error=str(e))
            return await self.get_episodic_count()

    async def get_distill_candidates(self, limit: int = 30) -> list[dict]:
        """查询最旧的未蒸馏记忆（按时间升序），用于蒸馏压缩"""
        try:
            cursor = await self._read_conn().execute(
                """SELECT id, timestamp, summary, importance FROM episodic_memories
                   WHERE distilled=0
                   ORDER BY timestamp ASC LIMIT ?""",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("db_memory.distill_candidates_failed", error=str(e))
            return []

    async def mark_memories_distilled(self, memory_ids: list[int],
                                       auto_commit: bool = True) -> None:
        """将指定记忆标记为已蒸馏（distilled=1），保留不删除"""
        if not memory_ids:
            return
        placeholders = _sql_placeholders(memory_ids)
        try:
            await self._conn.execute(
                f"UPDATE episodic_memories SET distilled=1 WHERE id IN ({placeholders})",
                memory_ids,
            )
            if auto_commit:
                await self._conn.commit()
        except Exception as e:
            logger.warning("db_memory.mark_distilled_failed", error=str(e))

    async def insert_memory_summary(self, summary_text: str, memory_count: int,
                                     auto_commit: bool = True) -> int:
        """写入一条蒸馏摘要记录，返回摘要 id"""
        cursor = await self._conn.execute(
            """INSERT INTO memory_summaries (summary_text, created_at, memory_count)
               VALUES (?, ?, ?)""",
            (summary_text, time.time(), memory_count),
        )
        if auto_commit:
            await self._conn.commit()
        return cursor.lastrowid

    async def get_memory_summaries(self, limit: int = 5) -> list[dict]:
        """获取最近的蒸馏摘要（按时间降序）"""
        try:
            cursor = await self._read_conn().execute(
                """SELECT id, summary_text, created_at, memory_count
                   FROM memory_summaries
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("db_memory.get_summaries_failed", error=str(e))
            return []

    async def get_recent_undistilled(self, limit: int = 20) -> list[dict]:
        """获取最近的未蒸馏记忆（按时间降序），用于构建记忆提示"""
        try:
            cursor = await self._read_conn().execute(
                """SELECT id, timestamp, summary, importance FROM episodic_memories
                   WHERE distilled=0
                   ORDER BY timestamp DESC LIMIT ?""",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            # 旧库可能没有 distilled 列，降级返回所有最近记忆
            logger.debug("db_memory.recent_undistilled_failed", error=str(e))
            return await self.get_episodic_recent(limit=limit)
