"""ConversationLogMixin — 对话日志 / 审计日志 / 过期数据清理。

自 db/database.py 拆分（上帝文件 Phase 3）：函数体逐字节搬移，仅缩进调整。
依赖 DatabaseManager 的 self._conn / self.write_transaction / self.update_session
（mixin 方法可调用组合类成员，由 MRO 保证）。
"""
from __future__ import annotations

import contextlib
import sqlite3
import time

from loguru import logger


class ConversationLogMixin:
    async def insert_conversation_log(self, user_id: str, source: str,
                                       user_message: str, assistant_reply: str,
                                       emotion_label: str = "", model_used: str = "",
                                       session_id: str = "",
                                       auto_commit: bool = True,
                                       request_context_json: str = "{}") -> None:
        await self._conn.execute(
            """INSERT INTO conversation_logs
               (timestamp, user_id, source, user_message, assistant_reply, emotion_label, model_used, session_id, request_context_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), user_id, source, user_message, assistant_reply, emotion_label,
             model_used, session_id, request_context_json),
        )
        if auto_commit:
            await self._conn.commit()

    async def get_recent_replies(self, user_id: str, source: str = "",
                                  limit: int = 5) -> list[str]:
        """查询指定用户最近的回复（跨对话去重，替代易失的内存缓存）。

        根因修复（用户反馈"每段对话80%一样"）：
          原去重机制依赖内存变量 _recent_replies，存在两个致命问题：
          1. 服务重启后内存清空 → 去重历史丢失 → 对相同输入生成相同回复
          2. 去 key 用 session_id，而 session_id 不稳定：
             - 微信 adapter 根本没传 session_id（空串）
             - QQ c2c 的 session_id 每小时换一次（SES-YYYYMMDD-XXXXX）
             → key 频繁变化 → 内存缓存命中失败 → 去重失效
          修复：从 conversation_logs 按 user_id（稳定标识 wechat_xxx/qq_xxx）查询
                最近 N 条非空回复，确保重启后/换 session 后去重状态不丢失。

        Args:
            user_id: 稳定用户标识（wechat_{openid} / qq_{openid}），与写库时的 user_id 一致
            source: 消息来源过滤（qq_c2c/wechat_c2c 等），空串则不限来源
            limit: 返回最近 N 条回复（按时间倒序，最新在前）

        Returns:
            回复文本列表（最新在前），查询失败返回空列表（降级跳过去重，不阻塞主流程）
        """
        try:
            if source:
                cursor = await self._conn.execute(
                    "SELECT assistant_reply FROM conversation_logs "
                    "WHERE user_id=? AND source=? AND assistant_reply != '' "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (user_id, source, limit),
                )
            else:
                cursor = await self._conn.execute(
                    "SELECT assistant_reply FROM conversation_logs "
                    "WHERE user_id=? AND assistant_reply != '' "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (user_id, limit),
                )
            rows = await cursor.fetchall()
            # rows 按 timestamp DESC，最新在前；返回时保持最新在前
            return [row[0] for row in rows if row[0]]
        except (OSError, RuntimeError, sqlite3.Error) as e:
            logger.warning("database.get_recent_replies_failed user_id={} error={}",
                           user_id[:24] if user_id else "", str(e)[:200])
            return []

    async def get_recent_exchanges(self, user_id: str, source: str = "",
                                   limit: int = 5) -> list[tuple[str, str]]:
        """查询最近 N 轮 (user_message, assistant_reply) 交换对，最新在前。

        回复去重升级：仅取"最近 1 条回复"在多轮交替消息后窗口过早失效
        （用户同类消息隔 5-10 轮后再发，重复回复已是 DB 里更早的一条），
        导致相同输入仍生成相同回复。按交换对取出用户消息 + 回复，
        由 _dedup_reply_against_recent 用用户消息相似度定位候选窗口。

        Args:
            user_id: 稳定用户标识，与写库时一致
            source: 消息来源过滤，空串则不限来源
            limit: 返回最近 N 轮（最新在前）

        Returns:
            [(user_message, assistant_reply), ...]；查询失败返回 []（降级跳过去重）
        """
        try:
            if source:
                cursor = await self._conn.execute(
                    "SELECT user_message, assistant_reply FROM conversation_logs "
                    "WHERE user_id=? AND source=? AND assistant_reply != '' "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (user_id, source, limit),
                )
            else:
                cursor = await self._conn.execute(
                    "SELECT user_message, assistant_reply FROM conversation_logs "
                    "WHERE user_id=? AND assistant_reply != '' "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (user_id, limit),
                )
            rows = await cursor.fetchall()
            return [(row[0] or "", row[1]) for row in rows]
        except (OSError, RuntimeError, sqlite3.Error) as e:
            logger.warning("database.get_recent_exchanges_failed user_id={} error={}",
                           user_id[:24] if user_id else "", str(e)[:200])
            return []

    async def insert_audit_log(self, event_type: str, user_id: str = "", detail: str = "",
                                auto_commit: bool = True) -> None:
        await self._conn.execute(
            """INSERT INTO audit_logs (timestamp, event_type, user_id, detail)
               VALUES (?, ?, ?, ?)""",
            (time.time(), event_type, user_id, detail),
        )
        if auto_commit:
            await self._conn.commit()


    async def log_conversation(self, user_id: str, source: str,
                                user_message: str, assistant_reply: str,
                                emotion_label: str = "", model_used: str = "",
                                session_id: str = "", cost_usd: float = 0,
                                cache_hit: int = 0, cache_miss: int = 0) -> None:
        async with self.write_transaction():
            await self.insert_conversation_log(
                user_id=user_id, source=source,
                user_message=user_message, assistant_reply=assistant_reply,
                emotion_label=emotion_label, model_used=model_used,
                session_id=session_id,
                auto_commit=False,
            )
            if session_id:
                await self.update_session(
                    session_id, cost_usd=cost_usd,
                    cache_hit=cache_hit, cache_miss=cache_miss,
                    auto_commit=False,
                )

    async def cleanup_expired_data(self, auto_commit: bool = True) -> dict[str, int]:
        """按 cleanup_config 表中的策略清理过期数据。返回各表删除行数。

        P0 根治（2026-08-05 rework）：
        1) 用独立 aiosqlite 连接执行（与主连接隔离），避免 cleanup 的长 DELETE 拖慢
           主路径的读/写（主连接命中"独占线程"的 45s 静默期历史阻塞根因）。
        2) 每删一张表立即 commit（逐表提交），释放 SQLite 单写锁。
           原实现所有表的 DELETE 合并在一个长事务里、最后才 commit，长时间独占总写锁，
           与 instinct/记忆编码等并发写时 → 其他写者等锁超时 "database is locked"
           （14:37:45 实证）。逐表提交让写锁在每个 DELETE 之间释放，锁竞争降为短暂可容忍。
        WAL 支持多连接并发，synchronous=NORMAL 让 commit 不 fsync（WAL 仅 checkpoint 时 fsync）。
        """
        import aiosqlite as _aiosqlite
        result: dict[str, int] = {}
        if not self.db_path:
            return result

        _cleanup_t0 = time.time()
        _w_conn = None
        try:
            _w_conn = await _aiosqlite.connect(str(self.db_path))
            _w_conn.row_factory = _aiosqlite.Row
            await _w_conn.execute("PRAGMA journal_mode=WAL")
            # busy_timeout 5000→20000：与主连接一致，避免后台 cleanup 与主连接/
            # instinct/curator 并发写时 5s 等锁不够 → "database is locked"（14:37:45 实证）。
            await _w_conn.execute("PRAGMA busy_timeout=20000")
            await _w_conn.execute("PRAGMA synchronous=NORMAL")
            await _w_conn.execute("PRAGMA cache_size=-20000")
            await _w_conn.execute("PRAGMA temp_store=MEMORY")
            try:
                cursor = await _w_conn.execute(
                    "SELECT table_name, retention_days, date_column FROM cleanup_config WHERE enabled=1"
                )
                configs = await cursor.fetchall()
            except (OSError, ValueError):
                logger.debug("database.cleanup_config_read_error", exc_info=True)
                return result

            now = time.time()
            for row in configs:
                table_name = row["table_name"]
                retention_days = row["retention_days"]
                date_column = row["date_column"]
                cutoff = now - retention_days * 86400
                try:
                    # 白名单校验表名和列名，防止 SQL 注入
                    import re
                    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', table_name):
                        logger.warning("database.cleanup_invalid_table", table=table_name)
                        continue
                    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', date_column):
                        logger.warning("database.cleanup_invalid_column", column=date_column)
                        continue
                    del_cursor = await _w_conn.execute(
                        f'DELETE FROM "{table_name}" WHERE "{date_column}" < ? AND "{date_column}" > 0',
                        (cutoff,),
                    )
                    deleted = del_cursor.rowcount
                    result[table_name] = deleted
                    if deleted > 0:
                        logger.info("database.cleanup", table=table_name,
                                    deleted=deleted, retention_days=retention_days)
                    # 根治（2026-08-05）：每删一张表立即 commit，释放写锁。
                    # 原实现所有表的 DELETE 合并在一个长事务里，最后才 commit，
                    # 长时间独占总写锁 → 并发写（instinct/记忆编码/主路径）等锁超时
                    # "database is locked"（14:37:45 实证）。逐表提交让写锁在每个
                    # DELETE 之间释放，其他写者得以插入，锁竞争降为短暂、可容忍。
                    if auto_commit:
                        try:
                            await _w_conn.commit()
                        except (OSError, RuntimeError) as e:
                            logger.warning("database.cleanup_commit_failed table={} error={}", table_name, e)
                except (OSError, RuntimeError) as e:
                    logger.warning("database.cleanup_failed", table=table_name, error=str(e))
                    result[table_name] = 0

            if auto_commit:
                try:
                    await _w_conn.commit()
                except (OSError, RuntimeError) as e:
                    logger.warning("清理过期数据提交事务失败: {}", e)

            _cleanup_ms = int((time.time() - _cleanup_t0) * 1000)
            if _cleanup_ms > 2000:
                logger.warning("database.cleanup_slow elapsed_ms={}", _cleanup_ms)
            return result
        except Exception as e:
            logger.warning("database.cleanup_conn_failed error={}", str(e))
            return result
        finally:
            if _w_conn is not None:
                with contextlib.suppress(Exception):
                    await _w_conn.close()
