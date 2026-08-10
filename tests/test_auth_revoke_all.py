from __future__ import annotations

import pytest

from web.routers import auth


@pytest.mark.asyncio
async def test_revoke_all_invalidates_token_evicted_from_lru(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "_SECRET", "test-secret")
    monkeypatch.setattr(auth, "_TOKENS_MAX_SIZE", 1)
    monkeypatch.setattr(auth, "_get_revoked_path", lambda: tmp_path / "revoked.json")
    monkeypatch.setattr(auth, "_get_token_epoch_path", lambda: tmp_path / "epoch")
    monkeypatch.setattr(auth, "_token_epoch", None)
    auth._tokens.clear()
    auth._revoked_cache.clear()
    auth._revoked_cache_mtime = 0.0

    old_token, _ = auth._issue_token()
    current_token, _ = auth._issue_token()
    assert old_token not in auth._tokens

    await auth.revoke_all(user_id="webui")

    assert not auth._validate_token(old_token)
    assert not auth._validate_token(current_token)


def test_epoch_token_expiry_can_be_extracted(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "_SECRET", "test-secret")
    monkeypatch.setattr(auth, "_get_token_epoch_path", lambda: tmp_path / "epoch")
    monkeypatch.setattr(auth, "_token_epoch", None)

    token, expiry = auth._issue_token()

    assert auth._extract_expiry(token) == expiry
