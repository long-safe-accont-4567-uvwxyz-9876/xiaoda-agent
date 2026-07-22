"""测试并行子代理表情包选择 — 确保使用子代理专属表情包管理器。

验证 _finalize_parallel_reply 不再始终使用 xiaoda 的 sticker_manager，
而是优先使用子代理的专属表情包管理器。
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_core._shared import RequestContext
from agent_core.sub_agent_manager import SubAgentManagerMixin


class FakeStickerManager:
    """模拟表情包管理器"""

    def __init__(self, name: str, available: bool = True, sticker_path: str | None = None):
        self.name = name
        self._available = available
        self._sticker_path = Path(sticker_path) if sticker_path else None
        self.detect_called = False
        self.pick_called = False

    @property
    def available(self) -> bool:
        return self._available

    def detect_emotion(self, reply: str) -> str:
        self.detect_called = True
        if "[emotion:happy]" in reply:
            return "happy"
        return ""

    def should_send(self, *args, **kwargs) -> bool:
        return True

    def pick(self, emotion: str, strict: bool = False) -> Path | None:
        self.pick_called = True
        return self._sticker_path

    def strip_emotion_tag(self, text: str) -> str:
        import re
        return re.sub(r'\[emotion:[^\]]*\]', '', text).rstrip()


class StubForParallelFinalize:
    """桩对象：仅实现 _finalize_parallel_reply 依赖的方法。"""

    def __init__(self):
        self.sticker_manager = FakeStickerManager("xiaoda", available=True,
                                                   sticker_path="/tmp/xiaoda_sticker.png")
        self._sub_sticker_managers: dict[str, FakeStickerManager] = {}
        self._voice_mode = False
        self._bg_task_manager = MagicMock()
        self._bg_task_manager.run_background_tasks = MagicMock()
        self.tts = MagicMock()
        self.tts.available = False
        self._error_classifier = MagicMock()
        self._error_classifier.classify = MagicMock(return_value=MagicMock(
            reason=MagicMock(value="test"), action=MagicMock(value="test"), is_retryable=False))

    def get_sticker_manager(self, name: str):
        if name in self._sub_sticker_managers:
            return self._sub_sticker_managers[name]
        return self.sticker_manager

    def get_sticker_info(self, reply: str, user_emotion: str = "",
                         force_sticker: bool = False):
        """旧路径：使用 xiaoda 的 sticker_manager"""
        clean = self.sticker_manager.strip_emotion_tag(reply)
        return clean, None

    def _finalize_reply(self, reply: str, style: str = "xiaoda") -> str:
        return reply

    def _clean_reply(self, text: str) -> str:
        return text

    def register_sub_sticker_manager(self, name: str, mgr: FakeStickerManager):
        self._sub_sticker_managers[name] = mgr


# 将 StubForParallelFinalize 与 SubAgentManagerMixin 混合
class ParallelStickerTestHarness(StubForParallelFinalize, SubAgentManagerMixin):
    pass


@pytest.fixture
def ctx() -> RequestContext:
    return RequestContext()


@pytest.mark.asyncio
async def test_parallel_uses_sub_agent_sticker_manager(ctx):
    """并行模式下应使用子代理的专属表情包管理器，而非 xiaoda 的。"""
    mgr = ParallelStickerTestHarness()

    # 设置子代理 xiaoli 的专属表情包管理器
    xiaoli_mgr = FakeStickerManager("xiaoli", available=True,
                                     sticker_path="/tmp/xiaoli_sticker.png")
    mgr.register_sub_sticker_manager("xiaoli", xiaoli_mgr)

    intermediate = [
        {"agent": "xiaoli", "display_name": "小莉", "reply": "你好呀[emotion:happy]"},
    ]
    all_replies = "【小莉】\n你好呀[emotion:happy]"

    # 模拟 detect_emotion
    with patch('agent_core.sub_agent_manager.detect_emotion') as mock_detect:
        mock_detect.return_value = {"primary": "happy"}

        result = await mgr._finalize_parallel_reply(
            all_replies, "你好", "u1", "qq", "s1", False, ctx, intermediate=intermediate
        )

    # xiaoli 的表情包管理器应该被调用
    assert xiaoli_mgr.detect_called, "xiaoli 的 sticker_manager.detect_emotion 应被调用"
    assert xiaoli_mgr.pick_called, "xiaoli 的 sticker_manager.pick 应被调用"
    # 结果应包含 xiaoli 的表情包路径
    assert result.sticker_path is not None
    assert "xiaoli" in str(result.sticker_path)


@pytest.mark.asyncio
async def test_parallel_fallback_to_xiaoda_when_sub_unavailable(ctx):
    """子代理表情包不可用时降级到 xiaoda 的表情包管理器。"""
    mgr = ParallelStickerTestHarness()

    # xiaoli 的表情包管理器不可用
    xiaoli_mgr = FakeStickerManager("xiaoli", available=False)
    mgr.register_sub_sticker_manager("xiaoli", xiaoli_mgr)

    intermediate = [
        {"agent": "xiaoli", "display_name": "小莉", "reply": "你好呀[emotion:happy]"},
    ]
    all_replies = "【小莉】\n你好呀[emotion:happy]"

    with patch('agent_core.sub_agent_manager.detect_emotion') as mock_detect:
        mock_detect.return_value = {"primary": "happy"}

        _result = await mgr._finalize_parallel_reply(
            all_replies, "你好", "u1", "qq", "s1", False, ctx, intermediate=intermediate
        )

    # 降级路径：get_sticker_info 应被调用
    # （get_sticker_info 内部使用 xiaoda 的 sticker_manager）
    assert not xiaoli_mgr.pick_called, "xiaoli 不可用时不应 pick"


@pytest.mark.asyncio
async def test_parallel_no_sticker_when_all_unavailable(ctx):
    """所有表情包管理器都不可用时，不返回表情包。"""
    mgr = ParallelStickerTestHarness()
    # 覆盖默认 xiaoda sticker_manager 为不可用
    mgr.sticker_manager = FakeStickerManager("xiaoda", available=False)

    intermediate = [
        {"agent": "xiaoli", "display_name": "小莉", "reply": "你好呀[emotion:happy]"},
    ]
    all_replies = "【小莉】\n你好呀[emotion:happy]"

    with patch('agent_core.sub_agent_manager.detect_emotion') as mock_detect:
        mock_detect.return_value = {"primary": "happy"}

        result = await mgr._finalize_parallel_reply(
            all_replies, "你好", "u1", "qq", "s1", False, ctx, intermediate=intermediate
        )

    assert result.sticker_path is None


@pytest.mark.asyncio
async def test_parallel_curious_emotion_maps_to_curious_not_confused(ctx):
    """并行路径中 好奇 情绪应映射为 curious 而非 confused。

    BUG: 并行路径使用 CN_TO_EN_MAP（emotional_memory）而非 CN_TO_EN（emotion_enum），
    CN_TO_EN_MAP 将 好奇 映射为 confused，但 sticker_manager 支持 curious 不支持 confused，
    导致好奇情绪的表情包永远选不到。
    """
    mgr = ParallelStickerTestHarness()

    # 子代理 sticker_manager：detect_emotion 返回空（触发 fallback），
    # 但 pick 只支持 curious 不支持 confused
    class CuriousOnlyStickerManager(FakeStickerManager):
        def detect_emotion(self, reply: str) -> str:
            return ""  # 强制走 fallback 路径

        def pick(self, emotion: str, strict: bool = False):
            self.pick_called = True
            self.last_pick_emotion = emotion
            if emotion == "curious":
                return Path("/tmp/xiaoli_curious.png")
            return None  # confused 等其他标签不支持

    xiaoli_mgr = CuriousOnlyStickerManager("xiaoli", available=True,
                                             sticker_path="/tmp/xiaoli_curious.png")
    mgr.register_sub_sticker_manager("xiaoli", xiaoli_mgr)

    intermediate = [
        {"agent": "xiaoli", "display_name": "小莉", "reply": "咦？这是什么呢"},
    ]
    all_replies = "【小莉】\n咦？这是什么呢"

    # detect_emotion 返回 "好奇" 作为 primary
    with patch('agent_core.sub_agent_manager.detect_emotion') as mock_detect:
        mock_detect.return_value = {"primary": "好奇"}

        result = await mgr._finalize_parallel_reply(
            all_replies, "这是什么", "u1", "qq", "s1", False, ctx, intermediate=intermediate
        )

    # 好奇应映射为 curious（sticker_manager 支持），而非 confused（不支持）
    assert hasattr(xiaoli_mgr, 'last_pick_emotion'), "pick 应被调用"
    assert xiaoli_mgr.last_pick_emotion == "curious", \
        f"好奇应映射为curious 实际映射为: {xiaoli_mgr.last_pick_emotion}"
    assert result.sticker_path is not None, "应返回好奇表情包"
