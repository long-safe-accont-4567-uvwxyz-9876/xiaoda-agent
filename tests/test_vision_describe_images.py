"""P0 修复验证测试：_describe_images 安全客户端 + 响应校验 + BadRequestError 捕获

配套 spec：docs/specs/spec-context-management-2026-07-26.md
配套 tasks：Task 1.6, 1.7, 1.8

验证目标：
1. _describe_images 走 _select_client_for_provider，不直接访问 _client
2. 识别 "cannot read image" 等失败模式，走兜底分支
3. 捕获 BadRequestError 并记录 status_code
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import openai
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_vision_response(content: str):
    """构造模拟的 vision API response。"""
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    response = MagicMock()
    response.choices = [choice]
    return response


@pytest.mark.asyncio
async def test_vision_no_direct_client_access():
    """Task 1.6: _describe_images 走 _select_client_for_provider，不直接访问 _client。"""
    from agent_core.message_processor import MessageProcessorMixin

    # 创建 mixin 实例（不通过 AgentCore）
    processor = MessageProcessorMixin.__new__(MessageProcessorMixin)

    # mock router：_select_client_for_provider 返回 mock client
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_make_vision_response("这是一张猫的图片，毛色橘黄。")
    )
    mock_router = MagicMock()
    mock_router._select_client_for_provider = AsyncMock(return_value=mock_client)
    # P0 修复：_describe_images 现在先调用 get_vision_provider_and_model() 动态选择
    mock_router.get_vision_provider_and_model = MagicMock(return_value=("mimo", "mimo-vision"))
    # 注意：_client 仍然存在，但代码不应使用它
    mock_router._client = "SHOULD_NOT_BE_USED"
    processor.router = mock_router

    image_data = [{"data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB", "mimeType": "image/png"}]
    result = await processor._describe_images(image_data)

    # 验证：调用了 _select_client_for_provider（参数来自 get_vision_provider_and_model）
    mock_router._select_client_for_provider.assert_called_once_with("mimo")
    # 验证：调用了 mock_client（不是 _client）
    mock_client.chat.completions.create.assert_called_once()
    # 验证：返回了合法 description
    assert "猫" in result
    print("✅ Task 1.6 验证通过：走 _select_client_for_provider，未直接访问 _client")


@pytest.mark.asyncio
async def test_vision_failure_pattern_cannot_read_image():
    """Task 1.7: 识别 "cannot read image" 失败模式，走兜底分支。"""
    from agent_core.message_processor import MessageProcessorMixin

    processor = MessageProcessorMixin.__new__(MessageProcessorMixin)
    mock_client = MagicMock()
    # 模拟 MiMo 返回 "cannot read image"
    mock_client.chat.completions.create = AsyncMock(
        return_value=_make_vision_response("cannot read image")
    )
    mock_router = MagicMock()
    mock_router._select_client_for_provider = AsyncMock(return_value=mock_client)
    mock_router.get_vision_provider_and_model = MagicMock(return_value=("mimo", "mimo-vision"))
    processor.router = mock_router

    image_data = [{"data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB", "mimeType": "image/png"}]
    result = await processor._describe_images(image_data)

    # 验证：返回空字符串（走兜底分支）
    assert result == "", \
        f"应返回空字符串走兜底，实际返回: {result!r}"
    print("✅ Task 1.7 验证通过：识别 'cannot read image' 失败模式，走兜底分支")


@pytest.mark.asyncio
async def test_vision_failure_pattern_chinese():
    """Task 1.7: 识别中文失败模式 '无法识别图片'。"""
    from agent_core.message_processor import MessageProcessorMixin

    processor = MessageProcessorMixin.__new__(MessageProcessorMixin)
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_make_vision_response("抱歉，无法识别这张图片内容。")
    )
    mock_router = MagicMock()
    mock_router._select_client_for_provider = AsyncMock(return_value=mock_client)
    mock_router.get_vision_provider_and_model = MagicMock(return_value=("mimo", "mimo-vision"))
    processor.router = mock_router

    image_data = [{"data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB", "mimeType": "image/png"}]
    result = await processor._describe_images(image_data)

    assert result == "", f"中文失败模式应走兜底，实际: {result!r}"
    print("✅ Task 1.7 验证通过：识别中文失败模式 '无法识别'")


@pytest.mark.asyncio
async def test_vision_too_short_response():
    """Task 1.7: 过短响应（< 10 字符）走兜底。"""
    from agent_core.message_processor import MessageProcessorMixin

    processor = MessageProcessorMixin.__new__(MessageProcessorMixin)
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_make_vision_response("猫")  # 仅 1 字符
    )
    mock_router = MagicMock()
    mock_router._select_client_for_provider = AsyncMock(return_value=mock_client)
    mock_router.get_vision_provider_and_model = MagicMock(return_value=("mimo", "mimo-vision"))
    processor.router = mock_router

    image_data = [{"data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB", "mimeType": "image/png"}]
    result = await processor._describe_images(image_data)

    assert result == "", f"过短响应应走兜底，实际: {result!r}"
    print("✅ Task 1.7 验证通过：过短响应（< 10 字符）走兜底")


@pytest.mark.asyncio
async def test_vision_bad_request_error():
    """Task 1.8: 捕获 BadRequestError，记录 status_code。"""
    from agent_core.message_processor import MessageProcessorMixin

    processor = MessageProcessorMixin.__new__(MessageProcessorMixin)
    mock_client = MagicMock()

    # 构造 BadRequestError（需要 message、response、body 参数）
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.headers = {}
    bad_request_err = openai.BadRequestError(
        message="invalid_image_format: cannot read image",
        response=mock_response,
        body={"error": {"code": "invalid_image_format", "message": "cannot read image"}},
    )

    mock_client.chat.completions.create = AsyncMock(side_effect=bad_request_err)
    mock_router = MagicMock()
    mock_router._select_client_for_provider = AsyncMock(return_value=mock_client)
    mock_router.get_vision_provider_and_model = MagicMock(return_value=("mimo", "mimo-vision"))
    processor.router = mock_router

    image_data = [{"data": "invalid_base64_data", "mimeType": "image/png"}]
    result = await processor._describe_images(image_data)

    # 验证：返回空字符串（走兜底）
    assert result == ""
    print("✅ Task 1.8 验证通过：捕获 BadRequestError，返回空字符串走兜底")


@pytest.mark.asyncio
async def test_vision_normal_success():
    """正常成功场景：返回合法 description。"""
    from agent_core.message_processor import MessageProcessorMixin

    processor = MessageProcessorMixin.__new__(MessageProcessorMixin)
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_make_vision_response("这是一张风景照片，远处有山脉，近处有湖泊，天空晴朗。")
    )
    mock_router = MagicMock()
    mock_router._select_client_for_provider = AsyncMock(return_value=mock_client)
    mock_router.get_vision_provider_and_model = MagicMock(return_value=("mimo", "mimo-vision"))
    processor.router = mock_router

    image_data = [{"data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB", "mimeType": "image/png"}]
    result = await processor._describe_images(image_data)

    assert "山脉" in result and "湖泊" in result
    print("✅ 正常场景验证通过：合法 description 透传")


if __name__ == "__main__":
    asyncio.run(test_vision_no_direct_client_access())
    asyncio.run(test_vision_failure_pattern_cannot_read_image())
    asyncio.run(test_vision_failure_pattern_chinese())
    asyncio.run(test_vision_too_short_response())
    asyncio.run(test_vision_bad_request_error())
    asyncio.run(test_vision_normal_success())
    print("\n🎉 所有 P0 vision 测试通过！")
