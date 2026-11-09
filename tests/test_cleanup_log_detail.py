"""database.cleanup_failed 必须打出表名/异常类型/原因。

回归背景（2026-11-09）：原来写 logger.warning("database.cleanup_failed", table=..., error=...)，
loguru 只按 {} / {name} 占位符格式化，多余的 keyword 参数不会进日志正文，结果是启动时
3 次失败、耗时 60.2 秒，却只留下裸事件名，表名与原因全丢。
"""
from __future__ import annotations

import asyncio
import sqlite3

from loguru import logger

from db.conversation_logs import ConversationLogMixin


class _Dummy(ConversationLogMixin):
    """只带 db_path 的最小组合体，复现 cleanup_expired_data 的独立连接路径。"""

    def __init__(self, db_path) -> None:
        self.db_path = db_path


def _prepare(db_path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE cleanup_config ("
            "table_name TEXT, retention_days INTEGER, date_column TEXT, enabled INTEGER)"
        )
        # 指向不存在的表：DELETE 会抛 sqlite3.OperationalError("no such table: ...")
        conn.execute(
            "INSERT INTO cleanup_config VALUES ('ghost_table_missing', 30, 'timestamp', 1)"
        )
        conn.commit()
    finally:
        conn.close()


def _run(tmp_path):
    db = tmp_path / "agent.db"
    _prepare(db)
    records: list[dict] = []
    sink = logger.add(lambda m: records.append(m.record), level="WARNING")
    try:
        result = asyncio.run(_Dummy(db).cleanup_expired_data())
    finally:
        logger.remove(sink)
    return result, [r["message"] for r in records]


def test_cleanup_failed_logs_table_type_and_reason(tmp_path):
    result, messages = _run(tmp_path)
    assert result == {"ghost_table_missing": 0}
    failed = [m for m in messages if m.startswith("database.cleanup_failed")]
    assert failed, messages
    msg = failed[0]
    assert "table=ghost_table_missing" in msg
    assert "exc_type=OperationalError" in msg
    assert "no such table" in msg


def test_cleanup_invalid_table_logs_table_name(tmp_path):
    db = tmp_path / "agent.db"
    _prepare(db)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM cleanup_config")
    conn.execute("INSERT INTO cleanup_config VALUES ('evil; DROP TABLE x', 30, 'timestamp', 1)")
    conn.commit()
    conn.close()
    records: list[dict] = []
    sink = logger.add(lambda m: records.append(m.record), level="WARNING")
    try:
        asyncio.run(_Dummy(db).cleanup_expired_data())
    finally:
        logger.remove(sink)
    messages = [r["message"] for r in records]
    assert any(m.startswith("database.cleanup_invalid_table") for m in messages), messages
    assert any("table=evil; DROP TABLE x" in m for m in messages), messages
