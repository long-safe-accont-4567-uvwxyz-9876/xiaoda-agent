"""Bug 4 (P1-4): 主动问候提前截断

根因：
  1. nudge_engine.py:357 asyncio.wait_for(self._core.process(...), timeout=30) → 30s 超时截断
  2. message_processor 的 is_reply_likely_complete 对短回复判定"完整"直接 force_close

修复：
  1. timeout=30 → timeout=90
  2. 内部场景（system_context 非空）跳过提前完成判定
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from agent_core._shared import RequestContext
from agent_core.message_processor import MessageProcessorMixin, _system_context_var


class TestNudgeTimeout:
    """测试 nudge_engine 主动问候超时从 30s 提升到 90s。"""

    def test_nudge_process_timeout_is_90(self):
        """_generate_idle_greeting 中 self._core.process 的 timeout 应为 90（非 30）。"""
        from emotion.nudge_engine import NudgeEngine

        source = inspect.getsource(NudgeEngine._generate_idle_greeting)
        # 主动问候走 self._core.process 路径，timeout 应为 90
        assert "timeout=90" in source, (
            "nudge_engine._generate_idle_greeting 中 self._core.process 的 timeout 应为 90s"
            "（与主路径 180s 对齐的一半），当前源码未包含 timeout=90"
        )

    def test_nudge_process_timeout_not_30(self):
        """确保 self._core.process 调用的 timeout 不再是 30。"""
        from emotion.nudge_engine import NudgeEngine

        source = inspect.getsource(NudgeEngine._generate_idle_greeting)
        # 找到 self._core.process 附近的 timeout
        # process 调用块中不应有 timeout=30
        _process_idx = source.find("self._core.process")
        assert _process_idx != -1, "源码中应包含 self._core.process 调用"
        # 截取 process 调用到下一个 timeout 之间的片段
        _segment = source[_process_idx:_process_idx + 500]
        assert "timeout=30" not in _segment, (
            "self._core.process 调用的 timeout 不应为 30（会导致主动问候被提前截断）"
        )


class TestInternalScenarioSkipsEarlyComplete:
    """测试内部场景（system_context 非空）跳过提前完成判定。"""

    @pytest.mark.asyncio
    async def test_internal_scenario_short_reply_not_force_closed(self):
        """内部场景：短回复不以标点结尾 → 不应追加"。"（跳过 force_close）。

        正常场景下，"早上好" 不以合法句末标记结尾 → force_close 追加 "。" → "早上好。"
        内部场景（system_context 非空）应跳过提前完成判定，返回原文 "早上好"。
        """
        from agent_core.message_processor import _stream_finish_reason_var

        proc = MagicMock()
        proc._parse_verification_result = MagicMock(return_value=(None, "", None))
        proc._clean_reply = MagicMock(side_effect=lambda x: x.strip() if isinstance(x, str) else "")
        # 首轮无 tool_calls 分支已抽取为独立方法，需绑定真实实现
        proc._finalize_reply_without_tools = MessageProcessorMixin._finalize_reply_without_tools.__get__(proc)
        proc._retry_verification_reply = MessageProcessorMixin._retry_verification_reply.__get__(proc)
        proc._handle_empty_reply = MessageProcessorMixin._handle_empty_reply.__get__(proc)

        # 设置 finish_reason="stop"（避免触发 length/no_finish 重试）
        _stream_finish_reason_var.set("stop")
        # 设置 system_context 非空（内部场景）
        _sys_token = _system_context_var.set("（场景：早上好，主动问候）")

        try:
            reply, tool_results = await MessageProcessorMixin._run_verification_loop(
                proc, "早上好", [], None, MagicMock(),
                task_type="chat", temperature=0.7, max_tokens=None,
                user_openid="123", session_id="s1", is_owner=True,
                ctx=RequestContext(), user_input="（主动问候）",
            )
        finally:
            _system_context_var.reset(_sys_token)
            _stream_finish_reason_var.set(None)

        # 内部场景：不应追加"。"，返回原文
        assert reply == "早上好", (
            f"内部场景短回复应原样返回 '早上好'，实际为 '{reply}'"
            f"（不应被 force_close 追加 '。'）"
        )

    @pytest.mark.asyncio
    async def test_normal_scenario_short_reply_force_closed(self):
        """正常场景：短回复不以标点结尾 → 应追加"。"（原有 force_close 行为不变）。

        确保 Bug 4 修复不影响正常用户消息的完整性判定。
        """
        from agent_core.message_processor import _stream_finish_reason_var

        proc = MagicMock()
        proc._parse_verification_result = MagicMock(return_value=(None, "", None))
        proc._clean_reply = MagicMock(side_effect=lambda x: x.strip() if isinstance(x, str) else "")
        # 首轮无 tool_calls 分支已抽取为独立方法，需绑定真实实现
        proc._finalize_reply_without_tools = MessageProcessorMixin._finalize_reply_without_tools.__get__(proc)
        proc._retry_verification_reply = MessageProcessorMixin._retry_verification_reply.__get__(proc)
        proc._handle_empty_reply = MessageProcessorMixin._handle_empty_reply.__get__(proc)

        _stream_finish_reason_var.set("stop")
        # 正常场景：system_context 为空
        _sys_token = _system_context_var.set("")

        try:
            reply, tool_results = await MessageProcessorMixin._run_verification_loop(
                proc, "早上好", [], None, MagicMock(),
                task_type="chat", temperature=0.7, max_tokens=None,
                user_openid="123", session_id="s1", is_owner=True,
                ctx=RequestContext(), user_input="你好",
            )
        finally:
            _system_context_var.reset(_sys_token)
            _stream_finish_reason_var.set(None)

        # 正常场景：force_close 追加 "。"
        assert reply == "早上好。", (
            f"正常场景短回复应 force_close 为 '早上好。'，实际为 '{reply}'"
            f"（Bug 4 修复不应影响正常消息的完整性判定）"
        )
