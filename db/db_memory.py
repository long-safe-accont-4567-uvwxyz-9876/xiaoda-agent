import asyncio
import time
from typing import Any

import aiosqlite
from loguru import logger


# FTS 同步失败计数（模块级，便于外部观测/对账）。主表写入成功后 FTS 索引同步
# 失败会导致记忆"查不到"，此前仅 debug 静默吞掉，无任何告警与计数。
_fts_sync_failures = 0


def get_fts_sync_failures() -> int:
    """返回 FTS 同步失败累计计数（便于外部观测/对账）。"""
    return _fts_sync_failures


def _record_fts_sync_failure(event: str, error: Exception) -> None:
    """记录一次 FTS 同步失败：告警 + 递增计数，避免静默丢失索引。"""
    global _fts_sync_failures
    _fts_sync_failures += 1
    logger.warning(event, error=str(error), fts_sync_failures_total=_fts_sync_failures)

from db.db_memory_utils import (  # noqa: E402,F401
    compute_missing_vec_ids, _parse_entity_list, _sql_placeholders,
    _entity_like_conditions, _rows_to_entity_results, _scope_where,
    _rows_to_fts_results,
)
from db.db_memory_child import ChildChunkMixin
from db.db_memory_entity import EntityMixin
from db.db_memory_episodic import EpisodicMixin
from db.db_memory_search import SearchMixin
from db.db_memory_distill import DistillPortraitMixin
from db.db_memory_lifecycle import LifecycleMixin
from db.db_memory_emotion import EmotionRecallMixin



class MemoryDB(ChildChunkMixin, EntityMixin, EpisodicMixin, SearchMixin, DistillPortraitMixin, LifecycleMixin, EmotionRecallMixin):
    """管理情景记忆、画像等记忆数据的读写。"""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        conn.row_factory = aiosqlite.Row
        # 只读连接池（由 DatabaseManager.init 注入）：检索方法分流使用，
        # 避免 7 路检索通道排队同一个主连接导致总耗时=各通道之和
        self._read_pool: list[aiosqlite.Connection] = []
        self._read_idx = 0

    def _read_conn(self) -> aiosqlite.Connection:
        """取只读连接（round-robin），池空时回退主连接（保留原行为）。"""
        if not self._read_pool:
            return self._conn
        conn = self._read_pool[self._read_idx % len(self._read_pool)]
        self._read_idx += 1
        return conn

    async def commit(self) -> None:
        await self._conn.commit()

    async def rollback(self) -> None:
        """回滚当前事务。用于 auto_commit=False 批量操作失败时清理脏事务状态。

        根因：aiosqlite 单连接共享事务状态，auto_commit=False 操作若不 commit/rollback，
        事务会残留在连接上，后续协程的 DB 操作在脏事务中执行 → "SQL logic error"。
        """
        await self._conn.rollback()


    async def _sync_fts(self, memory_id: int, summary: str, event_label: str, *,
                        delete_first: bool = True, auto_commit: bool = False) -> None:
        """同步 episodic_memory_fts 索引：分词 → (可选 DELETE) → INSERT → (可选 commit)。

        失败统一走 _record_fts_sync_failure（告警 + 计数），不抛出。
        """
        try:
            from db.fts_utils import _tokenize_for_fts
            tokenized = _tokenize_for_fts(summary)
            if tokenized.strip():
                if delete_first:
                    await self._conn.execute(
                        "DELETE FROM episodic_memory_fts WHERE id = ?", (memory_id,))
                await self._conn.execute(
                    "INSERT INTO episodic_memory_fts(id, summary_index) VALUES(?, ?)",
                    (memory_id, tokenized),
                )
                if auto_commit:
                    await self._conn.commit()
        except Exception as e:
            _record_fts_sync_failure(event_label, e)




