"""local_ai.models.registry — Persistent Model Registry high-level interface.

Wraps :class:`db.db_local_ai.LocalAIDB` with business logic:
- Bundled model protection: ``ownership == "bundled"`` records cannot be removed
  nor have their validation state mutated through this registry.
- Path collision: registering two models with the same ``directory`` is rejected.
- Duplicate ID protection: re-registering an existing ID raises
  :class:`ModelAlreadyExistsError`.
- Schema seeding: the bundled BGE embedding model entry is seeded by migration
  v25 and survives re-runs idempotently.

The registry accepts either a :class:`db.database.DatabaseManager` (uses its
``local_ai`` attribute) or a raw :class:`LocalAIDB` instance. All multi-statement
writes flow through ``LocalAIDB._transaction()`` which delegates to the
``DatabaseManager.write_transaction()`` serialization lock.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from db.db_local_ai import ModelMutationStatus
from local_ai.contracts import InstalledModel, ModelPurpose

if TYPE_CHECKING:  # pragma: no cover - typing only
    from db.database import DatabaseManager
    from db.db_local_ai import LocalAIDB


# ── Exceptions ──────────────────────────────────────────────


class ModelRemovalBlockedError(ValueError):
    """Raised when attempting to remove or mutate a bundled model record."""


class ModelAlreadyExistsError(ValueError):
    """Raised when registering a model whose ID is already present."""


class ModelPathCollisionError(ValueError):
    """Raised when registering a model whose directory is already in use."""


class ModelNotFoundError(ValueError):
    """Raised when a model_id refers to no installed record."""


# ── Helpers ─────────────────────────────────────────────────


def _record_to_installed(record: dict[str, Any]) -> InstalledModel:
    """Convert a raw DB row dict into an InstalledModel frozen dataclass."""
    return InstalledModel.from_dict(
        {
            "id": record["id"],
            "catalog_id": record["catalog_id"],
            "revision": record["revision"],
            "purpose": ModelPurpose(record["purpose"]),
            "directory": record["directory"],
            "manifest_checksum": record["manifest_checksum"],
            "validation_state": record["validation_state"],
            "ownership": record["ownership"],
            "installed_at": record["installed_at"],
            "metadata": record.get("metadata", {}) or {},
        }
    )


# ── Registry ────────────────────────────────────────────────


class ModelRegistry:
    """High-level persistent model registry.

    Args:
        db_manager_or_local_db: either a ``DatabaseManager`` (its ``local_ai``
            attribute is used) or a ``LocalAIDB`` instance directly.
    """

    OWNERSHIP_BUNDLED = "bundled"

    def __init__(self, db_manager_or_local_db: "DatabaseManager | LocalAIDB") -> None:
        # Late import to avoid a circular import at module load time.
        from db.db_local_ai import LocalAIDB

        if isinstance(db_manager_or_local_db, LocalAIDB):
            self._db: LocalAIDB = db_manager_or_local_db
        else:
            local_ai = getattr(db_manager_or_local_db, "local_ai", None)
            if local_ai is None:
                raise ValueError(
                    "DatabaseManager.local_ai is not initialized; "
                    "call manager.init() first"
                )
            self._db = local_ai

    # ── Reads ────────────────────────────────────────────

    async def list(self) -> list[InstalledModel]:
        """Return all installed models ordered by installed_at ascending."""
        rows = await self._db.list_models()
        return [_record_to_installed(row) for row in rows]

    async def get(self, model_id: str) -> InstalledModel | None:
        """Return the model with the given ID, or ``None`` if not present."""
        row = await self._db.get_model(model_id)
        return _record_to_installed(row) if row is not None else None

    # ── Writes ───────────────────────────────────────────

    async def register(self, installed: InstalledModel) -> InstalledModel:
        """Insert a new installed model record.

        Raises:
            ModelAlreadyExistsError: if a model with the same ID already exists.
            ModelPathCollisionError: if a model with the same directory already exists.
        """
        conflict, saved = await self._db.insert_model(
            self._installed_to_record(installed)
        )
        if conflict == "id":
            raise ModelAlreadyExistsError(
                f"model with id={installed.id!r} already exists"
            )
        if conflict == "directory":
            raise ModelPathCollisionError(
                f"directory {installed.directory!r} is already in use by another model"
            )
        assert saved is not None  # just inserted
        return _record_to_installed(saved)

    async def mark_validation(
        self, model_id: str, state: str, checksum: str
    ) -> InstalledModel:
        """Update ``validation_state`` and ``manifest_checksum`` for a model.

        Bundled models are immutable through this API; use a migration to change
        their validation state.

        Raises:
            ModelNotFoundError: if ``model_id`` does not exist.
            ModelRemovalBlockedError: if the model is bundled.
        """
        if not state or not isinstance(state, str):
            raise ValueError("state must be a non-empty string")
        if not checksum or not isinstance(checksum, str):
            raise ValueError("checksum must be a non-empty string")
        status, saved = await self._db.mark_validation_if_mutable(
            model_id, state, checksum
        )
        if status is ModelMutationStatus.NOT_FOUND:
            raise ModelNotFoundError(f"model not found: {model_id!r}")
        if status is ModelMutationStatus.BUNDLED:
            raise ModelRemovalBlockedError(
                f"bundled model {model_id!r} validation state is immutable"
            )
        assert saved is not None
        return _record_to_installed(saved)

    async def remove(self, model_id: str) -> None:
        """Delete a non-bundled installed model.

        Raises:
            ModelNotFoundError: if ``model_id`` does not exist.
            ModelRemovalBlockedError: if the model is bundled.
        """
        status = await self._db.delete_if_mutable(model_id)
        if status is ModelMutationStatus.NOT_FOUND:
            raise ModelNotFoundError(f"model not found: {model_id!r}")
        if status is ModelMutationStatus.BUNDLED:
            raise ModelRemovalBlockedError(
                f"bundled model {model_id!r} cannot be removed"
            )

    # ── Internal ─────────────────────────────────────────

    @staticmethod
    def _installed_to_record(installed: InstalledModel) -> dict[str, Any]:
        """Flatten an InstalledModel into a DB-ready dict (installed_at kept raw)."""
        return {
            "id": installed.id,
            "catalog_id": installed.catalog_id,
            "revision": installed.revision,
            "purpose": installed.purpose.value,
            "directory": installed.directory,
            "manifest_checksum": installed.manifest_checksum,
            "validation_state": installed.validation_state,
            "ownership": installed.ownership,
            "installed_at": installed.installed_at,
            "metadata": installed.to_dict()["metadata"],
        }


__all__ = [
    "ModelAlreadyExistsError",
    "ModelNotFoundError",
    "ModelPathCollisionError",
    "ModelRegistry",
    "ModelRemovalBlockedError",
]
