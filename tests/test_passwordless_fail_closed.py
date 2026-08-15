from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from web.routers import auth
from web.schemas import LoginRequest


def _make_request() -> SimpleNamespace:
    request = SimpleNamespace()
    request.client = SimpleNamespace(host="127.0.0.1")
    return request


def _setup(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(auth, "_SECRET", "test-secret")
    monkeypatch.setattr(auth, "_get_token_epoch_path", lambda: tmp_path / "epoch")
    monkeypatch.setattr(auth, "_token_epoch", None)
    monkeypatch.delenv("WEBUI_PASSWORD", raising=False)
    auth._rate_limit.clear()
    auth._tokens.clear()


@pytest.mark.asyncio
async def test_passwordless_loopback_bind_issues_token(monkeypatch, tmp_path):
    """未设置 WEBUI_PASSWORD 且监听回环地址时，保持现有免密行为。"""
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(auth, "_WEBUI_BIND_HOST", "127.0.0.1")
    monkeypatch.setattr(auth, "_get_client_ip", lambda request: "127.0.0.1")

    result = await auth.login(LoginRequest(password=""), _make_request())

    assert result.data is not None
    assert result.data.token
    assert result.data.expires_at > 0


@pytest.mark.asyncio
async def test_passwordless_non_loopback_bind_denies(monkeypatch, tmp_path):
    """未设置 WEBUI_PASSWORD 且监听非回环地址时，不发放 token。"""
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(auth, "_WEBUI_BIND_HOST", "0.0.0.0")
    monkeypatch.setattr(auth, "_get_client_ip", lambda request: "127.0.0.1")

    with pytest.raises(HTTPException) as exc_info:
        await auth.login(LoginRequest(password=""), _make_request())

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_passwordless_public_client_denies_even_loopback_bind(monkeypatch, tmp_path):
    """监听回环但客户端来自公网时，仍拒绝免密（保留原有 client_ip 判定）。"""
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(auth, "_WEBUI_BIND_HOST", "127.0.0.1")
    monkeypatch.setattr(auth, "_get_client_ip", lambda request: "8.8.8.8")

    with pytest.raises(HTTPException) as exc_info:
        await auth.login(LoginRequest(password=""), _make_request())

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_passwordless_unset_bind_host_fails_closed(monkeypatch, tmp_path):
    """绑定地址未知（未注入）时按非回环处理，fail-closed。"""
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(auth, "_WEBUI_BIND_HOST", "")
    monkeypatch.setattr(auth, "_get_client_ip", lambda request: "127.0.0.1")

    with pytest.raises(HTTPException) as exc_info:
        await auth.login(LoginRequest(password=""), _make_request())

    assert exc_info.value.status_code == 403
