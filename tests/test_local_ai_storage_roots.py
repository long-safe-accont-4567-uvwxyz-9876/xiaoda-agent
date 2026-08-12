"""tests/test_local_ai_storage_roots.py — restricted-mode allowed storage roots.

Covers the I1 fix (Task 6 plan Step 3: "paths outside configured roots when
restricted mode is active"):

- Restricted mode (`allowed_storage_roots` non-empty): the destination must
  resolve inside one of the allowed roots; paths outside are rejected,
  including relative paths and symlink escapes through intermediate
  components. Roots are compared by realpath (symbolic-link safe).
- Unrestricted mode (empty configuration): behavior unchanged (regression).
- Config loading parses `LOCAL_AI_ALLOWED_STORAGE_ROOTS` (comma-separated,
  matching the project's list env-var style).
- REST API endpoints inherit the StoragePolicy validation automatically.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from local_ai.models.storage import StoragePolicy, StorageValidation  # noqa: E402
from web.config_service import ConfigService  # noqa: E402
from web.routers import auth as auth_module  # noqa: E402
from web.routers.local_ai_storage import router as local_ai_storage_router  # noqa: E402

# Substring asserted on validation.reason when a destination escapes the roots.
_OUTSIDE_REASON = "outside allowed storage roots"


def _data(r) -> dict:
    """Unwrap Envelope.data from a TestClient response."""
    return r.json()["data"]


@pytest.fixture
def config_service(tmp_path) -> ConfigService:
    """Fresh ConfigService backed by a temp overrides file (no real disk side effects)."""
    return ConfigService(path=tmp_path / "overrides.json")


@pytest.fixture
def policy(config_service) -> StoragePolicy:
    """StoragePolicy in unrestricted mode (empty allowed_storage_roots)."""
    return StoragePolicy(config_service=config_service)


@pytest.fixture
def models_root(tmp_path) -> Path:
    """An allowed storage root for restricted-mode fixtures."""
    root = tmp_path / "models"
    root.mkdir()
    return root


@pytest.fixture
def restricted_config_service(tmp_path, models_root) -> ConfigService:
    """ConfigService with allowed_storage_roots = [models_root] (restricted mode).

    Uses its own overrides file so it never mutates the shared `config_service`
    fixture (which unrestricted-mode tests rely on).
    """
    cs = ConfigService(path=tmp_path / "restricted_overrides.json")
    cs.set("local_ai.allowed_storage_roots", [str(models_root)])
    return cs


@pytest.fixture
def restricted_policy(restricted_config_service) -> StoragePolicy:
    return StoragePolicy(config_service=restricted_config_service)


# ─────────────────────────────────────────────────────────────
# 1. Restricted mode — destinations inside the allowed root pass
# ─────────────────────────────────────────────────────────────

def test_restricted_mode_accepts_path_inside_allowed_root(restricted_policy, models_root):
    """A subdirectory of an allowed root is a valid destination."""
    val = restricted_policy.validate_destination(str(models_root / "bge-m3"), 1024)
    assert isinstance(val, StorageValidation)
    assert val.writable is True
    assert val.error is None
    assert val.path == str(models_root / "bge-m3")


def test_restricted_mode_accepts_root_itself(restricted_policy, models_root):
    val = restricted_policy.validate_destination(str(models_root), 0)
    assert val.writable is True
    assert val.error is None


# ─────────────────────────────────────────────────────────────
# 2. Restricted mode — destinations outside the allowed root are rejected
# ─────────────────────────────────────────────────────────────

def test_restricted_mode_rejects_path_outside_allowed_root(restricted_policy, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    val = restricted_policy.validate_destination(str(outside), 0)
    assert val.writable is False
    assert val.error is not None
    assert _OUTSIDE_REASON in (val.reason or "")


def test_restricted_mode_rejects_relative_path_resolving_outside(
    restricted_policy, tmp_path, monkeypatch,
):
    """A relative path whose resolved target lies outside the allowed root is rejected."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(tmp_path)  # cwd is outside the allowed root
    val = restricted_policy.validate_destination("elsewhere", 0)
    assert val.writable is False
    assert _OUTSIDE_REASON in (val.reason or "")


def test_restricted_mode_accepts_relative_path_inside_allowed_root(
    restricted_policy, models_root, monkeypatch,
):
    sub = models_root / "sub"
    sub.mkdir()
    monkeypatch.chdir(models_root)
    val = restricted_policy.validate_destination("sub", 0)
    assert val.writable is True
    assert val.error is None


def test_restricted_mode_rejects_symlink_escape_through_intermediate_component(
    restricted_policy, policy, tmp_path, models_root,
):
    """A symlinked intermediate component escaping the allowed root is rejected.

    The final component is a real directory, so the pre-resolution final-symlink
    check cannot detect the escape; only the realpath-vs-roots comparison does.
    The same path is accepted in unrestricted mode, proving the boundary is
    added by restricted mode itself.
    """
    outside = tmp_path / "outside"
    (outside / "dest").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    escaped = alias / "dest"
    try:
        val = restricted_policy.validate_destination(str(escaped), 0)
        assert val.writable is False, "symlink escape must be rejected in restricted mode"
        assert _OUTSIDE_REASON in (val.reason or "")
        # Unrestricted mode: the same resolved target is accepted.
        val_unrestricted = policy.validate_destination(str(escaped), 0)
        assert val_unrestricted.writable is True
    finally:
        alias.unlink()


# ─────────────────────────────────────────────────────────────
# 3. Unrestricted mode — empty config keeps previous behavior (regression)
# ─────────────────────────────────────────────────────────────

def test_unrestricted_mode_accepts_paths_outside_any_configured_roots(
    policy, config_service, tmp_path, monkeypatch,
):
    """Empty allowed_storage_roots = unrestricted mode; outside paths still pass."""
    monkeypatch.delenv("LOCAL_AI_ALLOWED_STORAGE_ROOTS", raising=False)
    assert config_service.get("local_ai.allowed_storage_roots", None) == []
    outside = tmp_path / "elsewhere"
    val = policy.validate_destination(str(outside), 0)
    assert val.writable is True
    assert val.error is None


# ─────────────────────────────────────────────────────────────
# 4. Roots are compared by realpath (symbolic-link safe)
# ─────────────────────────────────────────────────────────────

def test_allowed_root_is_realpath_compared(restricted_config_service, tmp_path):
    """A root configured through a symlink alias resolves to the real directory."""
    real = tmp_path / "real_models"
    real.mkdir()
    alias = tmp_path / "models_alias"
    alias.symlink_to(real, target_is_directory=True)
    try:
        restricted_config_service.set("local_ai.allowed_storage_roots", [str(alias)])
        policy = StoragePolicy(config_service=restricted_config_service)
        # Lexically outside the configured alias, but inside its realpath target.
        val = policy.validate_destination(str(real / "sub"), 0)
        assert val.writable is True
    finally:
        alias.unlink()


# ─────────────────────────────────────────────────────────────
# 5. set_default honors restricted mode
# ─────────────────────────────────────────────────────────────

def test_set_default_rejects_path_outside_allowed_root(
    restricted_policy, restricted_config_service, tmp_path,
):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(ValueError):
        restricted_policy.set_default(str(outside))
    assert restricted_config_service.get("local_ai.default_model_root", "") == ""


def test_set_default_persists_path_inside_allowed_root(
    restricted_policy, restricted_config_service, models_root,
):
    restricted_policy.set_default(str(models_root / "sub"))
    assert restricted_config_service.get("local_ai.default_model_root", "") == str(models_root / "sub")


# ─────────────────────────────────────────────────────────────
# 6. REST API endpoints inherit the StoragePolicy validation
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def app(restricted_config_service, monkeypatch) -> FastAPI:
    """Minimal FastAPI app mounting the local-ai-storage router in restricted mode."""
    from web.routers import local_ai_storage as _storage_router

    monkeypatch.setattr(_storage_router, "get_config_service", lambda: restricted_config_service)
    app = FastAPI()
    app.include_router(local_ai_storage_router, prefix="/api/v1")
    return app


@pytest.fixture
def authed_client(app) -> TestClient:
    """TestClient preconfigured with a valid Bearer token (auth internals patched)."""
    secret = "test-secret-roots"
    expiry = time.time() + 3600.0
    nonce = "deadbeef"
    epoch = "0"
    payload = f"{expiry}.{nonce}.{epoch}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()

    auth_module._SECRET = secret  # noqa: SLF001
    auth_module._token_epoch = 0  # noqa: SLF001
    auth_module._tokens[token] = expiry  # noqa: SLF001
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def test_api_validate_inherits_restricted_mode(authed_client, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    r = authed_client.post(
        "/api/v1/local-ai/storage/validate",
        json={"path": str(outside), "required_bytes": 0},
    )
    assert r.status_code == 200  # validation result returned, not an HTTP error
    body = _data(r)
    assert body["writable"] is False
    assert _OUTSIDE_REASON in (body["reason"] or "")


def test_api_put_default_rejects_outside_allowed_root(authed_client, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    r = authed_client.put("/api/v1/local-ai/storage/default", json={"path": str(outside)})
    assert 400 <= r.status_code < 500


# ─────────────────────────────────────────────────────────────
# 7. Config loading parses LOCAL_AI_ALLOWED_STORAGE_ROOTS
# ─────────────────────────────────────────────────────────────

def test_config_service_parses_allowed_storage_roots_env(monkeypatch, tmp_path):
    """Comma-separated env var is split, stripped and stored under local_ai."""
    monkeypatch.setenv("LOCAL_AI_ALLOWED_STORAGE_ROOTS", "/data/models, /mnt/models ")
    cs = ConfigService(path=tmp_path / "overrides.json")
    assert cs.get("local_ai.allowed_storage_roots") == ["/data/models", "/mnt/models"]


def test_config_service_env_unset_or_empty_keeps_empty_roots(monkeypatch, tmp_path):
    monkeypatch.delenv("LOCAL_AI_ALLOWED_STORAGE_ROOTS", raising=False)
    assert ConfigService(path=tmp_path / "a.json").get("local_ai.allowed_storage_roots") == []
    monkeypatch.setenv("LOCAL_AI_ALLOWED_STORAGE_ROOTS", "")
    assert ConfigService(path=tmp_path / "b.json").get("local_ai.allowed_storage_roots") == []
