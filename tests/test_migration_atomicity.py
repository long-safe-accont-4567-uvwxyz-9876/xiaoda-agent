"""迁移框架原子性与 v17 升级兼容回归（2026-08-24 审查修复）。

覆盖两个审查确认缺陷：
1. 迁移非原子：迁移体失败时 dirty 记录的 commit 把部分 DDL/DML 一并提交。
   修复后迁移包在 SAVEPOINT 内，失败回滚到 savepoint——部分写入不落盘，
   重试从干净状态开始。
2. v17 升级断链：目标表含 v20 才有的 user_id，`INSERT SELECT *` 对
   v16 及更早的 13 列旧表报列数不匹配并永久 dirty。修复后显式列清单复制。
"""
from __future__ import annotations

import aiosqlite
import pytest

from db.database import DatabaseManager


async def _make_legacy_v16_greeting_db(tmp_path):
    """构造带真实 v16 形状 greeting_schedules（13 列、无 user_id）的旧库。"""
    db_path = tmp_path / "legacy_v16.db"
    conn = await aiosqlite.connect(db_path)
    await conn.execute("""
        CREATE TABLE episodic_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            summary TEXT NOT NULL,
            importance REAL DEFAULT 0.5,
            emotion_label TEXT DEFAULT '',
            session_id TEXT DEFAULT 'user',
            embedding_id INTEGER DEFAULT -1,
            rag_status TEXT DEFAULT 'pending',
            rag_synced_at REAL DEFAULT 0,
            doc_id TEXT DEFAULT '',
            source TEXT DEFAULT 'user',
            access_count INTEGER DEFAULT 0,
            distilled INTEGER DEFAULT 0,
            entities TEXT DEFAULT '',
            event_type TEXT DEFAULT '',
            metadata_json TEXT DEFAULT '{}',
            content_hash TEXT DEFAULT '',
            version INTEGER DEFAULT 1,
            user_id TEXT DEFAULT 'default',
            agent_id TEXT DEFAULT 'xiaoda',
            is_raw INTEGER DEFAULT 0,
            salience REAL DEFAULT 0.5,
            last_accessed REAL DEFAULT 0,
            phase TEXT DEFAULT 'buffer',
            difficulty REAL NOT NULL DEFAULT 5.0,
            stability REAL NOT NULL DEFAULT 3.0,
            last_review REAL NOT NULL DEFAULT 0,
            reinforcement_count INTEGER NOT NULL DEFAULT 0,
            created_at REAL DEFAULT 0
        )
    """)
    # v16 时点 schema_version 已记录到 16
    await conn.execute("""
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)
    """)
    for v in range(1, 17):
        await conn.execute(
            "INSERT INTO schema_version VALUES (?, ?)", (v, float(v)))
    # v17 之前形状：13 列，无 user_id，type 无 reminder 分支
    await conn.execute("""
        CREATE TABLE greeting_schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL CHECK(type IN ('fixed','random')),
            time TEXT DEFAULT '',
            window_start TEXT DEFAULT '',
            window_end TEXT DEFAULT '',
            count_per_day INTEGER DEFAULT 1,
            days TEXT NOT NULL DEFAULT '[1,2,3,4,5,6,7]',
            prompt_hint TEXT DEFAULT '',
            channels TEXT NOT NULL DEFAULT '["web"]',
            enabled INTEGER NOT NULL DEFAULT 1,
            next_fire_times TEXT DEFAULT '[]',
            drawn_date TEXT DEFAULT '',
            created_at REAL NOT NULL
        )
    """)
    await conn.execute(
        "INSERT INTO greeting_schedules (type, time, created_at) "
        "VALUES ('fixed', '09:00', 1700000000)")
    await conn.commit()
    await conn.close()
    return db_path


@pytest.mark.asyncio
async def test_v17_upgrades_from_real_v16_shape(tmp_path):
    """v16 库升级必须通过 v17：数据保留、user_id 回填 default、支持 reminder。"""
    db_path = await _make_legacy_v16_greeting_db(tmp_path)
    manager = DatabaseManager(db_path)
    # 最小化直连（不走 init()：那会先建当前形状基表，破坏"真实 v16 形状"
    # 前提）。本测试只验证 v17+v20 对旧形状的升级兼容。
    manager._conn = await aiosqlite.connect(str(db_path))
    manager._conn.row_factory = aiosqlite.Row
    try:
        # 直应用 v17+v20（断言中的 user_id 由 v20 回填）：最小化 v16 夹具
        # 不含 FTS 等后续迁移（v22+）假设存在的对象，跑全链会把无关缺口
        # 混入本回归；全链升级路径由新鲜库上的迁移套件覆盖。
        await manager._apply_migration(
            17, "greeting_schedules_reminder_type", manager._migrate_v17)
        await manager._apply_migration(
            20, "greeting_schedules.user_id_column", manager._migrate_v20)
        rows = await manager.fetch_all("SELECT * FROM greeting_schedules")
        assert len(rows) == 1
        row = rows[0]
        assert row["type"] == "fixed"
        assert row["time"] == "09:00"
        assert row["user_id"] == "default"  # v20 前默认归属
        cols = {r["name"] for r in await manager.fetch_all(
            "PRAGMA table_info(greeting_schedules)")}
        assert "user_id" in cols
        # reminder 类型可写入（CHECK 约束已更新）
        await manager._conn.execute(
            "INSERT INTO greeting_schedules (type, time, created_at) "
            "VALUES ('reminder', '10:00', 1700000100)")
        await manager._conn.commit()
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_failed_migration_rolls_back_partial_writes(tmp_path):
    """迁移体失败：部分 DDL/DML 必须被 SAVEPOINT 回滚，dirty=1 且版本未推进。"""
    db_path = tmp_path / "atomic.db"
    manager = DatabaseManager(db_path)
    await manager.init()
    conn = manager._conn
    before_version = (await manager.fetch_one(
        "SELECT MAX(version) AS v FROM schema_version"))["v"]
    try:
        async def broken_migration():
            await conn.execute(
                "CREATE TABLE _atomic_probe (id INTEGER PRIMARY KEY)")
            await conn.execute(
                "INSERT INTO _atomic_probe VALUES (1)")
            raise RuntimeError("fault-injected")

        with pytest.raises(RuntimeError, match="fault-injected"):
            await manager._apply_migration(
                9999, "atomicity probe", broken_migration)

        # 部分写入已回滚：表不存在、无脏行
        tables = {r[0] for r in await conn.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "_atomic_probe" not in tables
        after_version = (await manager.fetch_one(
            "SELECT MAX(version) AS v FROM schema_version"))["v"]
        assert after_version == before_version
        state = await manager.fetch_one(
            "SELECT dirty, last_version FROM migration_state WHERE id=1")
        assert state["dirty"] == 1
        assert state["last_version"] == 9999
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_retry_after_failed_migration_succeeds_clean(tmp_path):
    """失败迁移重跑：第一次的部分写入不残留，第二次干净成功。"""
    db_path = tmp_path / "retry.db"
    manager = DatabaseManager(db_path)
    await manager.init()
    conn = manager._conn
    try:
        calls = {"n": 0}

        async def flaky_migration():
            calls["n"] += 1
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS _flaky_probe "
                "(id INTEGER PRIMARY KEY, tag TEXT)")
            await conn.execute(
                "INSERT INTO _flaky_probe (tag) VALUES (?)",
                (f"attempt{calls['n']}",))
            if calls["n"] < 2:
                raise RuntimeError("first attempt fails")

        with pytest.raises(RuntimeError):
            await manager._apply_migration(9998, "flaky", flaky_migration)

        # 第二次成功：只有第二次的行，没有第一次残留
        await manager._apply_migration(9998, "flaky", flaky_migration)
        rows = await manager.fetch_all("SELECT * FROM _flaky_probe")
        assert [r["tag"] for r in rows] == ["attempt2"]
    finally:
        await manager.close()
