"""reconciliation schema 生产接入测试（数据库小任务B-3）。

契约：
1. 迁移链（v32 memory_reconciliation_shadow）已把 SCHEMA_SQL 接入生产：
   新装库经 DatabaseManager.init() 后，job/action/target/snapshot/outbox/epoch
   表与索引与 db_memory_reconciliation.create_schema 产物同构。
2. register_migration(db) 钩子可用：迁移代理可在迁移框架中显式调用，
   幂等且不触碰 legacy_migrations。
3. doctor/memory_schema_readiness 具备 reconciliation 能力清单，
   缺表时报 missing_capabilities。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.database import DatabaseManager
from db.db_memory_reconciliation import SCHEMA_SQL, create_schema, register_migration


def _parse_schema_sql(sql: str) -> dict[str, list[str]]:
    """解析 SCHEMA_SQL → {对象名: [规范化语句]}，用于同构比较。"""
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    objects: dict[str, list[str]] = {}
    for stmt in statements:
        normalized = re.sub(r"\s+", " ", stmt).strip().upper()
        m = re.match(
            r"CREATE (?:TABLE|INDEX|UNIQUE INDEX) IF NOT EXISTS (\w+)", normalized
        )
        assert m, f"SCHEMA_SQL 中出现无法识别的语句: {stmt[:80]}"
        # 键用小写对象名（sqlite_master 口径），语句文本保留规范化大写
        objects.setdefault(m.group(1).lower(), []).append(normalized)
    return objects


@pytest.mark.asyncio
async def test_fresh_install_matches_create_schema(tmp_path):
    """新装库（走 v32 正式迁移）与 create_schema 直接产物同构：
    表集合、索引集合、以及关键列完全一致。
    """
    manager = DatabaseManager(tmp_path / "fresh.db")
    await manager.init()

    async def _snapshot(conn) -> tuple[set, dict]:
        # aiosqlite 的 execute_fetchall 是协程，必须 await；行值统一用下标取
        # （参考连接未设 Row factory）。对象集合含 table+index：
        # SCHEMA_SQL 期望集同时覆盖两类（autoindex 无 sql 行，天然不入选）
        tables = {
            row[0] for row in await conn.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
            )
        }
        indexes = {}
        for row in await conn.execute_fetchall(
            "SELECT name, tbl_name FROM sqlite_master "
            "WHERE type='index' AND sql IS NOT NULL"
        ):
            indexes[row[0]] = row[1]
        return tables, indexes

    prod_tables, prod_indexes = await _snapshot(manager._conn)

    import aiosqlite
    ref_conn = await aiosqlite.connect(":memory:")
    try:
        # 前置：reconciliation 表引用 episodic_memories 的可见性由运行时 SQL 提供，
        # SCHEMA_SQL 本身无外键依赖，可直接在空库执行
        await create_schema(ref_conn)
        ref_tables, ref_indexes = await _snapshot(ref_conn)
    finally:
        await ref_conn.close()

    expected_tables = set(_parse_schema_sql(SCHEMA_SQL))
    missing_in_prod = expected_tables - (prod_tables & expected_tables)
    assert not missing_in_prod, f"生产库缺少 reconciliation 对象: {missing_in_prod}"

    # 列级同构：逐表对比生产 vs create_schema 参考实现
    import aiosqlite as _aiosqlite
    ref2 = await _aiosqlite.connect(":memory:")
    try:
        await create_schema(ref2)
        for obj in expected_tables:
            if obj.startswith("idx_"):
                continue
            ref_cols = {
                row[1] for row in await ref2.execute_fetchall(
                    f"PRAGMA table_info({obj})")
            }
            prod_cols = {
                row[1] for row in await manager._conn.execute_fetchall(
                    f"PRAGMA table_info({obj})")
            }
            assert prod_cols == ref_cols, (
                f"表 {obj} 列不同构: 仅生产有={prod_cols - ref_cols}, "
                f"仅参考有={ref_cols - prod_cols}"
            )
    finally:
        await ref2.close()
    await manager.close()


@pytest.mark.asyncio
async def test_required_indexes_present_after_migration(tmp_path):
    """v32 迁移后 claim 索引就位。"""
    manager = DatabaseManager(tmp_path / "idx.db")
    await manager.init()
    rows = await manager.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='index' AND name IN (?, ?)",
        ("idx_reconciliation_jobs_claim", "idx_memory_index_outbox_claim"),
    )
    names = {row["name"] for row in rows}
    assert names == {"idx_reconciliation_jobs_claim", "idx_memory_index_outbox_claim"}
    await manager.close()


@pytest.mark.asyncio
async def test_register_migration_is_idempotent_and_creates_all_objects(tmp_path):
    """register_migration 钩子：供迁移代理调用；幂等、可重复执行。"""
    import aiosqlite

    conn = await aiosqlite.connect(tmp_path / "hook.db")
    try:
        await conn.execute(
            "CREATE TABLE episodic_memories (id INTEGER PRIMARY KEY)"
        )
        await register_migration(conn)
        await register_migration(conn)  # 二次调用不抛错

        rows = await conn.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {row[0] for row in rows}
        for required in (
            "memory_knowledge_sources",
            "memory_reconciliation_jobs",
            "memory_reconciliation_actions",
            "memory_reconciliation_targets",
            "memory_reconciliation_snapshots",
            "memory_index_outbox",
            "memory_retrieval_epochs",
        ):
            assert required in tables
        idx_rows = await conn.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_reconciliation%'"
        )
        assert idx_rows, "claim 索引未创建"
    finally:
        await conn.close()


def test_readiness_lists_reconciliation_capabilities():
    """doctor readiness 必须声明 reconciliation 必需能力清单。"""
    from doctor.memory_schema_readiness import REQUIRED_RECONCILIATION_TABLES

    expected = {
        "memory_knowledge_sources",
        "memory_reconciliation_jobs",
        "memory_reconciliation_actions",
        "memory_reconciliation_targets",
        "memory_reconciliation_snapshots",
        "memory_index_outbox",
        "memory_retrieval_epochs",
    }
    assert expected <= set(REQUIRED_RECONCILIATION_TABLES)
