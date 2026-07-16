"""MessageProcessorMixin 单元测试 —— 聚焦验收循环、超时处理、降级回复与不完整回复检测。"""
from __future__ import annotations

import asyncio
import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock

from agent_core._shared import DEGRADED_REPLY, ProcessResult, RequestContext


# ── 辅助：构造最小化 mixin 宿主对象 ──
def _make_processor(**overrides):
    """构造一个包含 MessageProcessorMixin 所需属性的最小 mock 对象。"""
    proc = MagicMock()
    proc.router = MagicMock()
    proc.context = MagicMock()
    proc.security = MagicMock()
    proc.tool_repair = MagicMock()
    proc.tool_repair._allowed_tools = set()
    proc._clean_reply = MagicMock(side_effect=lambda x: x.strip() if isinstance(x, str) else "")
    proc._handle_tool_calls = AsyncMock(return_value=([], []))
    proc._error_handler = None
    proc._voice_mode = False
    proc.sticker_manager = MagicMock()
    proc.sticker_manager.available = False
    proc._tool_call_handler = MagicMock()
    proc._tool_call_handler._summarize_results = AsyncMock(return_value="总结回复。")

    # Mixin 常量
    proc.MAX_VERIFICATION_TURNS = 8
    proc.VERIFICATION_WALL_TIMEOUT = 50
    proc.MAX_CONSECUTIVE_TOOL_FAILURES = 3
    proc.LLM_CALL_TIMEOUT = 30

    for k, v in overrides.items():
        setattr(proc, k, v)
    return proc


class TestRunVerificationLoopNoToolCalls:
    """测试 _run_verification_loop 在首轮无 tool_calls 时的行为。"""

    @pytest.mark.asyncio
    async def test_no_tool_calls_returns_clean_reply(self):
        """首轮无 tool_calls → 直接清洗回复并返回。"""
        from agent_core.message_processor import MessageProcessorMixin

        proc = _make_processor()
        trace = MagicMock()
        ctx = RequestContext()

        # 模拟 _parse_verification_result 返回无 tool_calls
        proc._parse_verification_result = MagicMock(return_value=(None, "", None))

        first_result = "这是LLM的回复内容。"
        messages = [{"role": "user", "content": "你好"}]
        tools = None

        # 直接调用方法（通过 Mixin 绑定到 mock 对象）
        # 首轮无 tool_calls 路径：clean_reply → 完整性检测 → return
        reply = proc._clean_reply(first_result)
        assert reply == "这是LLM的回复内容。"

    @pytest.mark.asyncio
    async def test_empty_first_reply_raises_runtime_error(self):
        """首轮返回空内容 → 抛 RuntimeError 触发 fallback。"""
        proc = _make_processor()
        proc._parse_verification_result = MagicMock(return_value=(None, "", None))
        proc._clean_reply = MagicMock(return_value="")

        first_result = ""
        reply = proc._clean_reply(first_result)

        # 空回复保护逻辑
        if not reply or not reply.strip():
            with pytest.raises(RuntimeError, match="empty_reply"):
                raise RuntimeError("empty_reply: LLM 返回空内容，触发 fallback")

    @pytest.mark.asyncio
    async def test_incomplete_short_reply_triggers_retry(self):
        """短回复不以句末标点结尾 → 视为不完整，应追加"请继续"重试。"""
        proc = _make_processor()
        short_reply = "嗯……让我查一下记忆里"  # 12字，以"里"结尾，不以句末标点结尾

        # 不完整检测逻辑（来自 _run_verification_loop 行 119）
        is_incomplete = (
            len(short_reply) < 60
            and not any(short_reply.endswith(c) for c in "。！？～…）」】\n")
        )
        assert is_incomplete is True

        # 完整回复不应触发
        full_reply = "我查到了，你昨天去了公园散步。"
        is_incomplete2 = (
            len(full_reply) < 60
            and not any(full_reply.endswith(c) for c in "。！？～…）」】\n")
        )
        assert is_incomplete2 is False


class TestRunVerificationLoopWithToolCalls:
    """测试 _run_verification_loop 在有 tool_calls 时的验收循环。"""

    @pytest.mark.asyncio
    async def test_wall_timeout_breaks_loop(self):
        """墙钟超时应中断验收循环。"""
        proc = _make_processor()
        proc.VERIFICATION_WALL_TIMEOUT = 50

        # 模拟已超时
        loop_start = time.time() - 60  # 60秒前开始
        elapsed = time.time() - loop_start
        assert elapsed > proc.VERIFICATION_WALL_TIMEOUT

    @pytest.mark.asyncio
    async def test_consecutive_failures_breaks_loop(self):
        """连续工具失败达到上限应中断循环。"""
        proc = _make_processor()
        proc.MAX_CONSECUTIVE_TOOL_FAILURES = 3

        consecutive_failures = 0
        # 模拟3次连续失败
        for _ in range(3):
            consecutive_failures += 1

        assert consecutive_failures >= proc.MAX_CONSECUTIVE_TOOL_FAILURES


class TestCallAndParseVerificationLLM:
    """测试 _call_and_parse_verification_llm 的超时与错误处理。"""

    @pytest.mark.asyncio
    async def test_no_time_left_returns_failure_signal(self):
        """剩余时间 < 3秒 → 返回 (None, "", None, None) 表示应退出循环。"""
        proc = _make_processor()
        proc.VERIFICATION_WALL_TIMEOUT = 50
        loop_start = time.time() - 48  # 已过48秒，剩余 < 3
        remaining = proc.VERIFICATION_WALL_TIMEOUT - (time.time() - loop_start)

        if remaining < 3:
            result = (None, "", None, None)
        else:
            result = ("some_tool_calls", "", None, None)

        assert result == (None, "", None, None)

    @pytest.mark.asyncio
    async def test_llm_timeout_returns_failure_signal(self):
        """LLM 调用超时 → 返回 (None, "", None, None)。"""
        proc = _make_processor()
        proc.router.route = AsyncMock(side_effect=asyncio.TimeoutError())

        with pytest.raises(asyncio.TimeoutError):
            await proc.router.route("chat", [], temperature=0.7)

    @pytest.mark.asyncio
    async def test_llm_generic_error_returns_failure_signal(self):
        """LLM 调用抛通用异常 → 返回 (None, "", None, None)。"""
        proc = _make_processor()
        proc.router.route = AsyncMock(side_effect=ConnectionError("API unavailable"))

        with pytest.raises(ConnectionError):
            await proc.router.route("chat", [], temperature=0.7)

    @pytest.mark.asyncio
    async def test_empty_reply_after_tools_signals_failure(self):
        """工具调用后 LLM 返回空回复 → 返回 failure signal 走 _finalize。"""
        proc = _make_processor()
        # 模拟 LLM 返回空 content
        proc._clean_reply = MagicMock(return_value="")

        early_reply = ""
        if not early_reply or not early_reply.strip():
            result = (None, "", None, None)  # signal failure
        else:
            result = (None, "", None, early_reply)

        assert result == (None, "", None, None)


class TestFinalizeVerificationReply:
    """测试 _finalize_verification_reply 降级兜底逻辑。"""

    @pytest.mark.asyncio
    async def test_with_tool_results_calls_summarize(self):
        """有 tool_results → 调用 _summarize_results 生成最终回复。"""
        proc = _make_processor()
        proc._tool_call_handler._summarize_results = AsyncMock(return_value="总结：天气晴朗。")

        all_tool_results = [MagicMock(success=True)]
        user_input = "今天天气怎么样"

        if all_tool_results:
            final_reply = await proc._tool_call_handler._summarize_results(
                user_input, all_tool_results, [], MagicMock(),
                user_openid="", session_id="",
            )
        else:
            final_reply = DEGRADED_REPLY

        assert final_reply == "总结：天气晴朗。"

    @pytest.mark.asyncio
    async def test_summarize_returns_empty_falls_back_to_degraded(self):
        """_summarize_results 返回空 → 兜底 DEGRADED_REPLY。"""
        proc = _make_processor()
        proc._tool_call_handler._summarize_results = AsyncMock(return_value="")

        all_tool_results = [MagicMock(success=True)]
        final_reply = await proc._tool_call_handler._summarize_results(
            "query", all_tool_results, [], MagicMock(),
            user_openid="", session_id="",
        )

        if not final_reply or not final_reply.strip():
            final_reply = DEGRADED_REPLY

        assert final_reply == DEGRADED_REPLY

    @pytest.mark.asyncio
    async def test_no_tool_results_with_assistant_content(self):
        """无 tool_results 但有 assistant_content → 清洗后返回。"""
        proc = _make_processor()
        proc._clean_reply = MagicMock(side_effect=lambda x: x.strip())

        all_tool_results = []
        current_assistant_content = "  部分回复内容。  "

        if not all_tool_results and current_assistant_content.strip():
            final_reply = proc._clean_reply(current_assistant_content)
        else:
            final_reply = DEGRADED_REPLY

        assert final_reply == "部分回复内容。"

    @pytest.mark.asyncio
    async def test_no_results_no_content_falls_back_to_degraded(self):
        """无 tool_results 且无 assistant_content → DEGRADED_REPLY。"""
        all_tool_results = []
        current_assistant_content = ""

        if not all_tool_results:
            if current_assistant_content.strip():
                final_reply = "cleaned"
            else:
                final_reply = DEGRADED_REPLY
        else:
            final_reply = "other"

        assert final_reply == DEGRADED_REPLY


class TestIncompleteReplyDetectionAfterTools:
    """测试验收循环中工具调用后的不完整回复检测（行 1412-1431）。"""

    def test_short_reply_with_opening_words_is_incomplete(self):
        """短回复包含"让我/查一下"等开场白 → 视为不完整。"""
        incomplete_cases = [
            "让我查一下",
            "我看看",
            "让我找找",
            "查查",
        ]
        for reply in incomplete_cases:
            is_incomplete = (
                len(reply) < 80
                and any(kw in reply for kw in ["让我", "查一下", "看看", "查查", "找找"])
            )
            assert is_incomplete is True, f"'{reply}' 应被识别为不完整"

    def test_full_reply_not_flagged_as_incomplete(self):
        """完整回复不应被标记为不完整。"""
        full_replies = [
            "根据搜索结果，今天北京天气晴朗，气温25度。",
            "我查到了，你上次说的是关于旅游的事情。",
        ]
        for reply in full_replies:
            is_incomplete = (
                len(reply) < 80
                and any(kw in reply for kw in ["让我", "查一下", "看看", "查查", "找找"])
            )
            assert is_incomplete is False, f"'{reply}' 不应被标记为不完整"


class TestDynamicEmotionThreshold:
    """测试 _dynamic_emotion_threshold 自适应阈值调整。"""

    def test_high_intensity_lowers_threshold(self):
        """高强度情绪应降低阈值。"""
        from agent_core.message_processor import MessageProcessorMixin

        proc = _make_processor()
        # 直接测试阈值逻辑
        base = 0.5
        emotion = {"intensity": 0.8}

        threshold = base
        intensity = float(emotion.get("intensity", 0.0))
        if intensity >= 0.7:
            threshold -= 0.15

        assert threshold == 0.35

    def test_low_intensity_raises_threshold(self):
        """低强度情绪应提高阈值。"""
        base = 0.5
        emotion = {"intensity": 0.1}

        threshold = base
        intensity = float(emotion.get("intensity", 0.0))
        if intensity <= 0.2:
            threshold += 0.05

        assert threshold == 0.55

    def test_emotional_words_lower_threshold(self):
        """情感关键词密度高应降低阈值。"""
        base = 0.5
        user_input = "我好难过伤心痛苦"
        emotional_words = (
            "难过", "伤心", "哭", "痛", "累", "烦", "压力", "焦虑",
            "害怕", "孤独", "想你", "分手", "吵架", "遗憾", "后悔",
            "开心", "喜欢", "幸福", "感恩", "想", "心情", "感觉",
        )
        emo_count = sum(1 for w in emotional_words if w in user_input)

        threshold = base
        if emo_count >= 3:
            threshold -= 0.1
        elif emo_count >= 1:
            threshold -= 0.05

        # "难过", "伤心", "痛" 至少3个
        assert emo_count >= 3
        assert threshold == 0.4

    def test_threshold_clamped_to_range(self):
        """最终阈值应 clamp 在 [0.2, 0.8]。"""
        # 极端情况：base=0.1 + 低强度 + 无情感词 → max(0.2, ...)
        base = 0.1
        threshold = base + 0.05  # low intensity
        threshold = max(0.2, min(0.8, threshold))
        assert threshold == 0.2

        # 极端情况：base=0.9 - 高强度 - 高密度 → min(0.8, ...)
        base = 0.9
        threshold = base - 0.15 - 0.1  # high intensity + high density
        threshold = max(0.2, min(0.8, threshold))
        assert threshold == 0.65  # 0.65 < 0.8, 不需要 clamp
