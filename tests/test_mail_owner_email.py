"""主人邮箱门控测试。

需求：邮箱设置中指定一个特定邮箱为主人邮箱；未设置时邮件功能整体不生效，
设置后仅该邮箱的邮件被处理（以主人身份，完整工具权限）。
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


# ── 1. 未设置主人邮箱：整体不生效（不拉取不处理） ──

@pytest.mark.asyncio
async def test_owner_email_not_set_skips_everything(monkeypatch):
    """未配置主人邮箱时，_poll_inbox 直接返回，不调用 agently 不处理任何邮件。"""
    poller = _make_poller({"mail.owner_email": ""})
    called = {"run": False, "process": False}

    async def _fake_run(args, timeout):
        called["run"] = True
        return (0, "{}", "")

    async def _fake_process(*a, **k):
        called["process"] = True

    monkeypatch.setattr("tools.mail_tools._run_agently", _fake_run)
    monkeypatch.setattr(poller, "_process_one_email", _fake_process)

    await poller._poll_inbox("all")
    assert called["run"] is False
    assert called["process"] is False


# ── 2. 设置主人邮箱：仅主人邮件被处理 ──

@pytest.mark.asyncio
async def test_non_owner_mail_skipped(monkeypatch):
    """非主人发件人邮件被忽略（标记已处理，不进入处理流程）。"""
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
    assert processed == ["m2"]
    # 非主人邮件被标记已处理，不会重复拉取
    assert "m1" in poller._processed_ids


@pytest.mark.asyncio
async def test_owner_mail_processed_and_identity_registered(monkeypatch):
    """主人邮箱被注册进 core.security.owner_ids（主人身份解析）。"""
    core = FakeCore()
    from web.mail_poller import MailPoller
    poller = MailPoller(core=core, config_service=FakeCfg(**{"mail.owner_email": "Owner@Example.com"}))

    poller._ensure_owner_identity("owner@example.com")
    assert "owner@example.com" in core.security.owner_ids

    # 重复调用幂等
    poller._ensure_owner_identity("owner@example.com")
    assert len([e for e in core.security.owner_ids if e == "owner@example.com"]) == 1


# ── 3. 配置 API：owner_email 校验与往返 ──

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
