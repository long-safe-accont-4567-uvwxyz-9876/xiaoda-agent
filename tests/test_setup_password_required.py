"""POST /setup/keys 密码必填 + 找回问答集成测试。

最小 FastAPI app 挂载 setup router（首次运行免认证路径，客户端回环来源）。
.env / 恢复文件全部重定向到 tmp_path；save_keys 的重型副作用
（凭证池重置 / config 更新 / 后台核心重初始化）全部打桩隔离，
绝不触碰真实 .env 与 credentials/。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import security.recovery_qa as rqa
from web.routers import setup as setup_router_mod


@pytest.fixture
def client(tmp_path, monkeypatch):
    import setup_wizard as sw
    monkeypatch.setattr(sw, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(sw, "ENV_EXAMPLE_PATH", str(tmp_path / "nonexistent.env.example"))
    monkeypatch.setattr(rqa, "_get_path", lambda: tmp_path / "webui_recovery.json")

    # 快照并恢复 WEBUI_PASSWORD（save_keys 会 load_dotenv 写 os.environ）
    monkeypatch.delenv("WEBUI_PASSWORD", raising=False)

    # 隔离 save_keys 的重型副作用
    monkeypatch.setattr(setup_router_mod, "_reset_credential_pool", lambda updates: None)
    monkeypatch.setattr(setup_router_mod, "_update_config_and_refresh_clients", lambda updates: None)

    async def _noop_reinit(qq_changed: bool) -> None:
        return None
    monkeypatch.setattr(setup_router_mod, "_reinit_and_maybe_restart_qq", _noop_reinit)

    app = FastAPI()
    # _auto_register_providers 需要 app.state.provider_service
    app.state.provider_service = SimpleNamespace(
        list=lambda: [],
        catalog=SimpleNamespace(get=lambda pid: None),
    )
    import web.app_ref as app_ref
    monkeypatch.setattr(app_ref, "_app", app)
    app.include_router(setup_router_mod.router)
    return TestClient(app, client=("127.0.0.1", 50000))


def _body(keys: dict, **extra) -> dict:
    return {"keys": keys, "test_required": True, **extra}


def test_weak_password_400(client):
    r = client.post("/setup/keys", json=_body(
        {"TAVILY_API_KEY": "x"},
        webui_password="short",
        recovery_question="q",
        recovery_answer="a1",
    ))
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "WEAK_PASSWORD"


def test_password_without_recovery_400(client):
    r = client.post("/setup/keys", json=_body(
        {"TAVILY_API_KEY": "x"},
        webui_password="newpass123",
    ))
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "RECOVERY_REQUIRED"


def test_password_with_only_question_400(client):
    r = client.post("/setup/keys", json=_body(
        {"TAVILY_API_KEY": "x"},
        webui_password="newpass123",
        recovery_question="q",
    ))
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "RECOVERY_REQUIRED"


def test_full_flow_writes_env_and_recovery(client, tmp_path):
    r = client.post("/setup/keys", json=_body(
        {"TAVILY_API_KEY": "x"},
        webui_password="newpass123",
        recovery_question="我的第一只宠物叫什么？",
        recovery_answer="miaomiao",
    ))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "WEBUI_PASSWORD" in body["data"]["saved"]

    # .env 被更新（含密码），行内容正确
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "WEBUI_PASSWORD=newpass123" in env_text

    # 恢复文件存在且内容合规
    recovery_path = tmp_path / "webui_recovery.json"
    assert recovery_path.exists()
    data = json.loads(recovery_path.read_text(encoding="utf-8"))
    assert data["question"] == "我的第一只宠物叫什么？"
    assert "miaomiao" not in recovery_path.read_text(encoding="utf-8")
    assert rqa.get_question() == "我的第一只宠物叫什么？"
    assert rqa.verify_answer("miaomiao") is True


def test_no_webui_password_ignores_recovery_fields(client, tmp_path):
    """未提供 webui_password 时忽略三个新字段（不写恢复文件）。"""
    r = client.post("/setup/keys", json=_body(
        {"TAVILY_API_KEY": "x"},
        recovery_question="q",
        recovery_answer="a1",
    ))
    assert r.status_code == 200, r.text
    assert not (tmp_path / "webui_recovery.json").exists()
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "WEBUI_PASSWORD=" not in env_text


def test_existing_recovery_kept_when_no_new_answer(client, tmp_path):
    """已有找回问题且本次未提供新 answer 时不要清掉旧问题。"""
    rqa.set_recovery("旧问题", "old-answer")
    r = client.post("/setup/keys", json=_body({"TAVILY_API_KEY": "x"}))
    assert r.status_code == 200, r.text
    assert rqa.get_question() == "旧问题"
    assert rqa.verify_answer("old-answer") is True
