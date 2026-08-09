import pytest

from db.database import DatabaseManager


@pytest.mark.asyncio
async def test_profile_migration_failure_stops_database_initialization(tmp_path, monkeypatch):
    manager = DatabaseManager(tmp_path / "migration-failure.db")

    async def fail():
        raise RuntimeError("forced v23 failure")

    monkeypatch.setattr(manager, "_migrate_v23", fail)
    with pytest.raises(RuntimeError, match="forced v23 failure"):
        await manager.init()
    assert manager.profiles is None
    await manager.close()


@pytest.mark.asyncio
async def test_cron_write_cannot_commit_an_outer_profile_transaction(tmp_path):
    manager = DatabaseManager(tmp_path / "transaction-lock.db")
    await manager.init()
    with pytest.raises(RuntimeError):
        async with manager.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO cron_last_run (task_name, last_run) VALUES ('outer', 1)"
            )
            await manager.set_cron_last_run("side", 2)
            raise RuntimeError("rollback")
    assert await manager.get_cron_last_run("outer") is None
    assert await manager.get_cron_last_run("side") is None
    await manager.close()


@pytest.mark.asyncio
async def test_reinitialization_closes_previous_profile_connection(tmp_path):
    manager = DatabaseManager(tmp_path / "reinitialize.db")
    await manager.init()
    old_connection = manager._profile_conn
    await manager.init()
    with pytest.raises(ValueError, match="no active connection"):
        await old_connection.execute("SELECT 1")
    await manager.close()
