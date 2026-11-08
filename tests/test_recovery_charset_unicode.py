"""找回问题 / 登录密码的 Unicode（中文）支持回归测试。

覆盖四件事：
1. 前后端共用常量一致，且长度按字符数（不是字节数）计算；
2. ``hmac.compare_digest`` 包装后能比较含中文的字符串；
3. 找回问答可以存中文、可以校验中文答案；
4. 中文新密码经 /auth/recover 写入后，能用中文密码登录。

所有用例都在临时凭证目录下跑，不读写真实 .env 与真实凭证目录。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from security import credential_text, recovery_qa
from web.routers import auth as auth_router

CHINESE_QUESTION = "我小学叫什么名字"
CHINESE_ANSWER = "我的第一辆车是蓝色的"
CHINESE_PASSWORD = "中文密码A1！安全"
INITIAL_PASSWORD = "initial-pass-1"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import config
    import setup_wizard

    cred_dir = tmp_path / "credentials"
    cred_dir.mkdir()
    monkeypatch.setattr(config, "get_credentials_dir", lambda: cred_dir)
    env_path = tmp_path / ".env"
    env_path.write_text(f"WEBUI_PASSWORD={INITIAL_PASSWORD}\n", encoding="utf-8")
    monkeypatch.setattr(setup_wizard, "ENV_PATH", str(env_path))
    monkeypatch.setenv("WEBUI_PASSWORD", INITIAL_PASSWORD)
    monkeypatch.setattr(auth_router, "_rate_limit", auth_router.OrderedDict())
    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/v1")
    return TestClient(app)


def test_shared_constants_are_char_based():
    assert credential_text.MIN_ANSWER_LEN == 6
    assert credential_text.MIN_PASSWORD_LEN == 8
    assert recovery_qa.MIN_ANSWER_LEN == credential_text.MIN_ANSWER_LEN
    # 6 个中文字符刚好达到最小长度：证明按字符而不是按字节
    assert credential_text.validate_answer("六个中文字符") == "六个中文字符"
    with pytest.raises(ValueError):
        credential_text.validate_answer("五个中文字")


def test_frontend_constants_match_backend():
    ts = Path("web/frontend/src/utils/credentialInput.ts").read_text(encoding="utf-8")
    assert f"MIN_ANSWER_LEN = {credential_text.MIN_ANSWER_LEN}" in ts
    assert f"MIN_PASSWORD_LEN = {credential_text.MIN_PASSWORD_LEN}" in ts
    assert f"MAX_QUESTION_LEN = {credential_text.MAX_QUESTION_LEN}" in ts


def test_constant_time_equal_accepts_chinese():
    assert credential_text.constant_time_equal("中文密码", "中文密码")
    assert not credential_text.constant_time_equal("中文密码", "中文密码2")


def test_set_and_verify_chinese_answer(client):
    recovery_qa.set_recovery(CHINESE_QUESTION, CHINESE_ANSWER)
    assert recovery_qa.get_question() == CHINESE_QUESTION
    assert recovery_qa.verify_answer(CHINESE_ANSWER) is True
    assert recovery_qa.verify_answer(CHINESE_ANSWER + "错") is False


def test_recover_and_login_with_chinese_password(client):
    recovery_qa.set_recovery(CHINESE_QUESTION, CHINESE_ANSWER)

    resp = client.post(
        "/api/v1/auth/recover",
        json={"answer": CHINESE_ANSWER, "new_password": CHINESE_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    assert os.environ["WEBUI_PASSWORD"] == CHINESE_PASSWORD

    ok = client.post("/api/v1/auth/login", json={"password": CHINESE_PASSWORD})
    assert ok.status_code == 200, ok.text

    bad = client.post("/api/v1/auth/login", json={"password": "别的密码123"})
    assert bad.status_code == 401
