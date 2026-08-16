"""P2：goat 模式持久化提权风险修复测试。

背景：/system/permission-mode PUT 传 mode=goat + confirm=yes 即可把权限切到
"梭哈"（跳过所有安全检查），且 permission_manager.set_mode 会把 goat 落盘——
重启后依然是 goat，一次误操作/一次被盗 session 即长期全局关安全。

修复：
1. goat/bypass 不落盘持久化（重启回到默认 DEFAULT 档）
2. 加载持久化文件时若发现 goat/bypass，降级为 DEFAULT 并告警（历史残留清理）
3. WebUI API 切换到 goat 时，设置了 WEBUI_PASSWORD 的情况下必须回传正确密码
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from security.permission_manager import (
    PermissionManager, PermissionMode, _load_persisted_mode, _persist_mode,
)


@pytest.fixture(autouse=True)
def _tmp_permission_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_PERMISSION_FILE", str(tmp_path / "permission_mode.json"))
    yield


def test_goat_mode_not_persisted():
    pm = PermissionManager()
    pm.set_mode("goat")
    assert pm.mode == PermissionMode.GOAT
    # 落盘检查：文件不存在或内容不是 goat
    loaded = _load_persisted_mode()
    assert loaded != PermissionMode.GOAT


def test_bypass_mode_not_persisted():
    pm = PermissionManager()
    pm.set_mode("bypass")
    loaded = _load_persisted_mode()
    assert loaded != PermissionMode.BYPASS


def test_default_mode_still_persisted():
    pm = PermissionManager()
    pm.set_mode("strict")
    assert _load_persisted_mode() == PermissionMode.STRICT


def test_stale_goat_persisted_file_downgraded(tmp_path, monkeypatch):
    """历史残留的 goat 持久化文件在加载时降级为 DEFAULT"""
    import json
    pfile = tmp_path / "permission_mode.json"
    pfile.write_text(json.dumps({"mode": "goat"}), encoding="utf-8")
    monkeypatch.setenv("AGENT_PERMISSION_FILE", str(pfile))
    monkeypatch.delenv("AGENT_PERMISSION_MODE", raising=False)
    monkeypatch.delenv("AGENT_DEV_MODE", raising=False)
    loaded = _load_persisted_mode()
    assert loaded is None or loaded == PermissionMode.DEFAULT


# ── API 层：goat 需要密码确认 ──────────────────────────────────────

def _make_app():
    from unittest.mock import AsyncMock, MagicMock
    from web.routers.system import router as system_router

    app = FastAPI()
    app.include_router(system_router, prefix="/api/v1")
    # 审计日志桩（端点会写 request.app.state.core.db）
    core = MagicMock()
    core.db.insert_audit_log = AsyncMock()
    core.db.commit = AsyncMock()
    app.state.core = core
    return app


def test_goat_requires_password_when_set(monkeypatch):
    monkeypatch.setenv("WEBUI_PASSWORD", "correct-horse-battery")
    app = _make_app()
    client = TestClient(app)
    token, _ = __import__("web.routers.auth", fromlist=["_issue_token"])._issue_token()
    headers = {"Authorization": f"Bearer {token}"}

    # 缺密码 → 403（密码二次确认失败）
    r = client.put("/api/v1/system/permission-mode",
                   json={"mode": "goat", "confirm": "yes"}, headers=headers)
    assert r.status_code == 403
    # 错误密码 → 403
    r = client.put("/api/v1/system/permission-mode",
                   json={"mode": "goat", "confirm": "yes", "password": "wrong"},
                   headers=headers)
    assert r.status_code == 403
    # 正确密码 → 200
    r = client.put("/api/v1/system/permission-mode",
                   json={"mode": "goat", "confirm": "yes",
                         "password": "correct-horse-battery"},
                   headers=headers)
    assert r.status_code == 200


def test_goat_password_not_required_when_no_password_set(monkeypatch):
    """未设置 WEBUI_PASSWORD（本地回环免密模式）时保持原 confirm 二次确认"""
    monkeypatch.delenv("WEBUI_PASSWORD", raising=False)
    app = _make_app()
    client = TestClient(app)
    token, _ = __import__("web.routers.auth", fromlist=["_issue_token"])._issue_token()
    headers = {"Authorization": f"Bearer {token}"}
    r = client.put("/api/v1/system/permission-mode",
                   json={"mode": "goat", "confirm": "yes"}, headers=headers)
    assert r.status_code == 200
