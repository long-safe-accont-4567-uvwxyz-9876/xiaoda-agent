"""Task 6: 斜杠命令经 WS 聊天通道真实执行。

关键裁决：不走 HTTP /api/v1/commands/run（不存在），而是复用 WSClient.chat
把命令当聊天消息发给主进程共享 AgentCore。这里验证 `_send_slash`：
- 未连接主进程（_ws is None）时不抛异常，仅写入状态提示。
"""
import pytest

from cli_app import XiaodaApp


@pytest.mark.asyncio
async def test_send_slash_no_connection_not_raise(monkeypatch):
    """未连接主进程时 _send_slash 不抛异常，仅追加一条状态提示。"""
    async def _noop(self, on_status):
        return None

    monkeypatch.setattr(XiaodaApp, "_connect_main_process", _noop)
    app = XiaodaApp()
    async with app.run_test() as pilot:
        chat = app.query_one("#chat")
        before = len(chat.children)
        await app._send_slash("/status", chat)
        await pilot.pause()
        assert len(chat.children) == before + 1
        assert "尚未连接主进程" in str(chat.children[-1].render())