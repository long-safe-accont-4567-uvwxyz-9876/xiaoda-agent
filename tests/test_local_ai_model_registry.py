"""tests/test_local_ai_model_registry.py — Persistent Model Registry tests (Task 7).

Covers (per Task 7 brief):
- Bundled BGE entry is seeded on first migration with ownership="bundled".
- Bundled model cannot be removed (ModelRemovalBlockedError).
- list()/get()/register()/mark_validation()/remove() basic CRUD.
- register() with duplicate ID raises ModelAlreadyExistsError.
- register() with a colliding directory raises ModelPathCollisionError.
- mark_validation() updates validation_state and manifest_checksum.
- Migration is idempotent: re-running does not duplicate the bundled entry.
- Fresh database seeds the built-in BGE entry on init.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from db.database import CURRENT_SCHEMA_VERSION, DatabaseManager
from db.db_local_ai import LocalAIDB
from local_ai.contracts import InstalledModel, ModelPurpose
from local_ai.models.registry import (
    ModelAlreadyExistsError,
    ModelNotFoundError,
    ModelPathCollisionError,
    ModelRegistry,
    ModelRemovalBlockedError,
)

BUNDLED_ID = "builtin:bge-small-zh-v1.5"
NOW = datetime(2026, 8, 9, 8, 30, tzinfo=timezone.utc)


def _make_installed(
    *,
    id: str = "local:test-model",
    catalog_id: str = "modelscope:test-model",
    revision: str = "abcdef0",
    purpose: ModelPurpose = ModelPurpose.EMBEDDING,
    directory: str | None = None,
    manifest_checksum: str = "sha256:abc",
    validation_state: str = "validated",
    ownership: str = "user",
    installed_at: datetime = NOW,
    metadata: dict | None = None,
) -> InstalledModel:
    return InstalledModel(
        id=id,
        catalog_id=catalog_id,
        revision=revision,
        purpose=purpose,
        directory=directory or str(Path("/models/") / id),
        manifest_checksum=manifest_checksum,
        validation_state=validation_state,
        ownership=ownership,
        installed_at=installed_at,
        metadata=metadata or {},
    )


@pytest.fixture
async def manager(tmp_path):
    db_path = tmp_path / "registry.db"
    mgr = DatabaseManager(db_path)
    await mgr.init()
    try:
        yield mgr
    finally:
        await mgr.close()


@pytest.fixture
def registry(manager):
    return ModelRegistry(manager)


# ─────────────────────────────────────────────────────────────
# Schema / migration
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schema_version_bumped_to_26(manager):
    row = await manager.fetch_one("SELECT MAX(version) AS version FROM schema_version")
    assert row["version"] == CURRENT_SCHEMA_VERSION
    assert CURRENT_SCHEMA_VERSION == 27


@pytest.mark.asyncio
async def test_installed_models_table_exists(manager):
    rows = await manager.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='installed_models'"
    )
    assert len(rows) == 1
    assert rows[0]["name"] == "installed_models"


@pytest.mark.asyncio
async def test_installed_models_indexes_exist(manager):
    rows = await manager.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='installed_models'"
    )
    names = {row["name"] for row in rows}
    assert "idx_installed_models_purpose" in names
    assert "idx_installed_models_catalog_id" in names


@pytest.mark.asyncio
async def test_local_ai_db_module_registered_on_manager(manager):
    assert manager.local_ai is not None


# ─────────────────────────────────────────────────────────────
# Bundled BGE seeding
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bundled_bge_is_registered_but_not_removable(registry):
    model = await registry.get(BUNDLED_ID)
    assert model is not None
    assert model.ownership == "bundled"
    assert model.removable is False
    assert model.purpose is ModelPurpose.EMBEDDING
    assert model.validation_state == "validated"
    assert model.manifest_checksum  # non-empty
    assert model.directory  # non-empty absolute path
    with pytest.raises(ModelRemovalBlockedError):
        await registry.remove(model.id)


@pytest.mark.asyncio
async def test_list_includes_bundled_bge(registry):
    models = await registry.list()
    ids = {m.id for m in models}
    assert BUNDLED_ID in ids


@pytest.mark.asyncio
async def test_fresh_database_seeds_bundled_entry_only_once(manager):
    # Re-running init() (which re-runs migrations idempotently) must not duplicate.
    await manager.init()
    rows = await manager.fetch_all(
        "SELECT id FROM installed_models WHERE id = ?", (BUNDLED_ID,)
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_migration_is_idempotent_bundled_not_duplicated(manager):
    # Manually re-running v25 migration function must be a no-op for the bundled entry.
    await manager._migrate_v25()
    await manager.commit()
    rows = await manager.fetch_all(
        "SELECT id FROM installed_models WHERE id = ?", (BUNDLED_ID,)
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_migrate_v25_does_not_touch_existing_user_records(manager, registry):
    user_model = _make_installed(id="local:user-1")
    await registry.register(user_model)
    await manager._migrate_v25()
    await manager.commit()
    models = await registry.list()
    ids = {m.id for m in models}
    assert {"builtin:bge-small-zh-v1.5", "local:user-1"} <= ids


# ─────────────────────────────────────────────────────────────
# CRUD: list / get / register / mark_validation / remove
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_returns_none_for_missing_id(registry):
    assert await registry.get("local:does-not-exist") is None


@pytest.mark.asyncio
async def test_register_inserts_new_model(registry):
    installed = _make_installed(id="local:new-model")
    saved = await registry.register(installed)
    assert saved.id == "local:new-model"
    fetched = await registry.get("local:new-model")
    assert fetched is not None
    assert fetched.catalog_id == installed.catalog_id
    assert fetched.revision == installed.revision
    assert fetched.purpose is ModelPurpose.EMBEDDING
    assert fetched.directory == installed.directory
    assert fetched.manifest_checksum == installed.manifest_checksum
    assert fetched.validation_state == installed.validation_state
    assert fetched.ownership == installed.ownership
    assert fetched.installed_at == installed.installed_at


@pytest.mark.asyncio
async def test_register_round_trips_metadata(registry):
    installed = _make_installed(
        id="local:meta-model",
        metadata={"source": "modelscope", "size_mb": 128, "tags": ["int8", "zh"]},
    )
    await registry.register(installed)
    fetched = await registry.get("local:meta-model")
    assert fetched is not None
    assert fetched.metadata["source"] == "modelscope"
    assert fetched.metadata["size_mb"] == 128
    # InstalledModel freezes metadata (lists -> tuples), so round-trip preserves
    # the frozen form.
    assert fetched.metadata["tags"] == ("int8", "zh")
    assert fetched.to_dict()["metadata"]["tags"] == ["int8", "zh"]


@pytest.mark.asyncio
async def test_register_round_trips_nested_metadata_json_serializable(registry):
    # 嵌套 dict 经 _freeze 后变成 MappingProxyType，落库必须深度普通化，
    # 否则 LocalAIDB.insert_model 的 json.dumps 会抛 TypeError。
    installed = _make_installed(
        id="local:chat-model",
        purpose=ModelPurpose.CHAT,
        metadata={
            "source": "modelscope",
            "repository": "owner/chat",
            "compatibility": {"runtimes": ["ort_genai"], "providers": ["cpu"]},
            "runtime_requirements": {"minimum_ram": 1024, "recommended_ram": 2048},
        },
    )
    await registry.register(installed)
    fetched = await registry.get("local:chat-model")
    assert fetched is not None
    assert fetched.metadata["compatibility"]["runtimes"] == ("ort_genai",)
    assert fetched.metadata["runtime_requirements"]["minimum_ram"] == 1024
    # 落库序列化后，嵌套 dict/list 应还原为普通 dict/list（非 mappingproxy/tuple）
    assert fetched.to_dict()["metadata"]["compatibility"]["runtimes"] == ["ort_genai"]
    assert fetched.to_dict()["metadata"]["runtime_requirements"]["minimum_ram"] == 1024


@pytest.mark.asyncio
async def test_register_with_duplicate_id_raises(registry):
    installed = _make_installed(id="local:dup")
    await registry.register(installed)
    with pytest.raises(ModelAlreadyExistsError):
        await registry.register(installed)


@pytest.mark.asyncio
async def test_concurrent_register_with_duplicate_id_maps_domain_error(registry):
    installed = _make_installed(id="local:concurrent-dup")
    results = await asyncio.gather(
        registry.register(installed),
        registry.register(installed),
        return_exceptions=True,
    )

    assert sum(isinstance(result, InstalledModel) for result in results) == 1
    assert sum(isinstance(result, ModelAlreadyExistsError) for result in results) == 1
    models = await registry.list()
    assert sum(model.id == installed.id for model in models) == 1


@pytest.mark.asyncio
async def test_register_with_colliding_directory_raises(registry):
    directory = str(Path("/models/shared-dir"))
    first = _make_installed(id="local:first", directory=directory)
    second = _make_installed(id="local:second", directory=directory)
    await registry.register(first)
    with pytest.raises(ModelPathCollisionError):
        await registry.register(second)


@pytest.mark.asyncio
async def test_concurrent_register_with_colliding_directory_maps_domain_error(registry):
    directory = str(Path("/models/concurrent-shared-dir"))
    first = _make_installed(id="local:concurrent-first", directory=directory)
    second = _make_installed(id="local:concurrent-second", directory=directory)
    results = await asyncio.gather(
        registry.register(first),
        registry.register(second),
        return_exceptions=True,
    )

    assert sum(isinstance(result, InstalledModel) for result in results) == 1
    assert sum(isinstance(result, ModelPathCollisionError) for result in results) == 1
    models = await registry.list()
    assert sum(model.directory == directory for model in models) == 1


@pytest.mark.asyncio
async def test_cross_connection_duplicate_id_maps_domain_error(tmp_path):
    db_path = tmp_path / "cross-connection-id.db"
    first_manager = DatabaseManager(db_path)
    second_manager = DatabaseManager(db_path)
    await first_manager.init()
    await second_manager.init()
    try:
        first_registry = ModelRegistry(first_manager)
        second_registry = ModelRegistry(second_manager)
        installed = _make_installed(id="local:cross-connection-dup")

        results = await asyncio.gather(
            first_registry.register(installed),
            second_registry.register(installed),
            return_exceptions=True,
        )

        assert sum(isinstance(result, InstalledModel) for result in results) == 1
        assert sum(isinstance(result, ModelAlreadyExistsError) for result in results) == 1, results
    finally:
        await second_manager.close()
        await first_manager.close()


@pytest.mark.asyncio
async def test_cross_connection_directory_collision_maps_domain_error(tmp_path):
    db_path = tmp_path / "cross-connection-directory.db"
    first_manager = DatabaseManager(db_path)
    second_manager = DatabaseManager(db_path)
    await first_manager.init()
    await second_manager.init()
    try:
        first_registry = ModelRegistry(first_manager)
        second_registry = ModelRegistry(second_manager)
        directory = str(Path("/models/cross-connection-shared-dir"))
        first = _make_installed(id="local:cross-connection-first", directory=directory)
        second = _make_installed(id="local:cross-connection-second", directory=directory)

        results = await asyncio.gather(
            first_registry.register(first),
            second_registry.register(second),
            return_exceptions=True,
        )

        assert sum(isinstance(result, InstalledModel) for result in results) == 1
        assert sum(isinstance(result, ModelPathCollisionError) for result in results) == 1
    finally:
        await second_manager.close()
        await first_manager.close()


@pytest.mark.asyncio
async def test_cross_connection_remove_maps_single_winner_and_not_found(tmp_path):
    db_path = tmp_path / "cross-connection-remove.db"
    first_manager = DatabaseManager(db_path)
    second_manager = DatabaseManager(db_path)
    await first_manager.init()
    await second_manager.init()
    try:
        first_registry = ModelRegistry(first_manager)
        second_registry = ModelRegistry(second_manager)
        installed = _make_installed(id="local:cross-connection-remove")
        await first_registry.register(installed)

        results = await asyncio.gather(
            first_registry.remove(installed.id),
            second_registry.remove(installed.id),
            return_exceptions=True,
        )

        assert sum(result is None for result in results) == 1
        assert sum(isinstance(result, ModelNotFoundError) for result in results) == 1
        assert await first_registry.get(installed.id) is None
        assert await second_registry.get(installed.id) is None
    finally:
        await second_manager.close()
        await first_manager.close()


@pytest.mark.asyncio
async def test_register_returns_inserted_row_without_post_transaction_lookup(
    registry, monkeypatch
):
    installed = _make_installed(id="local:transaction-return")

    async def reject_post_transaction_lookup(model_id):
        raise AssertionError(f"unexpected post-transaction lookup: {model_id}")

    monkeypatch.setattr(registry._db, "get_model", reject_post_transaction_lookup)

    saved = await registry.register(installed)

    assert saved.to_dict() == installed.to_dict()


@pytest.mark.asyncio
async def test_register_lets_database_constraint_arbitrate_uniqueness(manager):
    registry = ModelRegistry(manager)
    installed = _make_installed(id="local:constraint-arbitrated")
    statements = []
    await manager._conn.set_trace_callback(statements.append)
    try:
        await registry.register(installed)
    finally:
        await manager._conn.set_trace_callback(None)

    relevant = [
        statement.upper()
        for statement in statements
        if "INSTALLED_MODELS" in statement.upper()
    ]
    insert_index = next(
        index for index, statement in enumerate(relevant) if statement.lstrip().startswith("INSERT")
    )

    assert not any(
        statement.lstrip().startswith("SELECT") for statement in relevant[:insert_index]
    )


@pytest.mark.asyncio
async def test_register_with_colliding_directory_does_not_partial_insert(registry):
    directory = str(Path("/models/shared-dir"))
    first = _make_installed(id="local:first", directory=directory)
    second = _make_installed(id="local:second", directory=directory)
    await registry.register(first)
    with pytest.raises(ModelPathCollisionError):
        await registry.register(second)
    assert await registry.get("local:second") is None


@pytest.mark.asyncio
async def test_mark_validation_updates_state_and_checksum(registry):
    installed = _make_installed(
        id="local:validate-me", validation_state="pending", manifest_checksum="old"
    )
    await registry.register(installed)
    updated = await registry.mark_validation(
        "local:validate-me", "validated", "sha256:new"
    )
    assert updated.validation_state == "validated"
    assert updated.manifest_checksum == "sha256:new"
    fetched = await registry.get("local:validate-me")
    assert fetched is not None
    assert fetched.validation_state == "validated"
    assert fetched.manifest_checksum == "sha256:new"


@pytest.mark.asyncio
async def test_mark_validation_unknown_model_raises(registry):
    with pytest.raises(ModelNotFoundError):
        await registry.mark_validation("local:missing", "validated", "sha256:x")


@pytest.mark.asyncio
async def test_mark_validation_blocked_for_bundled_model(registry):
    with pytest.raises(ModelRemovalBlockedError):
        await registry.mark_validation(BUNDLED_ID, "invalid", "sha256:bad")


@pytest.mark.asyncio
async def test_mark_validation_rechecks_bundled_ownership_in_write_transaction(
    manager, registry, monkeypatch
):
    installed = _make_installed(
        id="local:validation-race",
        validation_state="pending",
        manifest_checksum="sha256:old",
    )
    await registry.register(installed)
    operation_started = asyncio.Event()
    release_operation = asyncio.Event()
    original = registry._db.mark_validation_if_mutable

    async def synchronized_mark_validation(model_id, state, checksum):
        operation_started.set()
        await release_operation.wait()
        return await original(model_id, state, checksum)

    monkeypatch.setattr(
        registry._db, "mark_validation_if_mutable", synchronized_mark_validation
    )
    task = asyncio.create_task(
        registry.mark_validation(installed.id, "validated", "sha256:new")
    )
    await asyncio.wait_for(operation_started.wait(), timeout=1)
    async with manager.write_transaction() as conn:
        await conn.execute(
            "UPDATE installed_models SET ownership = 'bundled' WHERE id = ?",
            (installed.id,),
        )
    release_operation.set()

    with pytest.raises(ModelRemovalBlockedError):
        await task
    saved = await registry.get(installed.id)
    assert saved is not None
    assert saved.ownership == "bundled"
    assert saved.validation_state == "pending"
    assert saved.manifest_checksum == "sha256:old"


@pytest.mark.asyncio
async def test_mark_validation_condition_blocks_cross_connection_bundled_race(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "cross-connection-validation-race.db"
    first_manager = DatabaseManager(db_path)
    second_manager = DatabaseManager(db_path)
    await first_manager.init()
    await second_manager.init()
    try:
        first_registry = ModelRegistry(first_manager)
        installed = _make_installed(
            id="local:cross-connection-validation-race",
            validation_state="pending",
            manifest_checksum="sha256:old",
        )
        await first_registry.register(installed)
        update_reached = asyncio.Event()
        release_update = asyncio.Event()
        original_execute = first_manager._conn.execute

        async def synchronized_execute(sql, parameters=None):
            normalized = " ".join(sql.split()).upper()
            if normalized.startswith("UPDATE INSTALLED_MODELS SET VALIDATION_STATE"):
                update_reached.set()
                await release_update.wait()
            if parameters is None:
                return await original_execute(sql)
            return await original_execute(sql, parameters)

        monkeypatch.setattr(first_manager._conn, "execute", synchronized_execute)
        task = asyncio.create_task(
            first_registry.mark_validation(
                installed.id, "validated", "sha256:new"
            )
        )
        await asyncio.wait_for(update_reached.wait(), timeout=1)
        await second_manager.execute(
            "UPDATE installed_models SET ownership = 'bundled' WHERE id = ?",
            (installed.id,),
        )
        await second_manager.commit()
        release_update.set()

        with pytest.raises(ModelRemovalBlockedError):
            await task
        saved = await second_manager.fetch_one(
            "SELECT * FROM installed_models WHERE id = ?", (installed.id,)
        )
        assert saved["ownership"] == "bundled"
        assert saved["validation_state"] == "pending"
        assert saved["manifest_checksum"] == "sha256:old"
    finally:
        await second_manager.close()
        await first_manager.close()


@pytest.mark.asyncio
async def test_remove_deletes_non_bundled_model(registry):
    installed = _make_installed(id="local:removable")
    await registry.register(installed)
    await registry.remove("local:removable")
    assert await registry.get("local:removable") is None


@pytest.mark.asyncio
async def test_remove_unknown_model_raises(registry):
    with pytest.raises(ModelNotFoundError):
        await registry.remove("local:never-existed")


@pytest.mark.asyncio
async def test_remove_bundled_model_raises(registry):
    with pytest.raises(ModelRemovalBlockedError):
        await registry.remove(BUNDLED_ID)


@pytest.mark.asyncio
async def test_remove_rechecks_bundled_ownership_in_write_transaction(
    manager, registry, monkeypatch
):
    installed = _make_installed(id="local:remove-race")
    await registry.register(installed)
    operation_started = asyncio.Event()
    release_operation = asyncio.Event()
    original = registry._db.delete_if_mutable

    async def synchronized_delete(model_id):
        operation_started.set()
        await release_operation.wait()
        return await original(model_id)

    monkeypatch.setattr(registry._db, "delete_if_mutable", synchronized_delete)
    task = asyncio.create_task(registry.remove(installed.id))
    await asyncio.wait_for(operation_started.wait(), timeout=1)
    async with manager.write_transaction() as conn:
        await conn.execute(
            "UPDATE installed_models SET ownership = 'bundled' WHERE id = ?",
            (installed.id,),
        )
    release_operation.set()

    with pytest.raises(ModelRemovalBlockedError):
        await task
    saved = await registry.get(installed.id)
    assert saved is not None
    assert saved.ownership == "bundled"


@pytest.mark.asyncio
async def test_remove_does_not_affect_other_models(registry):
    a = _make_installed(id="local:a", directory=str(Path("/models/a")))
    b = _make_installed(id="local:b", directory=str(Path("/models/b")))
    await registry.register(a)
    await registry.register(b)
    await registry.remove("local:a")
    assert await registry.get("local:a") is None
    assert await registry.get("local:b") is not None


# ─────────────────────────────────────────────────────────────
# Registry constructed directly from LocalAIDB (no DatabaseManager)
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_registry_accepts_local_ai_db_directly(manager):
    registry = ModelRegistry(manager.local_ai)
    model = await registry.get(BUNDLED_ID)
    assert model is not None
    assert model.ownership == "bundled"


@pytest.mark.asyncio
async def test_direct_local_ai_db_serializes_distinct_concurrent_registrations(manager):
    registry = ModelRegistry(LocalAIDB(manager._conn))
    first = _make_installed(
        id="local:direct-concurrent-distinct-first",
        directory=str(Path("/models/direct-concurrent-distinct-first")),
    )
    second = _make_installed(
        id="local:direct-concurrent-distinct-second",
        directory=str(Path("/models/direct-concurrent-distinct-second")),
    )

    results = await asyncio.gather(
        registry.register(first),
        registry.register(second),
        return_exceptions=True,
    )

    assert all(isinstance(result, InstalledModel) for result in results)
    models = await registry.list()
    assert {first.id, second.id} <= {model.id for model in models}


@pytest.mark.asyncio
async def test_direct_local_ai_db_serializes_concurrent_duplicate_id(manager):
    registry = ModelRegistry(LocalAIDB(manager._conn))
    installed = _make_installed(id="local:direct-concurrent-dup")

    results = await asyncio.gather(
        registry.register(installed),
        registry.register(installed),
        return_exceptions=True,
    )

    assert sum(isinstance(result, InstalledModel) for result in results) == 1
    assert sum(isinstance(result, ModelAlreadyExistsError) for result in results) == 1
    assert sum(model.id == installed.id for model in await registry.list()) == 1


@pytest.mark.asyncio
async def test_direct_local_ai_db_serializes_concurrent_directory_collision(manager):
    registry = ModelRegistry(LocalAIDB(manager._conn))
    directory = str(Path("/models/direct-concurrent-shared-dir"))
    first = _make_installed(id="local:direct-concurrent-first", directory=directory)
    second = _make_installed(id="local:direct-concurrent-second", directory=directory)

    results = await asyncio.gather(
        registry.register(first),
        registry.register(second),
        return_exceptions=True,
    )

    assert sum(isinstance(result, InstalledModel) for result in results) == 1
    assert sum(isinstance(result, ModelPathCollisionError) for result in results) == 1
    assert sum(model.directory == directory for model in await registry.list()) == 1


@pytest.mark.asyncio
async def test_same_connection_serializes_manager_and_direct_local_ai_entries(manager):
    registry = ModelRegistry(LocalAIDB(manager._conn))
    installed = _make_installed(id="local:cross-entry-serialized")

    async with manager.write_transaction():
        task = asyncio.create_task(registry.register(installed))
        await asyncio.sleep(0.05)
        completed_while_manager_held_transaction = task.done()

    result = await task

    assert completed_while_manager_held_transaction is False
    assert result.id == installed.id


@pytest.mark.asyncio
async def test_same_connection_serializes_distinct_local_ai_entries(manager):
    first_registry = ModelRegistry(LocalAIDB(manager._conn))
    second_registry = ModelRegistry(LocalAIDB(manager._conn))
    installed = _make_installed(id="local:cross-local-ai-serialized")

    async with first_registry._db._transaction():
        task = asyncio.create_task(second_registry.register(installed))
        await asyncio.sleep(0.05)
        completed_while_first_entry_held_transaction = task.done()

    result = await task

    assert completed_while_first_entry_held_transaction is False
    assert result.id == installed.id


@pytest.mark.asyncio
async def test_same_connection_rollback_does_not_undo_other_entry_commit(manager):
    registry = ModelRegistry(LocalAIDB(manager._conn))
    rolled_back = _make_installed(id="local:cross-entry-rolled-back")
    committed = _make_installed(id="local:cross-entry-committed")
    committed_task = None

    with pytest.raises(RuntimeError, match="force rollback"):
        async with manager.write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO installed_models (
                    id, catalog_id, revision, purpose, directory,
                    manifest_checksum, validation_state, ownership,
                    installed_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rolled_back.id,
                    rolled_back.catalog_id,
                    rolled_back.revision,
                    rolled_back.purpose.value,
                    rolled_back.directory,
                    rolled_back.manifest_checksum,
                    rolled_back.validation_state,
                    rolled_back.ownership,
                    rolled_back.installed_at.isoformat(),
                    "{}",
                ),
            )
            committed_task = asyncio.create_task(registry.register(committed))
            await asyncio.sleep(0.05)
            raise RuntimeError("force rollback")

    assert committed_task is not None
    saved = await committed_task

    assert saved.id == committed.id
    assert await registry.get(rolled_back.id) is None
    assert await registry.get(committed.id) is not None


# ─────────────────────────────────────────────────────────────
# Round-trip preservation
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_registered_model_round_trips_through_to_dict(registry):
    installed = _make_installed(
        id="local:round-trip",
        metadata={"quantization": "int8", "dimensions": 512},
    )
    await registry.register(installed)
    fetched = await registry.get("local:round-trip")
    assert fetched is not None
    assert fetched.to_dict() == installed.to_dict()


@pytest.mark.asyncio
async def test_list_returns_all_models_ordered_by_installed_at(registry):
    first = _make_installed(id="local:lst-1", directory=str(Path("/models/lst-1")))
    second = _make_installed(id="local:lst-2", directory=str(Path("/models/lst-2")))
    await registry.register(first)
    await registry.register(second)
    models = await registry.list()
    ids = [m.id for m in models]
    assert BUNDLED_ID in ids
    assert "local:lst-1" in ids
    assert "local:lst-2" in ids
