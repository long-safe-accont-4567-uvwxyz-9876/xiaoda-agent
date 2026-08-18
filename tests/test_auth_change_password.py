"""POST /auth/change-password 端点测试。

最小 FastAPI app 挂载 auth router；.env / 恢复文件 / epoch 全部重定向到
tmp_path，绝不触碰真实 .env / credentials/。
"""
from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import security.recovery_qa as rqa
from web.routers import auth


@pytest.fixture
def client(tmp_path, monkeypatch):
    """最小 app + 全部落盘路径隔离。"""
    monkeypatch.setattr(auth, "_SECRET", "test-secret")
    monkeypatch.setattr(auth, "_get_revoked_path", lambda: tmp_path / "revoked.json")
    monkeypatch.setattr(auth, "_get_token_epoch_path", lambda: tmp_path / "epoch")
    monkeypatch.setattr(auth, "_token_epoch", None)
    monkeypatch.setattr(rqa, "_get_path", lambda: tmp_path / "webui_recovery.json")

    import setup_wizard as sw
    monkeypatch.setattr(sw, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(sw, "ENV_EXAMPLE_PATH", str(tmp_path / "nonexistent.env.example"))

    # 快照并恢复 WEBUI_PASSWORD（change-password 成功会直接写 os.environ）
    monkeypatch.delenv("WEBUI_PASSWORD", raising=False)

    auth._tokens.clear()
    auth._revoked_cache.clear()
    auth._revoked_cache_mtime = 0.0
    auth._rate_limit.clear()
    grace = getattr(auth, "_revoked_grace", None)
    if grace is not None:
        grace.clear()

    app = FastAPI()
    app.include_router(auth.router)
    return TestClient(app, client=("203.0.113.9", 50000))


def _token_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_change_password_requires_token(client, monkeypatch):
    monkeypatch.setenv("WEBUI_PASSWORD", "oldpass123")
    r = client.post("/auth/change-password", json={
        "old_password": "oldpass123", "new_password": "newpass123", "answer": "x",
    })
    assert r.status_code == 401


def test_wrong_old_password_403(client, monkeypatch):
    monkeypatch.setenv("WEBUI_PASSWORD", "oldpass123")
    rqa.set_recovery("我的第一只宠物叫什么？", "miaomiao")
    token, _ = auth._issue_token()
    r = client.post("/auth/change-password", headers=_token_headers(token), json={
        "old_password": "wrong-old", "new_password": "newpass123", "answer": "miaomiao",
    })
    assert r.status_code == 403


def test_wrong_answer_403(client, monkeypatch):
    monkeypatch.setenv("WEBUI_PASSWORD", "oldpass123")
    rqa.set_recovery("我的第一只宠物叫什么？", "miaomiao")
    token, _ = auth._issue_token()
    r = client.post("/auth/change-password", headers=_token_headers(token), json={
        "old_password": "oldpass123", "new_password": "newpass123", "answer": "wrong-answer",
    })
    assert r.status_code == 403


def test_weak_new_password_400(client, monkeypatch):
    monkeypatch.setenv("WEBUI_PASSWORD", "oldpass123")
    rqa.set_recovery("我的第一只宠物叫什么？", "miaomiao")
    token, _ = auth._issue_token()
    r = client.post("/auth/change-password", headers=_token_headers(token), json={
        "old_password": "oldpass123", "new_password": "short", "answer": "miaomiao",
    })
    assert r.status_code == 400


def test_same_password_400(client, monkeypatch):
    monkeypatch.setenv("WEBUI_PASSWORD", "oldpass123")
    rqa.set_recovery("我的第一只宠物叫什么？", "miaomiao")
    token, _ = auth._issue_token()
    r = client.post("/auth/change-password", headers=_token_headers(token), json={
        "old_password": "oldpass123", "new_password": "oldpass123", "answer": "miaomiao",
    })
    assert r.status_code == 400


def test_change_password_success(client, tmp_path, monkeypatch):
    monkeypatch.setenv("WEBUI_PASSWORD", "oldpass123")
    rqa.set_recovery("我的第一只宠物叫什么？", "miaomiao")
    old_token, _ = auth._issue_token()
    assert auth._validate_token(old_token)

    r = client.post("/auth/change-password", headers=_token_headers(old_token), json={
        "old_password": "oldpass123", "new_password": "newpass123", "answer": "miaomiao",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    new_token = body["data"]["token"]
    assert new_token
    assert body["data"]["expires_at"] > 0

    # 新 token 可用，旧 token 失效（epoch 递增）
    assert auth._validate_token(new_token)
    assert not auth._validate_token(old_token)

    # .env 与运行时环境均已更新
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "WEBUI_PASSWORD=newpass123" in env_text
    assert os.environ["WEBUI_PASSWORD"] == "newpass123"

    # 新密码可登录
    r2 = client.post("/auth/login", json={"password": "newpass123"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["data"]["token"]


def test_change_password_rotates_recovery(client, monkeypatch):
    monkeypatch.setenv("WEBUI_PASSWORD", "oldpass123")
    rqa.set_recovery("旧问题", "miaomiao")
    token, _ = auth._issue_token()

    r = client.post("/auth/change-password", headers=_token_headers(token), json={
        "old_password": "oldpass123", "new_password": "newpass123", "answer": "miaomiao",
        "new_question": "新问题", "new_answer": "new-answer",
    })
    assert r.status_code == 200, r.text

    assert rqa.get_question() == "新问题"
    assert rqa.verify_answer("new-answer") is True
    assert rqa.verify_answer("miaomiao") is False


def test_change_password_without_old_password_when_unset(client):
    """当前 WEBUI_PASSWORD 为空时 old_password 可为空串。"""
    rqa.set_recovery("我的第一只宠物叫什么？", "miaomiao")
    token, _ = auth._issue_token()

    r = client.post("/auth/change-password", headers=_token_headers(token), json={
        "old_password": "", "new_password": "newpass123", "answer": "miaomiao",
    })
    assert r.status_code == 200, r.text
    assert auth._validate_token(r.json()["data"]["token"])


def test_env_update_failure_does_not_rotate_recovery(client, monkeypatch):
    """.env 写入失败时不轮换问答（顺序约束：密码先成功、问答后轮换）。"""
    monkeypatch.setenv("WEBUI_PASSWORD", "oldpass123")
    rqa.set_recovery("旧问题", "miaomiao")
    token, _ = auth._issue_token()

    def _boom(new_password: str) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(auth, "_update_env_password", _boom)

    r = client.post("/auth/change-password", headers=_token_headers(token), json={
        "old_password": "oldpass123", "new_password": "newpass123", "answer": "miaomiao",
        "new_question": "新问题", "new_answer": "new-answer",
    })
    assert r.status_code == 500
    # 问答未被轮换，旧答案仍有效
    assert rqa.get_question() == "旧问题"
    assert rqa.verify_answer("miaomiao") is True
