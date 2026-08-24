"""测试 WebUI 子 agent 路径的 XP 增加（修复 ws_hub.py:385）"""
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core._shared import ProcessResult
from web.ws_hub import process_and_serialize


@pytest.mark.asyncio
async def test_sub_agent_path_adds_xp():
    """子 agent 路径应触发 XP 增加（修复前：绕过 XP 逻辑）"""
    # Mock core 对象
    mock_core = MagicMock()
    mock_core.dispatch_web_sub_agent = AsyncMock(
        return_value=MagicMock(reply="test reply", emotion=None, sticker_path=None,
                               audio_path=None, image_paths=[], video_path=None,
                               tts_pending=False, tts_text="")
    )

    # Mock agent registry
    mock_registry = MagicMock()
    mock_registry.is_enabled.return_value = True
    mock_core.dispatcher.get_agent.return_value = MagicMock()  # 子 agent 存在

    # CR-FIX: mock 路径应该是 core.xp_system.get_xp_system
    # 因为 get_xp_system 是在 ws_hub.py 内部从 core.xp_system 导入的
    with patch('core.xp_system.get_xp_system') as mock_xp_sys:
        mock_xp_instance = MagicMock()
        mock_xp_instance.add_chat_xp = MagicMock()
        mock_xp_sys.return_value = mock_xp_instance

        # Mock app.state
        mock_app = MagicMock()
        mock_app.state.agent_registry = mock_registry

        # CR-FIX: 设置 MASTER_QQ_OPENID 环境变量以匹配预期
        os.environ["MASTER_QQ_OPENID"] = "webui_test"

        # 调用 process_and_serialize（子 agent 路径）
        result = await process_and_serialize(
            core=mock_core,
            text="你好",
            session_id="test_session",
            agent="kimi",  # 子 agent（!= "xiaoda"）
            app=mock_app,
        )

        # 验证 XP 增加被调用（参数为环境变量值 + 文本长度）
        # 实际调用使用 os.getenv("MASTER_QQ_OPENID", "webui")
        mock_xp_instance.add_chat_xp.assert_called_once()

        # 验证返回结果
        assert result is not None
        assert "reply" in result or "data" in result


@pytest.mark.asyncio
async def test_web_subagent_preserves_media_serialization(tmp_path, monkeypatch):
    from web import ws_hub

    monkeypatch.setattr(ws_hub, "MEDIA_ROOT", tmp_path / "media")
    sticker = tmp_path / "sticker.png"
    audio = tmp_path / "audio.wav"
    image = tmp_path / "image.png"
    video = tmp_path / "video.mp4"
    for path in (sticker, audio, image, video):
        path.write_bytes(b"media")

    core = MagicMock()
    core.dispatcher.get_agent.return_value = MagicMock()
    core.dispatch_web_sub_agent = AsyncMock(return_value=ProcessResult(
        reply="media reply",
        emotion="happy",
        sticker_path=sticker,
        audio_path=audio,
        image_paths=[image],
        video_path=video,
    ))

    data = await process_and_serialize(
        core,
        "画一张图",
        session_id="web-session",
        agent="xiaolang",
    )

    core.dispatch_web_sub_agent.assert_awaited_once()
    assert data["reply"] == "media reply"
    assert data["sticker_url"].endswith("/sticker.png")
    assert data["audio_url"].endswith("/audio.wav")
    assert data["image_urls"][0].endswith("/image.png")
    assert data["video_url"].endswith("/video.mp4")


@pytest.mark.asyncio
async def test_slash_command_with_subagent_selected_still_uses_main_process():
    core = MagicMock()
    core.process = AsyncMock(return_value=ProcessResult(reply="slash reply"))
    core.dispatch_web_sub_agent = AsyncMock()

    data = await process_and_serialize(
        core,
        "/help",
        session_id="web-session",
        agent="xiaolang",
    )

    core.process.assert_awaited_once()
    core.dispatch_web_sub_agent.assert_not_awaited()
    assert data["reply"] == "slash reply"


@pytest.mark.asyncio
async def test_main_path_xp_unchanged():
    """主 agent 路径的 XP 逻辑不受影响（在 message_processor 中）"""
    # Mock core 对象
    mock_core = MagicMock()
    mock_process_result = MagicMock(reply="test reply", emotion=None, tts_pending=False, tts_text="")
    mock_core.process = AsyncMock(return_value=mock_process_result)

    # 调用 process_and_serialize（主 agent 路径）
    with patch('web.ws_hub.serialize_result', return_value={"reply": "test reply"}):
        result = await process_and_serialize(
            core=mock_core,
            text="你好",
            session_id="test_session",
            agent="xiaoda",  # 主 agent
        )

        # 验证 core.process 被调用（主路径）
        mock_core.process.assert_called_once()

        # XP 增加在 message_processor 中，不在 ws_hub，所以不在这里验证
        assert result is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
