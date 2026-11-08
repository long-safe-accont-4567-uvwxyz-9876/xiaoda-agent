"""审计 H3：concept_edges 快照改为过滤 + 分块流式读取。

背景（2026-11-08 只读实测）：concept_edges 1,001,111 行 / 161.8MB 占整库
78%，`get_edge_snapshot()` 原实现无 WHERE 全表 fetchall()，把 100 万行一次性
搬进 Python 列表再组装（首载 3-4s，内存峰值=全部行对象）。本测试覆盖：
- 全量快照语义不变（{source: {target: weight}}）；
- relation / min_weight 过滤生效；
- 分块大小不影响结果（chunk_size=1/2/0 与默认一致）；
- 分块路径不会调用 fetchall（只有 chunk_size<=0 才一次性取）。
"""
from __future__ import annotations

import aiosqlite
import pytest

from db.db_concept import ConceptDB

_DDL = (
    "CREATE TABLE concept_edges ("
    " source_id TEXT NOT NULL, target_id TEXT NOT NULL,"
    " relation TEXT NOT NULL DEFAULT 'related',"
    " weight REAL NOT NULL DEFAULT 1.0, created TEXT NOT NULL,"
    " PRIMARY KEY (source_id, target_id))"
)

_ROWS = [
    ("n1", "n2", "co-occurrence", 1.0),
    ("n1", "n3", "co-occurrence", 3.0),
    ("n2", "n3", "related", 2.0),
    ("n3", "n4", "co-occurrence", 0.5),
    ("n4", "n5", "related", 5.0),
]


async def _make_db() -> tuple[aiosqlite.Connection, ConceptDB]:
    conn = await aiosqlite.connect(":memory:")
    await conn.execute(_DDL)
    await conn.executemany(
        "INSERT INTO concept_edges (source_id, target_id, relation, weight, created)"
        " VALUES (?, ?, ?, ?, '2026-11-08T00:00:00+08:00')",
        _ROWS,
    )
    await conn.commit()
    return conn, ConceptDB(conn)


@pytest.mark.asyncio
async def test_full_snapshot_semantics_unchanged():
    conn, db = await _make_db()
    try:
        snap = await db.get_edge_snapshot()
    finally:
        await conn.close()
    assert snap == {
        "n1": {"n2": 1.0, "n3": 3.0},
        "n2": {"n3": 2.0},
        "n3": {"n4": 0.5},
        "n4": {"n5": 5.0},
    }


@pytest.mark.asyncio
async def test_relation_filter():
    conn, db = await _make_db()
    try:
        snap = await db.get_edge_snapshot(relation="related")
    finally:
        await conn.close()
    assert snap == {"n2": {"n3": 2.0}, "n4": {"n5": 5.0}}


@pytest.mark.asyncio
async def test_min_weight_filter():
    conn, db = await _make_db()
    try:
        snap = await db.get_edge_snapshot(min_weight=2.0)
    finally:
        await conn.close()
    assert snap == {"n1": {"n3": 3.0}, "n2": {"n3": 2.0}, "n4": {"n5": 5.0}}


@pytest.mark.asyncio
async def test_combined_filters():
    conn, db = await _make_db()
    try:
        snap = await db.get_edge_snapshot(relation="co-occurrence", min_weight=1.0)
    finally:
        await conn.close()
    assert snap == {"n1": {"n2": 1.0, "n3": 3.0}}


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_size", [1, 2, 3, 0])
async def test_chunk_sizes_equivalent(chunk_size):
    conn, db = await _make_db()
    try:
        paged = await db.get_edge_snapshot(chunk_size=chunk_size)
        full = await db.get_edge_snapshot()
    finally:
        await conn.close()
    assert paged == full


@pytest.mark.asyncio
async def test_chunked_path_does_not_fetchall(monkeypatch):
    """分块路径必须走 fetchmany，不能退化回一次性 fetchall。"""
    conn, db = await _make_db()
    calls = {"fetchall": 0, "fetchmany": 0}
    real_execute = conn.execute

    class _CursorProxy:
        def __init__(self, result):
            self._result = result
            self._cursor = None

        async def __aenter__(self):
            # aiosqlite 的 Result 需 await __aenter__ 才拿到真正的 cursor
            self._cursor = await self._result.__aenter__()
            return self

        async def __aexit__(self, *exc):
            return await self._result.__aexit__(*exc)

        async def fetchall(self):
            calls["fetchall"] += 1
            return await self._cursor.fetchall()

        async def fetchmany(self, size=-1):
            calls["fetchmany"] += 1
            return await self._cursor.fetchmany(size)

    def _execute(sql, params=()):
        if "concept_edges" in sql and "SELECT" in sql:
            return _CursorProxy(real_execute(sql, params))
        return real_execute(sql, params)

    monkeypatch.setattr(conn, "execute", _execute)
    try:
        snap = await db.get_edge_snapshot(chunk_size=2)
    finally:
        await conn.close()
    assert len(snap) == 4
    assert calls["fetchall"] == 0
    assert calls["fetchmany"] >= 3
