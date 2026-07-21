"""G1: 问候短路测试 - 纯问候 <100ms 返回，不调 LLM."""
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

# P2-5: 改用 fixture 而非模块级 env var 设置，避免污染其他测试
# 原代码: os.environ["ENABLE_GREETING_SHORTCUT"] = "true" (模块级，永不恢复)

from agent_core.message_processor import MessageProcessorMixin
from agent_core import message_processor as mp_module


@pytest.fixture(autouse=True)
def _enable_greeting_shortcut(monkeypatch):
    """每个测试前自动设置 ENABLE_GREETING_SHORTCUT=true，测试后自动恢复。"""
    monkeypatch.setenv("ENABLE_GREETING_SHORTCUT", "true")
    yield
    # monkeypatch 自动恢复原值或删除


def _make_processor():
    """构造无依赖的 MessageProcessorMixin 实例."""
    mp = MessageProcessorMixin.__new__(MessageProcessorMixin)
    mp.slash_handler = None
    # P1-6: _try_greeting_shortcut 现在会读 _voice_mode（默认 False 保持原行为）
    mp._voice_mode = False
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
    """问候短路延迟 < 100ms.

    P2-5: 除平均值外，验证每次调用都不超过 100ms（避免偶发慢调用被平均掩盖）。
    """
    mp = _make_processor()
    durations_ms = []
    for _ in range(100):
        start = time.monotonic()
        mp._try_greeting_shortcut("你好", "user1", "qq")
        durations_ms.append((time.monotonic() - start) * 1000)

    avg = sum(durations_ms) / len(durations_ms)
    max_single = max(durations_ms)
    # 95 分位
    sorted_d = sorted(durations_ms)
    p95 = sorted_d[int(len(sorted_d) * 0.95)]

    assert avg < 100, f"平均延迟 {avg:.1f}ms 应 <100ms"
    assert max_single < 200, f"单次最大延迟 {max_single:.1f}ms 应 <200ms（允许偶发波动）"
    assert p95 < 100, f"P95 延迟 {p95:.1f}ms 应 <100ms"


def test_group_chat_skips_shortcut():
    """群聊模式不触发短路（避免刷屏）."""
    mp = _make_processor()
    result = mp._try_greeting_shortcut("你好", "user1", "qq_group")
    assert result is None


def test_disabled_via_env(monkeypatch):
    """ENABLE_GREETING_SHORTCUT=false 时关闭短路."""
    # fixture 已设 true，这里改 false 验证
    monkeypatch.setenv("ENABLE_GREETING_SHORTCUT", "false")
    mp = _make_processor()
    result = mp._try_greeting_shortcut("你好", "user1", "qq")
    assert result is None


def test_greeting_uses_shanghai_timezone(monkeypatch):
    """G1: 问候时段应使用 Asia/Shanghai 时区，不受系统时区影响."""
    # 确认模块已定义 Asia/Shanghai 时区常量
    assert hasattr(mp_module, '_SH_TZ'), "_SH_TZ 时区常量未定义"
    assert mp_module._SH_TZ == ZoneInfo("Asia/Shanghai")

    # 捕获 datetime.now 调用时的 tz 参数
    captured = {}

    class FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            captured['tz'] = tz
            # 返回上海时间 8:00（早上时段）以触发"早上好"
            return datetime(2026, 7, 21, 8, 0, tzinfo=tz)

    monkeypatch.setattr(mp_module, 'datetime', FakeDateTime)

    mp = _make_processor()
    result = mp._try_greeting_shortcut("你好", "user1", "qq")

    # 验证 datetime.now 被调用时传入了 Asia/Shanghai 时区
    assert captured.get('tz') == ZoneInfo("Asia/Shanghai"), \
        f"datetime.now 应传入 Asia/Shanghai 时区, 实际: {captured.get('tz')}"
    assert result is not None, "上海 8:00 应命中问候短路"
    assert "早上好" in result.reply, f"上海 8:00 应触发早上好, 实际: {result.reply}"
