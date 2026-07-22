"""_clean_reply_full 测试：统一清洗出口，三处入口行为一致。

验证：混合泄漏（N5 安全推理 + 生图泄漏）全清；strip_emotion 开关行为正确；
清洗幂等（对已清洗输出再清洗不变，对应三入口一致性）。
"""
from unittest.mock import MagicMock

from agent_core.tool_executor import ToolExecutorMixin


class _Stub(ToolExecutorMixin):
    def __init__(self):
        # get_sticker_manager 返回一个 strip_emotion_tag 透传的桩
        self._sm = MagicMock()
        self._sm.strip_emotion_tag = MagicMock(side_effect=lambda t: t)

    def get_sticker_manager(self, name):
        return self._sm


COMPLEX_INPUT = (
    "[该内容涉及生成露骨的色情内容，超出了范围。]"
    "Agnes Image 2.1 Flash 给你画好啦～ "
    "![x](https://image.pollinations.ai/prompt/cat) "
    'Width Height: 560x792 | Seed: 1 | Model: D | Prompt: "cat"'
)


def test_cleans_mixed_leaks():
    m = _Stub()
    out = m._clean_reply_full(COMPLEX_INPUT, style="xiaoda", strip_emotion=False)
    # N5 安全推理方括号已清
    assert "色情内容" not in out
    assert "超出了范围" not in out
    # 生图类泄漏已清
    assert "Agnes Image 2.1 Flash" not in out
    assert "Width Height" not in out
    assert "Seed: 1" not in out
    # 人格回复保留
    assert "给你画好啦" in out


def test_strip_emotion_false_does_not_call_sticker_manager():
    m = _Stub()
    m._clean_reply_full("你好呀～", style="xiaoda", strip_emotion=False)
    m._sm.strip_emotion_tag.assert_not_called()


def test_strip_emotion_true_calls_sticker_manager():
    m = _Stub()
    m._clean_reply_full("你好呀～", style="xiaoda", strip_emotion=True)
    m._sm.strip_emotion_tag.assert_called_once()


def test_idempotent_three_entrypoints_consistent():
    # _finalize_reply 内部走 _clean_reply_full；fast-path/主路径 else 也走 _clean_reply_full
    # 此处直接验证 _clean_reply_full 幂等性：对已清洗输出再清洗不变（稳定）
    m = _Stub()
    once = m._clean_reply_full(COMPLEX_INPUT, style="xiaoda", strip_emotion=False)
    twice = m._clean_reply_full(once, style="xiaoda", strip_emotion=False)
    assert once == twice


def test_empty_input():
    m = _Stub()
    assert m._clean_reply_full("", style="xiaoda") == ""
