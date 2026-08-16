"""主人邮箱权限分级测试。

需求：邮箱设置中指定一个特定邮箱为主人邮箱。邮件功能本身始终可用——
未设置主人邮箱或发件人不是主人邮箱时，邮件按访客身份处理（VULN-27 非主人
只读工具白名单）；设置且匹配时按主人身份处理（完整工具权限）。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ── 构造件 ──────────────────────────────────────────────

class FakeCfg:
    def __init__(self, **values):
        self.data = dict(values)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class FakeSecurity:
    def __init__(self):
        self.owner_ids = set()


class FakeCore:
    def __init__(self):
        self.security = FakeSecurity()


def _make_poller(cfg_values: dict) -> "object":
    from web.mail_poller import MailPoller
    return MailPoller(core=FakeCore(), config_service=FakeCfg(**cfg_values))


def _msg(msg_id: str, from_email: str) -> dict:
    return {
        "message_id": msg_id,
        "from": {"email": from_email, "name": "Sender"},
        "subject": "hi",
        "snippet": "hello",
    }


# ── 1. 未设置主人邮箱：邮件照常处理，只是不注册主人身份 ──

@pytest.mark.asyncio
async def test_no_owner_email_still_processes_as_guest(monkeypatch):
    """未配置主人邮箱时邮件功能照常运行，但不注册任何主人身份（访客权限）。"""
    poller = _make_poller({"mail.owner_email": ""})
    processed = []

    async def _fake_run(args, timeout):
        return (0, "{}", "")

    async def _fake_process(msg_id, *a, **k):
        processed.append(msg_id)

    monkeypatch.setattr("tools.mail_tools._run_agently", _fake_run)
    monkeypatch.setattr(poller, "_process_one_email", _fake_process)
    monkeypatch.setattr(
        "web.mail_poller._extract_messages",
        lambda out: [_msg("m1", "anyone@x.com")],
    )

    await poller._poll_inbox("all")
    assert processed == ["m1"]
    # 未注册任何主人身份
    assert poller.core.security.owner_ids == set()


# ── 2. 设置主人邮箱：非主人发件人照常处理（访客身份） ──

@pytest.mark.asyncio
async def test_non_owner_mail_processed_as_guest(monkeypatch):
    """非主人发件人的邮件仍被处理（访客身份），不被跳过。"""
    poller = _make_poller({"mail.owner_email": "Owner@Example.com"})
    processed = []

    async def _fake_run(args, timeout):
        return (0, "{}", "")

    async def _fake_process(msg_id, *a, **k):
        processed.append(msg_id)

    monkeypatch.setattr("tools.mail_tools._run_agently", _fake_run)
    monkeypatch.setattr(poller, "_process_one_email", _fake_process)
    monkeypatch.setattr(
        "web.mail_poller._extract_messages",
        lambda out: [_msg("m1", "stranger@x.com"), _msg("m2", "owner@example.com")],
    )

    await poller._poll_inbox("all")
    # 两封都处理（stranger 以访客身份、owner 以主人身份，由身份解析决定）
    assert processed == ["m1", "m2"]
    assert "owner@example.com" in poller.core.security.owner_ids


# ── 3. 身份注册/注销同步 ──

def test_owner_identity_registered_idempotent():
    """主人邮箱被注册进 core.security.owner_ids（主人身份解析），重复调用幂等。"""
    poller = _make_poller({"mail.owner_email": "Owner@Example.com"})
    poller._sync_owner_identity("owner@example.com")
    assert "owner@example.com" in poller.core.security.owner_ids

    poller._sync_owner_identity("owner@example.com")
    assert len([e for e in poller.core.security.owner_ids if e == "owner@example.com"]) == 1


def test_owner_email_change_unregisters_old():
    """更换主人邮箱时旧邮箱注销；清空时同样注销，不残留主人权限。"""
    poller = _make_poller({"mail.owner_email": "a@x.com"})
    poller._sync_owner_identity("a@x.com")
    assert "a@x.com" in poller.core.security.owner_ids

    # 更换：旧注销、新注册
    poller._sync_owner_identity("b@x.com")
    assert "a@x.com" not in poller.core.security.owner_ids
    assert "b@x.com" in poller.core.security.owner_ids

    # 清空：注销且不注册任何
    poller._sync_owner_identity("")
    assert "b@x.com" not in poller.core.security.owner_ids
    assert poller.core.security.owner_ids == set()


def test_owner_sync_does_not_touch_env_configured_owners():
    """环境变量配置的 owner（OWNER_IDS）不被同步逻辑误删。"""
    poller = _make_poller({"mail.owner_email": "a@x.com"})
    poller.core.security.owner_ids.add("qq_env_owner")

    poller._sync_owner_identity("a@x.com")
    poller._sync_owner_identity("")  # 清空主人邮箱
    assert "qq_env_owner" in poller.core.security.owner_ids
    assert "a@x.com" not in poller.core.security.owner_ids


# ── 4. 配置 API：owner_email 校验与往返 ──

def _make_client(cfg: FakeCfg, monkeypatch) -> TestClient:
    from fastapi import Request

    from web.routers import mail_manage
    from web.routers.auth import get_current_user as _orig_user

    app = FastAPI()
    app.include_router(mail_manage.router)

    async def _fake_user(request: Request = None) -> str:
        return "webui"

    app.dependency_overrides[_orig_user] = _fake_user
    monkeypatch.setattr(mail_manage, "_get_cfg_service", lambda request: cfg)
    return TestClient(app, client=("127.0.0.1", 0))


def test_mail_config_owner_email_roundtrip(monkeypatch):
    cfg = FakeCfg()
    client = _make_client(cfg, monkeypatch)

    r = client.put("/mail/config", json={
        "enabled": True, "mode": "all", "allowed_senders": [],
        "reply_channel": "mail", "max_per_day": 50, "dnd_start": 0, "dnd_end": 0,
        "owner_email": "owner@example.com",
    })
    assert r.status_code == 200, r.text
    assert r.json()["data"]["owner_email"] == "owner@example.com"

    r2 = client.get("/mail/config")
    assert r2.status_code == 200
    assert r2.json()["data"]["owner_email"] == "owner@example.com"


def test_mail_config_rejects_invalid_owner_email(monkeypatch):
    cfg = FakeCfg()
    client = _make_client(cfg, monkeypatch)

    r = client.put("/mail/config", json={
        "enabled": True, "mode": "all", "allowed_senders": [],
        "reply_channel": "mail", "max_per_day": 50, "dnd_start": 0, "dnd_end": 0,
        "owner_email": "not-an-email",
    })
    assert r.status_code == 400
