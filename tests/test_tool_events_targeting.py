"""工具事件定向发送与脱敏测试（审计修复 2026-08-29）。

覆盖：
- args_preview 序列化前按键名脱敏（token/secret/password/passwd/api_key/key/
  authorization/cookie/credential，大小写不敏感），仍截断 200；
- 事件定向发给当前请求来源连接（current_conn_id），不再广播给所有连接；
- 两个不同连接只有来源连接收到；
- conn 为空（QQ/CLI 入口）跳过 WS 发送；
- msg_id 使用 web._msg_context.current_msg_id（消除双 contextvar，不再恒为空）。
"""
import asyncio

import pytest

import web.tool_events as tool_events
import web.ws_hub as ws_hub_mod
from web._msg_context import current_conn_id, current_msg_id
from web.tool_events import _redact_args_preview, emit_tool_event


class FakeManager:
    """记录 send_to/broadcast 调用的假 ConnectionManager。"""

    def __init__(self, active: int = 2) -> None:
        self.active = active
        self.sent: list[tuple[str, dict]] = []
        self.broadcasts: list[dict] = []

    @property
    def active_count(self) -> int:
        return self.active

    async def send_to(self, conn_id: str, event: dict) -> None:
        self.sent.append((conn_id, event))

    async def broadcast(self, event: dict) -> None:
        self.broadcasts.append(event)


@pytest.fixture
def fake_manager(monkeypatch):
    """替换 ws_hub.manager（emit_tool_event 在函数内 import，读模块属性）。"""
    mgr = FakeManager()
    monkeypatch.setattr(ws_hub_mod, "manager", mgr)
    return mgr


async def _drain() -> None:
    """让 _spawn 的后台发送任务跑完（fire-and-forget，需让出事件循环）。"""
    for _ in range(5):
        await asyncio.sleep(0)


def test_sensitive_keys_redacted_case_insensitive():
    """敏感键（大小写不敏感子串命中）值替换为 ***，键名保留、非敏感键不动。"""
    preview = _redact_args_preview({
        "api_key": "sk-abc123",
        "Password": "p@ss",
        "Authorization": "Bearer-xyz",
        "session_cookie": "sid-123",
        "access_token": "tok-123",
        "client_secret": "sec-456",
        "db_credential": "cred-789",
        "passwd": "pw-000",
        "key": "plain-key-value",
        "query": "hello",
    })
    for secret in ("sk-abc123", "p@ss", "Bearer-xyz", "sid-123", "tok-123",
                   "sec-456", "cred-789", "pw-000", "plain-key-value"):
        assert secret not in preview
    # 序列化在截断前完成，长参数下 "query" 的值可能被截断，只断言键名保留
    assert '"query"' in preview
    assert preview.count("***") == 9


def test_non_sensitive_value_preserved():
    """非敏感键的值原样保留。"""
    preview = _redact_args_preview({"query": "hello", "limit": 5})
    assert preview == '{"query": "hello", "limit": 5}'


def test_args_preview_truncated_to_200():
    """脱敏后仍截断到 200 字符。"""
    preview = _redact_args_preview({"data": "x" * 5000, "api_key": "sk-long" * 100})
    assert len(preview) == 200
    assert "sk-long" not in preview


def test_empty_args_empty_preview():
    assert _redact_args_preview(None) == ""
    assert _redact_args_preview({}) == ""


@pytest.mark.asyncio
async def test_emit_targets_origin_conn_only(fake_manager):
    """事件只发来源连接（conn-A），不广播；msg_id 对上、敏感值脱敏。"""
    msg_token = current_msg_id.set("m-1")
    conn_token = current_conn_id.set("conn-A")
    try:
        await emit_tool_event("start", "web_search", {"query": "x", "api_key": "sk-1"})
        await _drain()
    finally:
        current_msg_id.reset(msg_token)
        current_conn_id.reset(conn_token)

    assert len(fake_manager.sent) == 1
    conn_id, event = fake_manager.sent[0]
    assert conn_id == "conn-A"
    assert fake_manager.broadcasts == []
    assert event["type"] == "tool_event"
    assert event["msg_id"] == "m-1"
    assert event["phase"] == "start"
    assert event["tool"] == "web_search"
    assert "sk-1" not in event["args_preview"]
    assert "***" in event["args_preview"]


@pytest.mark.asyncio
async def test_second_conn_never_receives(fake_manager):
    """两个连接在线时，只有来源连接收到事件，conn-B 不收到。"""
    fake_manager.sent.append(("conn-B", {"type": "tool_event", "prev": True}))
    msg_token = current_msg_id.set("m-2")
    conn_token = current_conn_id.set("conn-1")
    try:
        await emit_tool_event("end", "read_file", ok=True, elapsed_ms=5)
        await _drain()
    finally:
        current_msg_id.reset(msg_token)
        current_conn_id.reset(conn_token)

    recipients = [c for c, _ in fake_manager.sent]
    assert recipients == ["conn-B", "conn-1"]  # 新增事件只投递给来源 conn-1
    assert fake_manager.broadcasts == []


@pytest.mark.asyncio
async def test_empty_conn_skips_ws_send(fake_manager):
    """conn 为空（QQ/CLI 等非 WS 入口）：跳过 WS 发送，也绝不广播。"""
    await emit_tool_event("start", "web_search", {"query": "x"})
    await _drain()
    assert fake_manager.sent == []
    assert fake_manager.broadcasts == []


@pytest.mark.asyncio
async def test_no_active_connections_noop(monkeypatch):
    """无活跃连接时直接返回（不构建事件、不发送）。"""
    mgr = FakeManager(active=0)
    monkeypatch.setattr(ws_hub_mod, "manager", mgr)
    conn_token = current_conn_id.set("conn-A")
    try:
        await emit_tool_event("start", "web_search", {"query": "x"})
        await _drain()
    finally:
        current_conn_id.reset(conn_token)
    assert mgr.sent == []


@pytest.mark.asyncio
async def test_msg_id_uses_unified_contextvar(fake_manager):
    """msg_id 来自 web._msg_context.current_msg_id（修复双 contextvar 导致的恒空）。"""
    msg_token = current_msg_id.set("m-42")
    conn_token = current_conn_id.set("conn-A")
    try:
        await emit_tool_event("end", "t", ok=True)
        await _drain()
    finally:
        current_msg_id.reset(msg_token)
        current_conn_id.reset(conn_token)
    assert tool_events.current_msg_id is current_msg_id
    assert fake_manager.sent[0][1]["msg_id"] == "m-42"
