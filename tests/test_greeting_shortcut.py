"""G1: 问候短路测试 - 纯问候 <100ms 返回，不调 LLM."""
import os
import time

os.environ["ENABLE_GREETING_SHORTCUT"] = "true"

from agent_core.message_processor import MessageProcessorMixin


def _make_processor():
    """构造无依赖的 MessageProcessorMixin 实例."""
    mp = MessageProcessorMixin.__new__(MessageProcessorMixin)
    mp.slash_handler = None
    return mp


def test_pure_greeting_returns_shortcut():
    """纯问候"你好"应返回 shortcut reply，不调 LLM."""
    mp = _make_processor()
    # 任何问候变体
    for greeting in ["你好", "你好！", "你好。", "hi", "hello", "嗨", "在吗", "在不在？"]:
        result = mp._try_greeting_shortcut(greeting, "user1", "qq")
        assert result is not None, f"应命中短路: {greeting}"
        assert result.reply, f"reply 不能为空: {greeting}"
        assert result.emotion == "greeting"


def test_non_greeting_returns_none():
    """非问候"帮我写函数"应返回 None，走正常流程."""
    mp = _make_processor()
    for text in ["帮我写函数", "今天天气怎么样", "你好帮我写代码", "请问一下", "你好请问"]:
        result = mp._try_greeting_shortcut(text, "user1", "qq")
        assert result is None, f"不应命中短路: {text}"


def test_thank_you_returns_shortcut():
    """感谢类"谢谢"应返回 shortcut."""
    mp = _make_processor()
    for text in ["谢谢", "感谢", "thanks", "thx"]:
        result = mp._try_greeting_shortcut(text, "user1", "qq")
        assert result is not None


def test_greeting_shortcut_latency_under_100ms():
    """问候短路延迟 < 100ms."""
    mp = _make_processor()
    start = time.monotonic()
    for _ in range(100):
        mp._try_greeting_shortcut("你好", "user1", "qq")
    elapsed = (time.monotonic() - start) * 1000 / 100  # 平均 ms
    assert elapsed < 100, f"平均延迟 {elapsed:.1f}ms 应 <100ms"


def test_group_chat_skips_shortcut():
    """群聊模式不触发短路（避免刷屏）."""
    mp = _make_processor()
    result = mp._try_greeting_shortcut("你好", "user1", "qq_group")
    assert result is None


def test_disabled_via_env():
    """ENABLE_GREETING_SHORTCUT=false 时关闭短路."""
    mp = _make_processor()
    original = os.environ.get("ENABLE_GREETING_SHORTCUT")
    try:
        os.environ["ENABLE_GREETING_SHORTCUT"] = "false"
        result = mp._try_greeting_shortcut("你好", "user1", "qq")
        assert result is None
    finally:
        if original is None:
            os.environ.pop("ENABLE_GREETING_SHORTCUT", None)
        else:
            os.environ["ENABLE_GREETING_SHORTCUT"] = original
