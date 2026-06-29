"""情景记忆行数上限 (H1) — MAX_EPISODIC_ROWS 生效

参考:
- Episodic memory consolidation (CLP theory)
- LRU + importance-based eviction

特性:
- 配置项 MAX_EPISODIC_ROWS (默认 10000)
- 超过上限时按 importance + access_count + recency 综合评分淘汰
- 淘汰前归档到 distilled 表 (可选)
- 定期触发 (可挂载到 Dream Consolidation)
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from loguru import logger


class EpisodicLimiter:
    """情景记忆行数限制器

    用法:
        limiter = EpisodicLimiter(db_manager)
        # 检查并清理
        pruned = await limiter.enforce_limit()
        # 或挂到定时任务
        limiter.start_scheduler(interval=3600)
    """

    DEFAULT_MAX_ROWS = 10000
    DEFAULT_BATCH_SIZE = 500

    def __init__(self, db_manager: Any, max_rows: Optional[int] = None,
                 batch_size: int = DEFAULT_BATCH_SIZE) -> None:
        self._db = db_manager
        self._max_rows = max_rows or self.DEFAULT_MAX_ROWS
        self._batch_size = batch_size
        self._task: Optional[asyncio.Task] = None

    async def count_rows(self) -> int:
        """统计当前行数"""
        try:
            cursor = await self._db._conn.execute(
                "SELECT COUNT(*) FROM episodic_memories"
            )
            row = await cursor.fetchone()
            return row[0] if row else 0
        except Exception as e:
            logger.warning(f"EpisodicLimiter.count_failed: {e}")
            return 0

    async def enforce_limit(self) -> int:
        """执行上限清理, 返回清理的行数"""
        try:
            count = await self.count_rows()
            if count <= self._max_rows:
                return 0
            excess = count - self._max_rows
            # 按综合评分排序, 淘汰评分最低的
            # score = importance * 0.5 + (access_count / 10) * 0.3 + recency * 0.2
            now = time.time()
            cursor = await self._db._conn.execute(
                """
                SELECT id FROM episodic_memories
                WHERE session_id != 'archived'
                ORDER BY
                    (importance * 0.5
                     + MIN(access_count, 10) * 0.03
                     + MAX(0, 1 - (CAST(strftime('%s', 'now') AS REAL) - timestamp) / 86400.0) * 0.2) ASC
                LIMIT ?
                """,
                (excess,)
            )
            ids_to_prune = [row[0] for row in await cursor.fetchall()]
            if not ids_to_prune:
                return 0

            # 归档而非删除 (移到 archived session)
            placeholders = ",".join("?" * len(ids_to_prune))
            await self._db._conn.execute(
                f"UPDATE episodic_memories SET session_id='archived' "
                f"WHERE id IN ({placeholders})",
                ids_to_prune
            )
            await self._db._conn.commit()
            logger.info(f"EpisodicLimiter.puned count={len(ids_to_prune)} "
                         f"total={count} max={self._max_rows}")
            return len(ids_to_prune)
        except Exception as e:
            logger.error(f"EpisodicLimiter.enforce_failed: {e}")
            return 0

    def start_scheduler(self, interval: float = 3600.0) -> Optional[asyncio.Task]:
        """启动定期清理任务"""
        async def _loop() -> None:
            while True:
                try:
                    await self.enforce_limit()
                except Exception as e:
                    logger.error(f"EpisodicLimiter.loop_error: {e}")
                await asyncio.sleep(interval)
        try:
            loop = asyncio.get_event_loop()
            self._task = loop.create_task(_loop())
            return self._task
        except RuntimeError:
            return None

    def stop_scheduler(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    def set_max_rows(self, n: int) -> None:
        """运行时调整上限"""
        self._max_rows = max(100, n)

    def stats(self) -> dict:
        return {
            "max_rows": self._max_rows,
            "batch_size": self._batch_size,
            "scheduler_running": self._task is not None,
        }


# 全局单例
_limiter: Optional[EpisodicLimiter] = None


def get_episodic_limiter(db_manager: Any | None=None) -> EpisodicLimiter:
    global _limiter
    if _limiter is None and db_manager is not None:
        _limiter = EpisodicLimiter(db_manager)
    return _limiter  # type: ignore
