"""MemoryDB 的共享纯函数 helper —— 下沉自 db/db_memory.py。

仅下沉无状态纯函数（实体解析/SQL 占位/scope 过滤/FTS 行转换等），
便于 MemoryDB 后续 Mixin 拆分复用。模块级计数器 _fts_sync_failures
及其 get/record 保留在 db_memory.py（global 语义依赖单一模块）。
"""
from __future__ import annotations

import contextlib
from typing import Any

from loguru import logger

from db.db_local_ai import transaction_lock_for


class WriteTxGuard:
    """子仓库事务守卫（数据库小任务B-1）。

    由 DatabaseManager 装配时注入（``MemoryDB.attach_tx_guard``），使
    MemoryDB 的 auto_commit 写点能感知外层 ``write_transaction()``：
    - 外层事务持锁期间：语句直接执行，提交由外层统一负责；
    - 独立 auto_commit 写：在 transaction_lock_for(conn) 同一把连接级锁内
      执行并提交——不再裸 commit，杜绝把他人半事务提前提交。
    """

    def __init__(self, conn: Any, tx_active: Any) -> None:
        self._conn = conn
        self._tx_active = tx_active

    @property
    def conn(self) -> Any:
        return self._conn

    def active(self) -> bool:
        return bool(self._tx_active.get())


@contextlib.asynccontextmanager
async def owned_write_section(owner: Any, auto_commit: bool = True):
    """子仓库 auto_commit 写段的统一所有权边界（数据库小任务B-1）。

    yield 值 do_commit 表示本段是否负责 commit：
    - auto_commit=False：让渡（调用方/外层事务负责提交）；
    - 已注入守卫且外层 write_transaction 持锁中：让渡，避免锁重入死锁；
    - 已注入守卫的独立写：连接级锁内执行，退出时提交（不裸提交）；
    - 未注入守卫（独立构造实例）：退化为历史行为——退出时提交，无锁。

    用法：
        async with owned_write_section(self, auto_commit) as do_commit:
            cursor = await self._conn.execute(sql, params)
            ...
            if do_commit:
                await self._conn.commit()
    段内异常时提交被跳过（与历史裸路径一致：隐式事务留存由上层处置）。
    """
    guard = getattr(owner, "_tx_guard", None)
    if not auto_commit:
        yield False
    elif guard is not None and guard.active():
        yield False
    elif guard is not None:
        async with transaction_lock_for(guard.conn):
            yield True
            await guard.conn.commit()
    else:
        yield True
        await owner._conn.commit()


def active_memory_visibility_sql(table: str = "episodic_memories") -> str:
    """Return the canonical SQL predicate for user-visible episodic memory."""
    p = f"{table}." if table else ""
    try:
        from memory.reconciliation_policy import configured_policy
        effective_mode, _ = configured_policy()
    except ImportError:
        effective_mode = "shadow"
    if effective_mode != "enforce":
        return f"{p}status = 'active'"
    return (
        "("
        f"({p}is_raw = 0 AND {p}status = 'active') OR "
        f"({p}is_raw = 1 AND {p}status = 'active' AND NOT EXISTS ("
        "SELECT 1 FROM memory_knowledge_sources AS mks "
        "JOIN episodic_memories AS active_knowledge "
        "ON active_knowledge.id = mks.knowledge_id "
        f"WHERE mks.raw_id = {p}id "
        "AND active_knowledge.is_raw = 0 "
        "AND active_knowledge.status = 'active'"
        "))"
        ")"
    )


def compute_missing_vec_ids(memory_ids: list[int], vec_rowids: set[int]) -> list[int]:
    """对账：返回在主表存在但向量表缺失的记忆 id 列表。

    Args:
        memory_ids: 主表 episodic_memories 中应被向量索引的记忆 id（保持传入顺序）。
        vec_rowids: 向量表 memories_vec 中已存在的 rowid 集合。
    """
    return [mid for mid in memory_ids if mid not in vec_rowids]


def _parse_entity_list(raw: Any) -> list:
    """解析 entities 字段：JSON 字符串 → list；list 原样；其它 → []。"""
    if isinstance(raw, str) and raw:
        import json
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    return raw if isinstance(raw, list) else []


def _sql_placeholders(ids: list) -> str:
    """生成 SQL IN 子句的占位符串（如 '?,?,?'）。"""
    return ",".join("?" * len(ids))


def _entity_like_conditions(entity_names: list[str]) -> tuple[str, list[str]]:
    """构建 entities LIKE 模糊匹配条件与参数（用于实体反查）。"""
    conditions = " OR ".join(["entities LIKE ?" for _ in entity_names])
    params = [f'%"{e}"%' for e in entity_names]
    return conditions, params


def _rows_to_entity_results(rows: list) -> list[dict]:
    """把查询行转为 dict 列表，并解析 entities JSON 为 entity_list。"""
    results = []
    for r in rows:
        d = dict(r)
        d["entity_list"] = _parse_entity_list(d.get("entities", ""))
        results.append(d)
    return results


def _scope_where(scope: Any, *, is_raw: int | None = None,
                 table: str = "", include_archived_filter: bool = True) -> tuple[str, list]:
    """构建 scope 过滤 WHERE 片段（user_id/agent_id + 可选 is_raw + 可选 archived 过滤）。

    记录 debug 日志便于排查 scope 过滤是否正确。

    Args:
        scope: Scope 对象
        is_raw: None=不限, 0=只查提炼知识, 1=只查原始记录
        table: 表别名前缀（如 "em"），空串不加前缀
        include_archived_filter: 是否追加 session_id != 'archived'（默认 True）

    Returns:
        (where_sql, params)：where_sql 含前导空格，可直接拼接在 WHERE 后。
    """
    p = f"{table}." if table else ""
    where = f" AND {p}user_id = ? AND {p}agent_id = ?"
    params: list = [scope.user_id, scope.agent_id]
    boundary = getattr(scope, "boundary", None)
    boundary_value = getattr(boundary, "value", boundary)
    if boundary_value == "conversation":
        where += f" AND {p}session_id = ?"
        params.append(scope.session_id)
    else:
        where += f" AND COALESCE({p}session_id, '') NOT LIKE ?"
        params.append("qq_group:%")
    if include_archived_filter:
        where += f" AND COALESCE({p}session_id, '') != 'archived'"
    if is_raw is not None:
        where += f" AND {p}is_raw = ?"
        params.append(is_raw)
    logger.debug("db_memory.scope_where_built",
                 user_id=scope.user_id, agent_id=scope.agent_id,
                 is_raw=is_raw, table=table, where=where)
    return where, params


def _rows_to_fts_results(rows: list) -> list[dict]:
    """把 FTS 查询行转为 dict 列表，并将 bm25 负分转为正分。"""
    results = []
    for r in rows:
        d = dict(r)
        d["score"] = -d.get("score", 0)
        results.append(d)
    return results
