"""web/routers/system.py — set_permission_mode 权限切换测试（登录即主人）。

信任模型：持有有效 token 的人就是主人，切 goat 只需 confirm:"yes" 防误触头，
不需要额外的密码二次确认（goat/bypass 已不落盘持久化，重启自动回安全档位）。
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from security.permission_manager import PermissionMode, get_permission_manager
from web.routers.auth import _issue_token
from web.routers.system import router as system_router

PASSWORD = "correct-horse-battery"


class FakeDb:
    insert_audit_log = AsyncMock()
    commit = AsyncMock()


@pytest.fixture
def app():
    a = FastAPI()
    a.include_router(system_router)
    a.state.core = SimpleNamespace(db=FakeDb())
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


def _auth_headers() -> dict[str, str]:
    token, _ = _issue_token()
    return {"Authorization": f"Bearer {token}"}


def _put(client: TestClient, body: dict):
    return client.put("/system/permission-mode", json=body, headers=_auth_headers())


@pytest.fixture
def with_password(monkeypatch):
    monkeypatch.setenv("WEBUI_PASSWORD", PASSWORD)


@pytest.fixture
def without_password(monkeypatch):
    monkeypatch.delenv("WEBUI_PASSWORD", raising=False)


def test_goat_with_confirm_succeeds_even_with_password_set(with_password, client):
    """登录即主人：设置了 WEBUI_PASSWORD 时，confirm:"yes" 即可切 goat（无需密码）。"""
    r = _put(client, {"mode": "goat", "confirm": "yes"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["mode"] == "goat"
    assert get_permission_manager().mode == PermissionMode.GOAT
    client.app.state.core.db.insert_audit_log.assert_awaited()
    assert client.app.state.core.db.insert_audit_log.await_args.args[0] == "webui.permission_mode.set"


def test_goat_without_password_env_still_confirm_only(without_password, client):
    """无密码环境（本机免密模式）行为一致：confirm:"yes" 即可。"""
    r = _put(client, {"mode": "goat", "confirm": "yes"})
    assert r.status_code == 200, r.text
    assert get_permission_manager().mode == PermissionMode.GOAT


def test_goat_without_confirm_still_400(with_password, client):
    """缺少 confirm:"yes" 防误触头依旧 400，模式不变。"""
    before = get_permission_manager().mode
    r = _put(client, {"mode": "goat"})
    assert r.status_code == 400
    assert get_permission_manager().mode == before


def test_switch_to_default_needs_no_confirm(with_password, client):
    """降权切回 default 不需要 confirm。"""
    get_permission_manager().set_mode(PermissionMode.STRICT)
    r = _put(client, {"mode": "default"})
    assert r.status_code == 200, r.text
    assert get_permission_manager().mode == PermissionMode.DEFAULT


def test_unknown_mode_still_400(with_password, client):
    """未知模式依旧 400（回归保护）。"""
    r = _put(client, {"mode": "supergoat", "confirm": "yes"})
    assert r.status_code == 400
