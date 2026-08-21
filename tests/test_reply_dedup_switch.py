"""跨对话回复去重 WebUI 开关测试。

覆盖：
- config.get_reply_dedup_enabled：默认开启、override False/True 回读
- web/routers/models.py 的 GET/PUT /models/reply_dedup 校验与持久化
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from web.routers import models as models_router


@pytest.fixture
def mock_cfg(monkeypatch, tmp_path):
    overrides_file = tmp_path / "webui_overrides.json"
    overrides_file.write_text('{"models": {}}', encoding="utf-8")

    from web.config_service import ConfigService
    real_cfg = ConfigService(path=overrides_file)

    import web.config_service
    monkeypatch.setattr(web.config_service, "get_config_service", lambda: real_cfg)
    return real_cfg


def test_reply_dedup_default_enabled(mock_cfg):
    """未配置时默认开启。"""
    from config import get_reply_dedup_enabled
    assert get_reply_dedup_enabled() is True
    assert get_reply_dedup_enabled(default=False) is False  # default 参数可覆盖


def test_reply_dedup_override_off_on(mock_cfg):
    """写入 off/on 后按持久化值返回。"""
    from config import get_reply_dedup_enabled
    from web.config_service import get_config_service

    get_config_service().set("models.reply_dedup_enabled", False)
    assert get_reply_dedup_enabled() is False

    get_config_service().set("models.reply_dedup_enabled", True)
    assert get_reply_dedup_enabled() is True


def test_reply_dedup_read_no_write(mock_cfg):
    """读取不写 webui_overrides.json（与 temperature 同语义）。"""
    from config import get_reply_dedup_enabled

    get_reply_dedup_enabled()

    raw = json.loads(mock_cfg._path.read_text(encoding="utf-8"))
    assert "reply_dedup_enabled" not in raw.get("models", {})


def _make_client(mock_cfg) -> TestClient:
    """最小 FastAPI 应用：仅挂载 models 路由，跳过认证依赖。"""
    from unittest.mock import AsyncMock

    app = FastAPI()
    import web.routers.auth as auth_mod

    async def _fake_auth(request: Request) -> str:
        return "test-user"

    # _audit 需要 app.state.core.db（审计日志），用 AsyncMock 提供
    app.state.core = AsyncMock()
    app.state.core.db = AsyncMock()

    app.dependency_overrides[auth_mod.get_current_user] = _fake_auth
    app.include_router(models_router.router, prefix="/api/v1")
    return TestClient(app)


def test_router_get_put_reply_dedup(mock_cfg):
    with _make_client(mock_cfg) as client:
        r = client.get("/api/v1/models/reply_dedup")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["enabled"] is True
        assert data["source"] == "default"

        r = client.put("/api/v1/models/reply_dedup", json={"enabled": False})
        assert r.status_code == 200
        assert r.json()["data"]["enabled"] is False

        r = client.get("/api/v1/models/reply_dedup")
        assert r.status_code == 200
        assert r.json()["data"]["enabled"] is False
        assert r.json()["data"]["source"] == "override"

        # 非布尔值拒绝
        r = client.put("/api/v1/models/reply_dedup", json={"enabled": "yes"})
        assert r.status_code == 400