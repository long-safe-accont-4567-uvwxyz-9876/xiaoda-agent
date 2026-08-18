"""数据库迁移修复工具。

用法:
    # 标记 dirty 状态为 clean（手动修复数据库后使用）
    python -m db.repair_migration --mark-clean

    # 回滚指定版本的迁移记录（删除该版本的 schema_version 记录）
    python -m db.repair_migration --rollback <version>

    # 查看当前迁移状态
    python -m db.repair_migration --status
"""
import argparse
import asyncio
import sys

import aiosqlite

from config import DATA_DIR

DB_PATH = DATA_DIR / "agent.db"


def _connect() -> aiosqlite.Connection:
    """返回 aiosqlite 连接对象（未启动线程），供 async with 使用。

    调用方进入上下文后应先执行 PRAGMA busy_timeout 以避免锁冲突。
    """
    return aiosqlite.connect(str(DB_PATH))


async def show_status() -> int:
    """显示当前迁移状态。"""
    if not DB_PATH.exists():
        print(f"数据库不存在: {DB_PATH}")
        return 1
    async with _connect() as conn:
        await conn.execute("PRAGMA busy_timeout=10000")
        # 检查 migration_state 表是否存在
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='migration_state'"
        )
        if not await cursor.fetchone():
            print("migration_state 表不存在（数据库可能未初始化或为旧版本）")
            # 显示 schema_version
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
            )
            if await cursor.fetchone():
                cursor = await conn.execute("SELECT MAX(version) FROM schema_version")
                row = await cursor.fetchone()
                print(f"当前 schema_version: {row[0] if row and row[0] else 0}")
            return 0

        cursor = await conn.execute(
            "SELECT dirty, last_version, last_error FROM migration_state WHERE id = 1"
        )
        row = await cursor.fetchone()
        if not row:
            print("migration_state 表为空")
            return 1

        dirty, last_ver, last_err = row
        print(f"Dirty: {'是 ⚠️' if dirty else '否 ✅'}")
        print(f"Last version: {last_ver}")
        if last_err:
            print(f"Last error: {last_err}")

        cursor = await conn.execute("SELECT MAX(version) FROM schema_version")
        row = await cursor.fetchone()
        current = row[0] if row and row[0] else 0
        print(f"已应用 schema_version: {current}")
    return 0


async def mark_clean() -> int:
    """清除 dirty 状态。"""
    if not DB_PATH.exists():
        print(f"数据库不存在: {DB_PATH}")
        return 1
    async with _connect() as conn:
        await conn.execute("PRAGMA busy_timeout=10000")
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='migration_state'"
        )
        if not await cursor.fetchone():
            print("migration_state 表不存在，无需清理")
            return 0
        await conn.execute(
            "UPDATE migration_state SET dirty = 0, last_error = '' WHERE id = 1"
        )
        await conn.commit()
        print("✅ 已清除 dirty 状态，应用可正常启动")
    return 0


async def rollback(version: int) -> int:
    """回滚指定版本的迁移记录。"""
    if not DB_PATH.exists():
        print(f"数据库不存在: {DB_PATH}")
        return 1
    async with _connect() as conn:
        await conn.execute("PRAGMA busy_timeout=10000")
        # 删除该版本及更高版本的 schema_version 记录
        await conn.execute(
            "DELETE FROM schema_version WHERE version >= ?", (version,)
        )
        # 清除 dirty 状态
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='migration_state'"
        )
        if await cursor.fetchone():
            await conn.execute(
                "UPDATE migration_state SET dirty = 0, last_version = ?, last_error = '' WHERE id = 1",
                (version - 1,),
            )
        await conn.commit()
        print(f"✅ 已回滚到 v{version - 1}（删除了 v{version} 及以上的迁移记录）")
        print("⚠️ 注意：此操作仅删除迁移记录，不会撤销 schema 变更。")
        print("   如果迁移已部分执行，可能需要手动修复数据库结构。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="数据库迁移修复工具")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mark-clean", action="store_true", help="清除 dirty 状态")
    group.add_argument("--rollback", type=int, metavar="VERSION", help="回滚指定版本")
    group.add_argument("--status", action="store_true", help="查看当前迁移状态")
    args = parser.parse_args()

    if args.status:
        return asyncio.run(show_status())
    if args.mark_clean:
        return asyncio.run(mark_clean())
    if args.rollback is not None:
        return asyncio.run(rollback(args.rollback))
    return 0


if __name__ == "__main__":
    sys.exit(main())