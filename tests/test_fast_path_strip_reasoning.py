"""测试 fast_path 路径的 strip_reasoning 调用

验证：agnes-2.0-flash 等模型输出的推理标签在 fast_path 中被正确清理
"""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

# 确保项目路径
from pathlib import Path
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(PROJECT_ROOT))


def test_strip_reasoning_imports():
    """验证 strip_reasoning 函数可导入且正常工作"""
    from utils.text_utils import strip_reasoning

    # 测试 Agnes 风格推理标签
    input_text = "[emotion thinking]`` 这是内部推理\n实际回复内容"
    result = strip_reasoning(input_text)
    assert "[emotion thinking]" not in result
    assert "实际回复内容" in result

    # 测试裸文本推理行
    input_text = "Need think about this\nLet me recall...\n实际回复"
    result = strip_reasoning(input_text)
    assert "Need think" not in result
    assert "Let me recall" not in result
    assert "实际回复" in result

    # 测试第三人称引用
    input_text = 'They ask "how are you"\n实际回复内容'
    result = strip_reasoning(input_text)
    assert "They ask" not in result


def test_strip_reasoning_agnes_patterns():
    """测试 Agnes 模型特定的推理格式

    注意：(Final Output Generation) 等格式目前未匹配，需要后续增强 strip_reasoning
    """
    from utils.text_utils import strip_reasoning

    # 测试已支持的模式
    agnes_output = """[emotion thinking]`` 这是一段推理内容，需要清理
Let me think about this...
Need to recall...
实际回复：小妲来了哦～"""

    result = strip_reasoning(agnes_output)
    # 已支持的推理内容应被清理
    assert "[emotion thinking]" not in result
    assert "Let me think" not in result
    assert "Need to recall" not in result
    # 实际回复应保留
    assert "小妲来了哦" in result


@pytest.mark.asyncio
async def test_fast_path_finalize_calls_strip_reasoning():
    """测试 _finalize_fast_path_reply 调用 strip_reasoning

    这是回归测试：确保修复后 fast_path 会清理推理内容
    """
    from agent_core.message_processor import MessageProcessorMixin

    # 创建最小化的 MessageProcessorMixin 实例
    processor = MessageProcessorMixin.__new__(MessageProcessorMixin)

    # Mock 必要的属性
    processor.security = MagicMock()
    processor.security.check_output_privacy = MagicMock(return_value=(True, "", None))
    processor.router = MagicMock()
    processor.router.flush_costs = MagicMock()
    processor.context = MagicMock()
    processor.context.add_message = AsyncMock()
    processor._bg_task_manager = MagicMock()
    processor._bg_task_manager.run_background_tasks = MagicMock()
    processor._hook_engine = MagicMock()
    processor._hook_engine.fire_post_response = MagicMock()
    processor.sticker_manager = MagicMock()
    processor.sticker_manager.strip_emotion_tag = MagicMock(side_effect=lambda x: x)

    # Mock get_sticker_info 返回带有推理标签的回复
    reply_with_reasoning = "[emotion thinking]`` Need think\n实际回复内容"
    processor.get_sticker_info = MagicMock(return_value=(reply_with_reasoning, None))

    # Mock 其他依赖
    with patch("agent_core.message_processor.is_unified", return_value=False), \
         patch("agent_core.message_processor.get_degradation_strategy") as mock_degradation:
        mock_degradation.return_value.is_feature_available.return_value = True

        # Mock _build_voice_result
        processor._build_voice_result = AsyncMock(return_value=(None, False, ""))

        # Mock spawn
        with patch("agent_core.message_processor._spawn"):
            result = await processor._finalize_fast_path_reply(
                reply="[emotion thinking]`` 推理内容\n实际回复",
                user_input="test",
                is_master=True,
                user_id="test_user",
                source="web",
                emotion="happy",
                emotion_label="happy",
                ctx=MagicMock(last_user_emotion="neutral"),
                user_openid="test",
                session_id="test",
                force_voice=False,
            )

    # 验证：推理内容应被清理
    assert "[emotion thinking]" not in result.reply
    assert "推理内容" not in result.reply or "推理" not in result.reply


def test_strip_reasoning_chinese_monologue():
    """测试中文内部独白/推理泄露的清理

    回归测试：模型将中文思维链当作正文输出，包含"现在开始回复"、
    "根据SOUL.md"、"根据记忆碎片"等内部独白短语，应被 strip_reasoning 清理。
    """
    from utils.text_utils import strip_reasoning

    # 模型输出的中文内部独白（实际泄露样本的精简版）
    leaked_output = """根据SOUL.md中的时间感知规则，沙在中午时间点发消息时，我可以温柔提醒吃饭。
汐问现在几点了，我需要用真实时间回应。
根据记忆碎片，之前汐曾指出时间错误，我需要确保准确回应。
现在时间是中午12:17，星期六。
我应该给出准确时间，并可以温柔提醒吃饭。
我需要用小妲的温柔语气回应。
现在开始组织回复。
沙问"妲妲~现在几点了"，我应该用温柔语气回应，告诉沙现在是中午12:17，星期六，并温柔提醒沙该吃午饭了。
现在开始回复。"""

    result = strip_reasoning(leaked_output)

    # 内部独白短语应被清理
    assert "根据SOUL.md" not in result
    assert "根据记忆碎片" not in result
    assert "现在开始回复" not in result
    assert "现在开始组织回复" not in result
    assert "我需要用小妲" not in result
    assert "我应该给出准确时间" not in result
    # 第三人称引用（沙问/汐问）应被清理
    assert "沙问" not in result
    assert "汐问" not in result


def test_strip_reasoning_chinese_monologue_preserves_reply():
    """测试中文推理清理后保留正常回复内容"""
    from utils.text_utils import strip_reasoning

    # 推理 + 实际回复
    mixed_output = """根据SOUL.md，我应该温柔回应。
现在开始回复。
妲妲~现在是中午12:17哦，星期六。该吃午饭了呢，别忘了好好吃饭呀～"""

    result = strip_reasoning(mixed_output)

    # 推理内容应被清理
    assert "根据SOUL.md" not in result
    assert "现在开始回复" not in result
    # 实际回复应保留
    assert "妲妲" in result
    assert "12:17" in result
    assert "吃午饭" in result