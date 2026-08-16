"""web/routers/system.py — set_permission_mode GOAT 密码二次确认测试。

「登录即主人」模型下，持有有效 token 的任何人都是主人，而 GOAT 模式会持久化
到磁盘（重启后依然全权）。配置了 WEBUI_PASSWORD 时切换到 goat 必须用登录密码
二次确认（hmac 恒时比较），否则 403；未配置密码（本机免密模式）保持原有
confirm:"yes" 机制不变；切回 default/strict 等降权模式不需要密码。
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


def test_goat_without_password_field_403_when_webui_password_set(with_password, client):
    """有密码环境：切 goat 缺 password 字段 → 403，模式不变。"""
    before = get_permission_manager().mode
    r = _put(client, {"mode": "goat", "confirm": "yes"})
    assert r.status_code == 403
    assert get_permission_manager().mode == before
    assert client.app.state.core.db.insert_audit_log.await_count == 0


def test_goat_wrong_password_403(with_password, client):
    """有密码环境：错误密码 → 403，模式不变。"""
    before = get_permission_manager().mode
    r = _put(client, {"mode": "goat", "confirm": "yes", "password": "wrong-password"})
    assert r.status_code == 403
    assert get_permission_manager().mode == before
    assert client.app.state.core.db.insert_audit_log.await_count == 0


def test_goat_correct_password_200_and_mode_takes_effect(with_password, client):
    """有密码环境：正确密码 → 200 且模式生效，审计日志保持。"""
    r = _put(client, {"mode": "goat", "confirm": "yes", "password": PASSWORD})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["mode"] == "goat"
    assert get_permission_manager().mode == PermissionMode.GOAT
    # 保持现有审计行为
    client.app.state.core.db.insert_audit_log.assert_awaited()
    assert client.app.state.core.db.insert_audit_log.await_args.args[0] == "webui.permission_mode.set"


def test_goat_still_requires_confirm_even_with_correct_password(with_password, client):
    """正确密码不能替代 confirm:"yes" 的二次确认。"""
    before = get_permission_manager().mode
    r = _put(client, {"mode": "goat", "password": PASSWORD})
    assert r.status_code == 400
    assert get_permission_manager().mode == before


def test_goat_confirm_only_200_when_no_webui_password(without_password, client):
    """无密码环境（本机免密模式）：保持原有 confirm:"yes" 机制。"""
    r = _put(client, {"mode": "goat", "confirm": "yes"})
    assert r.status_code == 200, r.text
    assert get_permission_manager().mode == PermissionMode.GOAT


def test_switch_to_default_needs_no_password(with_password, client):
    """降权切回 default 不需要密码/confirm。"""
    get_permission_manager().set_mode(PermissionMode.STRICT)
    r = _put(client, {"mode": "default"})
    assert r.status_code == 200, r.text
    assert get_permission_manager().mode == PermissionMode.DEFAULT


def test_unknown_mode_still_400(with_password, client):
    """未知模式依旧 400（回归保护）。"""
    r = _put(client, {"mode": "supergoat", "confirm": "yes", "password": PASSWORD})
    assert r.status_code == 400
