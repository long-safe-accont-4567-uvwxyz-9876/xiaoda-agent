from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
import time
from types import SimpleNamespace

import pytest

from web.routers import auth


GRACE = 30.0  # 与 auth._RENEWAL_GRACE_SECONDS 保持一致


class _FakeClock:
    """可手动推进的时钟，用于精确控制宽限期边界。"""

    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _FakeRequest:
    """get_current_user 只用到 headers 与 state，最小化构造。"""

    def __init__(self, token: str) -> None:
        self.headers = {"Authorization": f"Bearer {token}"}
        self.state = SimpleNamespace()


def _setup_auth(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(auth, "_SECRET", "test-secret")
    monkeypatch.setattr(auth, "_get_revoked_path", lambda: tmp_path / "revoked.json")
    monkeypatch.setattr(auth, "_get_token_epoch_path", lambda: tmp_path / "epoch")
    monkeypatch.setattr(auth, "_token_epoch", None)
    auth._tokens.clear()
    auth._revoked_cache.clear()
    auth._revoked_cache_mtime = 0.0
    grace = getattr(auth, "_revoked_grace", None)
    if grace is not None:
        grace.clear()


def _make_token(expiry: float) -> str:
    """按 auth 的 token 格式手工签发一个指定过期时间的 token。"""
    nonce = secrets.token_hex(8)
    epoch = auth._load_token_epoch()
    payload = f"{expiry}.{nonce}.{epoch}"
    sig = hmac.new(auth._SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()


def test_revoke_with_grace_allows_access_within_window(monkeypatch, tmp_path):
    _setup_auth(monkeypatch, tmp_path)
    clock = _FakeClock(1_000_000.0)
    monkeypatch.setattr(auth, "_now", clock, raising=False)

    token, _ = auth._issue_token()
    auth._revoke_token(token, grace_seconds=GRACE)

    # 宽限期内：续期撤销的旧 token 仍可通过校验（并发请求 B 不会被误伤）
    assert auth._is_revoked(token) is False

    # 宽限期过后：撤销正常生效
    clock.t += GRACE + 1
    assert auth._is_revoked(token) is True


def test_revoke_without_grace_is_immediate(monkeypatch, tmp_path):
    _setup_auth(monkeypatch, tmp_path)

    token, _ = auth._issue_token()
    auth._revoke_token(token)  # logout / revoke-all 默认无宽限期

    assert auth._is_revoked(token) is True


@pytest.mark.asyncio
async def test_concurrent_sliding_renewal_does_not_401(monkeypatch, tmp_path):
    _setup_auth(monkeypatch, tmp_path)

    # 构造一个剩余 <1 天的旧 token，模拟两个并发请求持有同一 token
    token = _make_token(time.time() + 3600)
    req_a = _FakeRequest(token)
    req_b = _FakeRequest(token)

    results = await asyncio.gather(
        auth.get_current_user(req_a),
        auth.get_current_user(req_b),
        return_exceptions=True,
    )

    # 两个请求都不应 401（请求 B 校验时旧 token 已被 A 续期撤销，但仍在宽限期内）
    assert all(isinstance(r, str) and r == "webui" for r in results), results
    # 且滑动续期确实触发，两边都拿到新 token
    assert getattr(req_a.state, "new_token", None) is not None
    assert getattr(req_b.state, "new_token", None) is not None
