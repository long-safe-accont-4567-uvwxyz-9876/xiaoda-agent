"""POST /setup/keys 首跑引导令牌（bootstrap token）集成测试。

audit-fix-20260829 Task 1：首跑 + 私网非回环必须携带一次性引导令牌
（X-Setup-Token 头或 body 的 setup_token 字段），回环保留免令牌首跑体验。
CONFIG_DIR / 密钥文件 / .env / 恢复文件全部隔离到 tmp_path，
绝不触碰真实 ~/.ai-agent 与仓库 .env。
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import security.recovery_qa as rqa
from web.routers import setup as setup_router_mod

_LOOPBACK = "127.0.0.1"
_LAN_HOST = "192.168.1.50"  # 私网非回环
_PUBLIC_HOST = "8.8.8.8"


@pytest.fixture
def client_factory(tmp_path, monkeypatch):
    """隔离首跑判定与令牌密钥文件到 tmp_path，并打桩 save_keys 的重型副作用。"""
    import setup_wizard as sw

    monkeypatch.setattr(sw, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(sw, "ENV_EXAMPLE_PATH", str(tmp_path / "nonexistent.env.example"))
    monkeypatch.setattr(rqa, "_get_path", lambda: tmp_path / "webui_recovery.json")
    # setup.py 模块级绑定的 CONFIG_DIR 重定向到 tmp_path（引导令牌密钥落这里）
    monkeypatch.setattr(setup_router_mod, "CONFIG_DIR", tmp_path)
    # 快照并恢复 WEBUI_PASSWORD（真实环境若已设置，避免泄漏进断言）
    monkeypatch.delenv("WEBUI_PASSWORD", raising=False)

    # 隔离 save_keys 的重型副作用
    monkeypatch.setattr(setup_router_mod, "_reset_credential_pool", lambda updates: None)
    monkeypatch.setattr(setup_router_mod, "_update_config_and_refresh_clients", lambda updates: None)

    async def _noop(*args, **kwargs):
        return None
    monkeypatch.setattr(setup_router_mod, "_reinit_and_maybe_restart_qq", _noop)
    monkeypatch.setattr(setup_router_mod, "_reload_env_and_cache", _noop)

    def _make(host: str) -> TestClient:
        app = FastAPI()
        # _auto_register_providers 需要 app.state.provider_service
        app.state.provider_service = SimpleNamespace(
            list=lambda: [],
            catalog=SimpleNamespace(get=lambda pid: None),
        )
        import web.app_ref as app_ref
        monkeypatch.setattr(app_ref, "_app", app)
        app.include_router(setup_router_mod.router)
        return TestClient(app, client=(host, 50000))

    return _make


def _body(keys: dict | None = None, **extra) -> dict:
    return {"keys": keys or {"TAVILY_API_KEY": "x"}, **extra}


def _read_secret(tmp_path) -> str:
    return (tmp_path / "setup_bootstrap_secret").read_text(encoding="utf-8").strip()


# ── 回环：保留免令牌首跑体验 ──

def test_loopback_first_run_saves_without_token(client_factory, tmp_path):
    client = client_factory(_LOOPBACK)
    r = client.post("/setup/keys", json=_body())
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    # 回环路径不应触发令牌生成
    assert not (tmp_path / "setup_bootstrap_secret").exists()


# ── 私网非回环：必须携带引导令牌 ──

def test_private_nonloopback_without_token_403(client_factory, tmp_path):
    client = client_factory(_LAN_HOST)
    r = client.post("/setup/keys", json=_body())
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "SETUP_TOKEN_REQUIRED"
    # 无令牌请求即触发密钥生成，供本机用户读取后重试
    secret_path = tmp_path / "setup_bootstrap_secret"
    assert secret_path.exists()
    assert _read_secret(tmp_path)
    if os.name == "posix":
        assert secret_path.stat().st_mode & 0o777 == 0o600
    # .env 不得被写入（fail-closed：拒绝即无副作用）
    assert not (tmp_path / ".env").exists()


def test_private_nonloopback_header_token_ok(client_factory, tmp_path):
    client = client_factory(_LAN_HOST)
    first = client.post("/setup/keys", json=_body())
    assert first.status_code == 403, first.text
    token = _read_secret(tmp_path)
    r = client.post("/setup/keys", json=_body(), headers={"X-Setup-Token": token})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_private_nonloopback_body_token_ok(client_factory, tmp_path):
    client = client_factory(_LAN_HOST)
    first = client.post("/setup/keys", json=_body())
    assert first.status_code == 403, first.text
    token = _read_secret(tmp_path)
    r = client.post("/setup/keys", json=_body(setup_token=token))
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_private_nonloopback_wrong_token_rejected(client_factory):
    client = client_factory(_LAN_HOST)
    r_header = client.post("/setup/keys", json=_body(), headers={"X-Setup-Token": "wrong-token"})
    assert r_header.status_code == 403, r_header.text
    assert r_header.json()["detail"]["code"] == "SETUP_TOKEN_REQUIRED"
    r_body = client.post("/setup/keys", json=_body(setup_token="wrong-token"))
    assert r_body.status_code == 403, r_body.text


def test_public_source_still_403(client_factory):
    client = client_factory(_PUBLIC_HOST)
    r = client.post("/setup/keys", json=_body())
    assert r.status_code == 403, r.text


# ── 找回答案强度：最小 6 字符（常量与 recovery_qa 同源防漂移） ──

def test_min_answer_len_constant_shared():
    assert setup_router_mod.MIN_ANSWER_LEN == 6
    assert setup_router_mod.MIN_ANSWER_LEN == rqa.MIN_ANSWER_LEN


def test_recovery_answer_below_6_rejected(client_factory):
    client = client_factory(_LOOPBACK)
    r = client.post("/setup/keys", json=_body(
        webui_password="newpass123",
        recovery_question="我的第一只宠物叫什么？",
        recovery_answer="abc12",  # 5 字符
    ))
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "RECOVERY_INVALID"


def test_recovery_answer_exactly_6_accepted(client_factory, tmp_path):
    client = client_factory(_LOOPBACK)
    r = client.post("/setup/keys", json=_body(
        webui_password="newpass123",
        recovery_question="我的第一只宠物叫什么？",
        recovery_answer="abc123",  # 恰好 6 字符
    ))
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert rqa.verify_answer("abc123") is True
    assert rqa.verify_answer("abc12") is False
