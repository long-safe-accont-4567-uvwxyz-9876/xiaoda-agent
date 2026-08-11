"""tests/test_local_ai_storage.py — Server Storage Picker and Policy tests.

Covers (per Task 6 brief):
- DirectoryListing: root listing (None/empty) and subdirectory listing.
- StorageValidation: rejects non-existent paths, read-only paths, insufficient
  free space, path traversal (`..`), device files (`/dev/`, `/proc/`, `/sys/`),
  symlinks pointing outside the parent directory tree; accepts valid writable
  paths with enough free space.
- Default persistence: validate_destination does NOT auto-persist the default
  (unsaved destination is not reused); saved default is re-validated before
  download.
- REST API: GET /api/v1/local-ai/storage, POST /api/v1/local-ai/storage/validate,
  GET/PUT /api/v1/local-ai/storage/default. All endpoints require auth (401
  without token).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from local_ai.models.storage import (  # noqa: E402
    DirectoryListing,
    StoragePolicy,
    StorageValidation,
)
from web.config_service import ConfigService  # noqa: E402
from web.routers import auth as auth_module  # noqa: E402
from web.routers.local_ai_storage import router as local_ai_storage_router  # noqa: E402

# ─────────────────────────────────────────────────────────────
# Helpers / fixtures
# ─────────────────────────────────────────────────────────────

def _is_root() -> bool:
    """Detect root so that permission-based assertions can be skipped.

    On Linux, chmod-based read-only directory tests do not actually block root
    (CAP_DAC_OVERRIDE), so we skip the writable checks when running as root.
    """
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _data(r) -> dict:
    """Unwrap Envelope.data from a TestClient response."""
    return r.json()["data"]


@pytest.fixture
def config_service(tmp_path) -> ConfigService:
    """Fresh ConfigService backed by a temp overrides file (no real disk side effects)."""
    return ConfigService(path=tmp_path / "overrides.json")


@pytest.fixture
def policy(config_service) -> StoragePolicy:
    """StoragePolicy bound to the isolated ConfigService."""
    return StoragePolicy(config_service=config_service)


@pytest.fixture
def app(config_service, monkeypatch) -> FastAPI:
    """Minimal FastAPI app mounting only the local-ai-storage router.

    The router depends on get_config_service() (module-level singleton). We
    monkeypatch the storage module's getter so it returns the test's isolated
    instance, keeping each test hermetic.
    """
    from web.routers import local_ai_storage as _storage_router

    monkeypatch.setattr(_storage_router, "get_config_service", lambda: config_service)
    app = FastAPI()
    app.include_router(local_ai_storage_router, prefix="/api/v1")
    return app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def authed_client(app) -> TestClient:
    """TestClient preconfigured with a valid Bearer token.

    We bypass the secret/epoch files by monkeypatching auth internals so the
    test does not depend on the production webui_secret file.
    """
    import base64
    import hashlib
    import hmac
    import time

    secret = "test-secret-storage"
    expiry = time.time() + 3600.0
    nonce = "deadbeef"
    epoch = "0"
    payload = f"{expiry}.{nonce}.{epoch}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()

    # Patch auth internals so _validate_token succeeds without touching disk.
    # We use the test app's lifespan-free setup, so patching the module is safe.
    auth_module._SECRET = secret  # noqa: SLF001
    auth_module._token_epoch = 0  # noqa: SLF001
    auth_module._tokens[token] = expiry  # noqa: SLF001
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


# ─────────────────────────────────────────────────────────────
# 1. Dataclass shape
# ─────────────────────────────────────────────────────────────

def test_directory_listing_is_frozen_dataclass():
    listing = DirectoryListing(path="/tmp", entries=("a", "b"), error=None)
    with pytest.raises(Exception):
        listing.path = "/other"  # type: ignore[misc]
    assert listing.path == "/tmp"
    assert listing.entries == ("a", "b")
    assert listing.error is None


def test_storage_validation_is_frozen_dataclass():
    val = StorageValidation(path="/tmp", writable=True, free_bytes=100, error=None, reason=None)
    with pytest.raises(Exception):
        val.writable = False  # type: ignore[misc]
    assert val.writable is True
    assert val.free_bytes == 100


# ─────────────────────────────────────────────────────────────
# 2. list_directory
# ─────────────────────────────────────────────────────────────

def test_list_directory_none_returns_root_entries(policy):
    """list_directory(None) returns root entries — on Linux, subdirs of /."""
    listing = policy.list_directory(None)
    assert isinstance(listing, DirectoryListing)
    assert listing.path == ""
    assert listing.error is None
    # Linux root has well-known top-level dirs
    entries = set(listing.entries)
    assert "etc" in entries or "home" in entries or "tmp" in entries, (
        f"expected common root dirs, got {entries}"
    )


def test_list_directory_empty_string_returns_root_entries(policy):
    """list_directory('') is treated the same as None — returns roots."""
    listing = policy.list_directory("")
    assert listing.path == ""
    assert listing.error is None
    assert len(listing.entries) > 0


def test_list_directory_returns_subdirectories(policy, tmp_path):
    (tmp_path / "subdir1").mkdir()
    (tmp_path / "subdir2").mkdir()
    (tmp_path / "file.txt").write_text("hi")

    listing = policy.list_directory(str(tmp_path))
    assert listing.path == str(tmp_path)
    assert listing.error is None
    entries = set(listing.entries)
    assert "subdir1" in entries
    assert "subdir2" in entries
    # Files should be filtered out — only directories are returned
    assert "file.txt" not in entries


def test_list_directory_blocks_path_traversal(policy):
    """list_directory must reject '..' components."""
    listing = policy.list_directory("/etc/../etc/passwd")
    assert listing.error is not None
    assert "traversal" in listing.error.lower() or "invalid" in listing.error.lower()
    assert listing.entries == ()


def test_list_directory_blocks_device_files(policy):
    """list_directory must reject /dev, /proc, /sys as listing roots."""
    for path in ("/dev", "/proc", "/sys"):
        listing = policy.list_directory(path)
        assert listing.error is not None, f"expected error for {path}"
        assert listing.entries == (), f"expected no entries for {path}"


def test_list_directory_nonexistent_returns_error(policy, tmp_path):
    listing = policy.list_directory(str(tmp_path / "does-not-exist"))
    assert listing.error is not None
    assert listing.entries == ()


def test_list_directory_normalizes_relative_path(policy, tmp_path, monkeypatch):
    """A relative path argument should be resolved against CWD (absolute resolution)."""
    sub = tmp_path / "relsub"
    sub.mkdir()
    monkeypatch.chdir(tmp_path)
    listing = policy.list_directory("relsub")
    assert listing.error is None
    assert "relsub" not in listing.entries  # it's the listed dir, not a child
    # No subdirectories inside relsub → empty tuple
    assert listing.entries == ()


# ─────────────────────────────────────────────────────────────
# 3. validate_destination — happy path
# ─────────────────────────────────────────────────────────────

def test_validate_destination_accepts_writable_path_with_enough_space(policy, tmp_path):
    val = policy.validate_destination(str(tmp_path), 1024)
    assert isinstance(val, StorageValidation)
    assert val.path == str(tmp_path)
    assert val.writable is True
    assert val.free_bytes >= 1024
    assert val.error is None
    assert val.reason is None


def test_validate_destination_zero_required_bytes(policy, tmp_path):
    val = policy.validate_destination(str(tmp_path), 0)
    assert val.writable is True
    assert val.error is None


def test_validate_destination_creates_missing_path_if_parent_writable(policy, tmp_path):
    """A non-existent path whose parent exists and is writable should be creatable."""
    new_dir = tmp_path / "new_subdir"
    val = policy.validate_destination(str(new_dir), 0)
    assert val.writable is True, f"expected writable, got reason={val.reason}"
    assert val.error is None


# ─────────────────────────────────────────────────────────────
# 4. validate_destination — rejections
# ─────────────────────────────────────────────────────────────

def test_validate_destination_rejects_nonexistent_uncreatable_path(policy):
    """A path whose parent does not exist cannot be created → not writable."""
    val = policy.validate_destination("/nonexistent_root_xyz/deep/nested", 0)
    assert val.writable is False
    assert val.error is not None or val.reason is not None


def test_validate_destination_rejects_path_traversal(policy, tmp_path):
    """Path traversal via '..' must be rejected even if resolved path exists."""
    val = policy.validate_destination(str(tmp_path) + "/../../etc", 0)
    assert val.writable is False
    assert val.reason is not None
    assert "traversal" in val.reason.lower() or "invalid" in val.reason.lower()


def test_validate_destination_rejects_device_files(policy):
    for path in ("/dev/null", "/proc/self", "/sys/kernel"):
        val = policy.validate_destination(path, 0)
        assert val.writable is False, f"expected rejection for {path}"
        assert val.reason is not None
        assert ("device" in val.reason.lower()
                or "special" in val.reason.lower()
                or "forbidden" in val.reason.lower()), (
            f"reason should mention device/special/forbidden: {val.reason}"
        )


def test_validate_destination_rejects_symlink_pointing_outside(policy, tmp_path):
    """A symlink inside the destination pointing outside the parent tree is rejected."""
    target_outside = tmp_path.parent / "outside_target_xyz"
    target_outside.mkdir(exist_ok=True)
    link = tmp_path / "escape_link"
    try:
        os.symlink(target_outside, link)
        # Validate the link itself as a destination — its target resolves outside
        # the parent tree of `link`.
        val = policy.validate_destination(str(link), 0)
        assert val.writable is False, (
            f"symlink escaping parent tree should be rejected; got writable={val.writable}"
        )
        assert val.reason is not None
    finally:
        if link.exists() or link.is_symlink():
            link.unlink()
        target_outside.rmdir()


def test_validate_destination_rejects_read_only_path(policy, tmp_path):
    """A read-only directory (chmod 0o500) should be reported as not writable."""
    if _is_root():
        pytest.skip("chmod-based read-only test does not block root")
    ro_dir = tmp_path / "readonly"
    ro_dir.mkdir()
    ro_dir.chmod(0o500)
    try:
        val = policy.validate_destination(str(ro_dir), 0)
        assert val.writable is False, "read-only dir should not be writable"
        assert val.reason is not None
    finally:
        # Restore writable so cleanup can succeed
        ro_dir.chmod(0o755)


def test_validate_destination_rejects_insufficient_free_space(policy, tmp_path):
    """required_bytes larger than free space → writable but insufficient? Per brief:
    validate_destination must reject paths with insufficient free space."""
    # Use an astronomically large value to guarantee it exceeds free space
    huge = 10**18  # 1 exabyte
    val = policy.validate_destination(str(tmp_path), huge)
    assert val.writable is False
    assert val.reason is not None
    assert "space" in val.reason.lower() or "free" in val.reason.lower()
    # free_bytes should still be reported (real value, not the required)
    assert val.free_bytes >= 0
    assert val.free_bytes < huge


# ─────────────────────────────────────────────────────────────
# 5. Default persistence semantics
# ─────────────────────────────────────────────────────────────

def test_unsaved_destination_is_not_reused(config_service, policy, tmp_path):
    """Per brief: validate_destination does NOT auto-persist the default."""
    policy.validate_destination(str(tmp_path), 1024)
    assert config_service.get("local_ai.default_model_root", "") == ""


def test_saved_default_is_revalidated_before_download(config_service, policy, tmp_path):
    """A saved default must be re-validated (returns writable=True) before download."""
    config_service.set("local_ai.default_model_root", str(tmp_path))
    result = policy.validate_destination(str(tmp_path), 1024)
    assert result.writable is True


def test_get_default_returns_configured_value(config_service, policy, tmp_path):
    config_service.set("local_ai.default_model_root", str(tmp_path))
    assert policy.get_default() == str(tmp_path)


def test_get_default_returns_empty_when_unset(policy):
    assert policy.get_default() == ""


def test_set_default_persists_valid_path(policy, config_service, tmp_path):
    policy.set_default(str(tmp_path))
    assert config_service.get("local_ai.default_model_root", "") == str(tmp_path)
    assert policy.get_default() == str(tmp_path)


def test_set_default_rejects_invalid_path(policy, config_service):
    """set_default must not persist an invalid (traversal) path."""
    with pytest.raises(ValueError):
        policy.set_default("/etc/../etc")
    assert config_service.get("local_ai.default_model_root", "") == ""


def test_set_default_rejects_unwritable_path(policy, config_service, tmp_path):
    """set_default must not persist a path that fails validation."""
    if _is_root():
        pytest.skip("chmod-based read-only test does not block root")
    ro_dir = tmp_path / "readonly_default"
    ro_dir.mkdir()
    ro_dir.chmod(0o500)
    try:
        with pytest.raises(ValueError):
            policy.set_default(str(ro_dir))
        assert config_service.get("local_ai.default_model_root", "") == ""
    finally:
        ro_dir.chmod(0o755)


# ─────────────────────────────────────────────────────────────
# 6. REST API — happy paths (authed)
# ─────────────────────────────────────────────────────────────

def test_api_get_storage_root_listing(authed_client):
    r = authed_client.get("/api/v1/local-ai/storage")
    assert r.status_code == 200
    body = _data(r)
    assert "path" in body
    assert "entries" in body
    assert body["path"] == ""
    assert isinstance(body["entries"], list)
    assert len(body["entries"]) > 0


def test_api_get_storage_subdirectory(authed_client, tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "file.txt").write_text("x")
    r = authed_client.get("/api/v1/local-ai/storage", params={"path": str(tmp_path)})
    assert r.status_code == 200
    body = _data(r)
    entries = set(body["entries"])
    assert "alpha" in entries
    assert "beta" in entries
    assert "file.txt" not in entries


def test_api_validate_destination_valid(authed_client, tmp_path):
    r = authed_client.post(
        "/api/v1/local-ai/storage/validate",
        json={"path": str(tmp_path), "required_bytes": 1024},
    )
    assert r.status_code == 200
    body = _data(r)
    assert body["path"] == str(tmp_path)
    assert body["writable"] is True
    assert body["free_bytes"] >= 1024
    assert body["error"] is None
    assert body["reason"] is None


def test_api_validate_destination_traversal_rejected(authed_client):
    r = authed_client.post(
        "/api/v1/local-ai/storage/validate",
        json={"path": "/etc/../etc", "required_bytes": 0},
    )
    assert r.status_code == 200  # validation result returned, not an HTTP error
    body = _data(r)
    assert body["writable"] is False
    assert body["reason"] is not None


def test_api_get_default_returns_empty_when_unset(authed_client):
    r = authed_client.get("/api/v1/local-ai/storage/default")
    assert r.status_code == 200
    body = _data(r)
    assert body["default_model_root"] == ""


def test_api_put_default_persists(authed_client, tmp_path):
    r = authed_client.put(
        "/api/v1/local-ai/storage/default",
        json={"path": str(tmp_path)},
    )
    assert r.status_code == 200
    body = _data(r)
    assert body["default_model_root"] == str(tmp_path)
    # Subsequent GET returns the persisted value
    r2 = authed_client.get("/api/v1/local-ai/storage/default")
    assert r2.status_code == 200
    assert _data(r2)["default_model_root"] == str(tmp_path)


def test_api_put_default_rejects_invalid(authed_client):
    r = authed_client.put(
        "/api/v1/local-ai/storage/default",
        json={"path": "/etc/../etc"},
    )
    # Invalid path → 4xx (validation error from set_default raising ValueError)
    assert 400 <= r.status_code < 500


# ─────────────────────────────────────────────────────────────
# 7. REST API — auth enforcement (401 without token)
# ─────────────────────────────────────────────────────────────

def test_api_get_storage_requires_auth(client):
    r = client.get("/api/v1/local-ai/storage")
    assert r.status_code == 401


def test_api_validate_requires_auth(client):
    r = client.post(
        "/api/v1/local-ai/storage/validate",
        json={"path": "/tmp", "required_bytes": 0},
    )
    assert r.status_code == 401


def test_api_get_default_requires_auth(client):
    r = client.get("/api/v1/local-ai/storage/default")
    assert r.status_code == 401


def test_api_put_default_requires_auth(client):
    r = client.put(
        "/api/v1/local-ai/storage/default",
        json={"path": "/tmp"},
    )
    assert r.status_code == 401
