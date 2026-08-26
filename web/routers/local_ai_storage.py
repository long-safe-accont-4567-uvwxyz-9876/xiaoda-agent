"""web/routers/local_ai_storage.py — Local AI Storage Picker REST API.

Endpoints (all require auth via get_current_user):
- GET  /api/v1/local-ai/storage           — list subdirectories (root listing
  when `path` query param is absent or empty).
- POST /api/v1/local-ai/storage/validate  — validate a destination path
  (existence, writability, free space) and return the result without
  persisting it.
- GET  /api/v1/local-ai/storage/default   — read the persisted
  default_model_root (empty string when unset).
- PUT  /api/v1/local-ai/storage/default   — validate then persist a new
  default_model_root. Returns 4xx on validation failure.

All endpoints return Envelope[dict]. Validation failures from
`POST /validate` are reported inside the envelope (writable=False); PUT default
raises HTTP 4xx because the user explicitly asked to persist.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from local_ai.models.storage import (
    DirectoryListing,
    StoragePolicy,
    StorageValidation,
)
from web.config_service import get_config_service
from web.routers.auth import get_current_user
from web.schemas import Envelope

router = APIRouter(
    tags=["local-ai-storage"],
    dependencies=[Depends(get_current_user)],
)


class ValidateRequest(BaseModel):
    path: str = Field(..., description="Absolute or relative path to validate.")
    required_bytes: int = Field(
        0, ge=0, description="Required free bytes; 0 skips the space check."
    )


class SetDefaultRequest(BaseModel):
    path: str = Field(..., description="Path to persist as the default model root.")


def _policy() -> StoragePolicy:
    """Build a StoragePolicy bound to the global ConfigService singleton."""
    return StoragePolicy(config_service=get_config_service())


@router.get("/local-ai/storage", response_model=Envelope[dict])
async def list_storage(
    path: str = "",
    user_id: str = Depends(get_current_user),
) -> Any:
    """List subdirectories of `path` (root listing when path absent/empty)."""
    policy = _policy()
    listing: DirectoryListing = policy.list_directory(path or None)
    return Envelope(data=listing.to_dict())


@router.post("/local-ai/storage/validate", response_model=Envelope[dict])
async def validate_destination(
    body: ValidateRequest,
    user_id: str = Depends(get_current_user),
) -> Any:
    """Validate a destination path; never raises on validation failure.

    The result (writable, free_bytes, error, reason) is returned inside the
    envelope so the UI can show the specific reason.
    """
    policy = _policy()
    result: StorageValidation = policy.validate_destination(body.path, body.required_bytes)
    return Envelope(data=result.to_dict())


@router.get("/local-ai/storage/default", response_model=Envelope[dict])
async def get_default_storage(user_id: str = Depends(get_current_user)) -> Any:
    """Return the persisted default_model_root (empty string when unset)."""
    policy = _policy()
    return Envelope(data={"default_model_root": policy.get_default()})


@router.put("/local-ai/storage/default", response_model=Envelope[dict])
async def set_default_storage(
    body: SetDefaultRequest,
    user_id: str = Depends(get_current_user),
) -> Any:
    """Validate then persist a new default_model_root.

    Raises 422 when validation fails (invalid path, not writable, etc.).
    """
    policy = _policy()
    try:
        policy.set_default(body.path)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Envelope(data={"default_model_root": policy.get_default()})
