"""POST /auth/recover 与 GET /auth/recover-question 端点测试。

最小 FastAPI app 挂载 auth router（参照 test_auth_revoke_all.py /
test_metrics_auth.py 的构造方式）。.env 与恢复文件均重定向到 tmp_path，
绝不触碰真实 .env / credentials/。
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

    # 快照并恢复 WEBUI_PASSWORD（recover 成功会直接写 os.environ）
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


# ── GET /auth/recover-question（无鉴权）─────────────────────

def test_recover_path_in_login_rate_limit_bucket():
    """/api/v1/auth/recover 必须纳入中间件 login 独立严格桶（答案爆破防护）。"""
    from web.middleware.rate_limit import _LOGIN_PATHS
    assert "/api/v1/auth/recover" in _LOGIN_PATHS


def test_get_question_unauthenticated_no_question(client):
    r = client.get("/auth/recover-question")
    assert r.status_code == 200
    assert r.json()["data"] == {"question": "", "has_question": False}


def test_get_question_unauthenticated_with_question(client):
    rqa.set_recovery("我的第一只宠物叫什么？", "miaomiao")
    r = client.get("/auth/recover-question")
    assert r.status_code == 200
    assert r.json()["data"] == {"question": "我的第一只宠物叫什么？", "has_question": True}


# ── POST /auth/recover（无鉴权）────────────────────────────

def test_recover_no_question_400(client):
    r = client.post("/auth/recover", json={"answer": "x", "new_password": "newpass123"})
    assert r.status_code == 400
    assert "找回" in r.json()["detail"]


def test_recover_weak_new_password_400(client):
    rqa.set_recovery("我的第一只宠物叫什么？", "miaomiao")
    r = client.post("/auth/recover", json={"answer": "miaomiao", "new_password": "short"})
    assert r.status_code == 400


def test_recover_wrong_answer_403_then_lock(client):
    rqa.set_recovery("我的第一只宠物叫什么？", "miaomiao")
    for _ in range(5):
        r = client.post("/auth/recover", json={"answer": "wrong", "new_password": "newpass123"})
        assert r.status_code == 403, r.text
    # 第 6 次触发 5 次/600s 锁定
    r = client.post("/auth/recover", json={"answer": "wrong", "new_password": "newpass123"})
    assert r.status_code == 429, r.text


def test_recover_success_updates_env_and_revokes_tokens(client, tmp_path):
    rqa.set_recovery("我的第一只宠物叫什么？", "miaomiao")
    old_token, _ = auth._issue_token()
    assert auth._validate_token(old_token)

    r = client.post("/auth/recover", json={"answer": "miaomiao", "new_password": "newpass123"})
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"ok": True}

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "WEBUI_PASSWORD=newpass123" in env_text
    # 运行时环境同步更新（后续登录立即生效）
    assert os.environ["WEBUI_PASSWORD"] == "newpass123"
    # epoch 递增：旧 token 全部失效
    assert not auth._validate_token(old_token)

    # 成功重置该 IP 的失败计数：再次错答案应回到 403 而非 429
    r2 = client.post("/auth/recover", json={"answer": "wrong", "new_password": "newpass456"})
    assert r2.status_code == 403, r2.text
