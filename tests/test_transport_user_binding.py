"""传输层 User 绑定测试。

验证 QQ / Web 适配器在 agent.process 执行期间将正确的 User 类型绑定到 EventBus，
而非仅测试 EventBus 的 bind/unbind API。
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core._shared import ProcessResult
from core.event_bus import event_bus


def test_event_bus_has_no_user_after_unbind():
    """unbind 后 bound_user 为 None（EventBus API 基线测试）。"""
    event_bus.unbind_user()
    assert event_bus.bound_user is None


@pytest.mark.asyncio
async def test_qq_adapter_binds_qq_user_during_process():
    """QQ 适配器 _process_c2c_reply 在 agent.process 期间绑定 QQUser，完成后解绑。

    验证真实适配器行为：_process_c2c_reply 调用 agent.process 前 bind QQUser，
    process 返回后（finally）unbind。在 process 执行期间 bound_user 应为 QQUser 实例。
    """
    from agent_core.user_qq import QQUser
    from qq_bot_adapter import AIQQBot

    event_bus.unbind_user()  # 清理前序状态

    captured: list = []

    async def fake_process(*args, **kwargs):
        # 在 agent.process 执行期间捕获 bound_user
        captured.append(event_bus.bound_user)
        return ProcessResult(reply="")  # 空 reply，跳过后续回复发送

    # 使用 __new__ 绕过 botpy.Client.__init__，仅设置 _process_c2c_reply 依赖的属性
    bot = AIQQBot.__new__(AIQQBot)
    bot.hitl_enabled = False  # 跳过高危操作审批（_check_high_risk_approval 直接返回）
    bot.agent = MagicMock()
    bot.agent.process = AsyncMock(side_effect=fake_process)

    class FakeMessage:
        async def reply(self, content: str = "", msg_seq: int = 0) -> dict:
            # 模拟真实 botpy：成功返回消息 dict（None 会被判为投递不明确，
            # 导致 ACK 路径在 agent.process 之前中断）
            return {"id": "fake_msg"}

    message = FakeMessage()

    try:
        await bot._process_c2c_reply(
            message=message,
            user_input="你好",
            user_id="qq_test_openid",
            user_openid="test_openid",
            session_id="test_session",
            is_master=True,
            image_data=[],
        )

        # agent.process 执行期间 bound_user 应为 QQUser
        assert len(captured) == 1, "agent.process 应被调用一次"
        assert isinstance(captured[0], QQUser), \
            "agent.process 执行期间 bound_user 应为 QQUser"
        # _process_c2c_reply 返回后应已解绑
        assert event_bus.bound_user is None, "process 完成后应已解绑"
    finally:
        event_bus.unbind_user()


@pytest.mark.asyncio
async def test_web_adapter_binds_web_user_during_process():
    """Web 适配器 _handle_chat 在 core.process 期间绑定 WebUser，完成后解绑。

    验证真实适配器行为：_handle_chat 调用 process_and_serialize（内部调 core.process）
    前 bind WebUser，完成后（finally）unbind。在 process 执行期间 bound_user 应为 WebUser。
    """
    from agent_core.user_web import WebUser
    from web.ws_hub import _handle_chat, manager

    event_bus.unbind_user()  # 清理前序状态

    captured: list = []

    async def fake_process(*args, **kwargs):
        # 在 core.process 执行期间捕获 bound_user
        captured.append(event_bus.bound_user)
        return ProcessResult(reply="")  # 空 reply，serialize_result 安全处理

    # 模拟 app.state.core —— _handle_chat 通过 ws.scope.get("app") 获取
    fake_core = MagicMock()
    fake_core.process = AsyncMock(side_effect=fake_process)
    fake_app = MagicMock()
    fake_app.state.core = fake_core

    ws = MagicMock()
    ws.scope.get.return_value = fake_app

    conn_id = "test_bind_conn"
    msg = {"text": "你好", "agent": "xiaoda", "session_id": "test_sess"}
    msg_id = "test_msg_id"

    try:
        await _handle_chat(conn_id, msg, msg_id, ws)

        # core.process 执行期间 bound_user 应为 WebUser
        assert len(captured) == 1, "core.process 应被调用一次"
        assert isinstance(captured[0], WebUser), \
            "core.process 执行期间 bound_user 应为 WebUser"
        # _handle_chat 返回后应已解绑
        assert event_bus.bound_user is None, "process 完成后应已解绑"
    finally:
        event_bus.unbind_user()
        # 清理 manager 全局状态（_handle_chat 会写入 _session_map）
        manager._session_map.pop(conn_id, None)
        manager._agent_map.pop(conn_id, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attachment", "expected_text", "expected_image_data", "expected_context"),
    [
        (
            {"image_url": "/media/upload/sample.png"},
            "📷 图片",
            [{"mimeType": "image/png", "data": "a" * 100}],
            "",
        ),
        (
            {"doc_path": "/tmp/sample.pdf"},
            "📄 sample.pdf",
            None,
            "用户上传了文档：/tmp/sample.pdf。请使用 document_reader 工具读取该文档内容后回答用户的问题。",
        ),
    ],
)
async def test_web_adapter_processes_attachment_only_messages(
    attachment, expected_text, expected_image_data, expected_context, tmp_path, monkeypatch
):
    from utils import text_utils
    from web import ws_hub
    from web.ws_hub import _handle_chat, manager

    upload_dir = tmp_path / "upload"
    upload_dir.mkdir()
    (upload_dir / "sample.png").write_bytes(b"image")
    if "doc_path" in attachment:
        doc_path = upload_dir / "sample.pdf"
        doc_path.write_bytes(b"document")
        attachment = {**attachment, "doc_path": str(doc_path)}
        expected_context = (
            f"用户上传了文档：{doc_path}。请使用 document_reader 工具读取该文档内容后回答用户的问题。"
        )
    monkeypatch.setattr(ws_hub, "MEDIA_ROOT", tmp_path)
    monkeypatch.setattr(
        text_utils,
        "encode_image_to_base64",
        lambda _: ("image/png", "a" * 100),
    )

    fake_core = MagicMock()
    fake_core.process = AsyncMock(return_value=ProcessResult(reply="已处理"))
    fake_app = MagicMock()
    fake_app.state.core = fake_core
    ws = MagicMock()
    ws.scope = {"app": fake_app}
    conn_id = "test_attachment_only_conn"
    msg = {"text": "", "agent": "xiaoda", "session_id": "test_sess", **attachment}

    try:
        await _handle_chat(conn_id, msg, "test_attachment_only", ws)
        fake_core.process.assert_awaited_once()
        process_kwargs = fake_core.process.await_args.kwargs
        assert process_kwargs["user_input"] == expected_text
        assert process_kwargs["image_data"] == expected_image_data
        assert process_kwargs["system_context"] == expected_context
    finally:
        manager._session_map.pop(conn_id, None)
        manager._agent_map.pop(conn_id, None)
