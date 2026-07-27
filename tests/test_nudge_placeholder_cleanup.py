"""Bug 3 (P0-3): 主动问候占位符污染内存 history

根因：nudge_engine 用 user_input="（主动问候）" 调用 process()，
  _finalize_main_reply 无条件把 user_input 追加到 context.history，
  下一条真实消息看到"用户之前说了（主动问候）"。
  background_tasks.py 写 DB 时清空占位符，但内存 history 未清。

修复：在 _process_impl 结束时，如果 system_context 非空（内部场景），
  从 context.history 弹出刚追加的占位符 user 消息。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core._shared import ProcessResult, RequestContext
from agent_core.message_processor import MessageProcessorMixin


def _make_processor_with_history():
    """构造带真实 history 列表的 processor mock。"""
    proc = MagicMock()
    proc.context = MagicMock()
    proc.context.history = []

    async def fake_add_message(role, content, **kwargs):
        proc.context.history.append({"role": role, "content": str(content)})
    proc.context.add_message = fake_add_message
    proc.context.current_address_term = "爸爸"

    proc.security = MagicMock()
    proc.security.is_allowed = MagicMock(return_value=(True, ""))
    proc._tool_call_handler = MagicMock()
    proc._tool_call_handler._tool_repair = MagicMock()
    proc.db = None  # 跳过 restore_from_db
    proc.slash_handler = None
    proc._voice_mode = False
    proc.tts = MagicMock()
    proc.tts.available = False
    proc.sticker_manager = MagicMock()
    proc.sticker_manager.available = False
    proc.router = MagicMock()
    proc._error_handler = None
    proc._bg_task_manager = MagicMock()
    proc._circuit_breaker = MagicMock()
    proc._cognitive_state = MagicMock()
    proc._hook_engine = MagicMock()
    proc._hook_engine.fire_post_response = AsyncMock()
    proc._chat_target_lock = asyncio.Lock()
    proc._user_chat_target = {}
    proc._router_engine = MagicMock()
    proc._tool_call_handler = MagicMock()

    return proc


def _mock_pre_main_path(proc):
    """Mock _process_impl 中 _run_main_process_path 之前的所有依赖。"""
    proc._init_and_restore_context = AsyncMock(
        return_value=(MagicMock(), "session-1", True, ""))
    proc._try_greeting_shortcut = MagicMock(return_value=None)
    proc._parse_chat_target = AsyncMock(return_value=[])
    proc._detect_voice_intent = MagicMock(return_value="none")
    proc.set_voice_mode = MagicMock()


@pytest.mark.asyncio
async def test_internal_scenario_placeholder_removed_from_history():
    """内部场景（system_context 非空）→ 占位符应从 history 清除。

    模拟 nudge_engine 调用 process(user_input="（主动问候）", system_context=scene_prompt)。
    _finalize_main_reply 会把占位符追加到 history，_process_impl 应在 return 前清除它。
    """
    proc = _make_processor_with_history()
    _mock_pre_main_path(proc)

    placeholder = "（主动问候）"
    greeting_reply = "早上好～新的一天开始啦！"

    async def mock_run_main(self, ctx, user_input, clean_input, user_id, source,
                            user_openid, session_id, status_callback, image_data,
                            is_master, force_voice, chat_targets, trace):
        # 模拟 _finalize_main_reply 的行为：记录 len_before + 追加占位符 + 追加回复
        try:
            from agent_core.message_processor import _history_len_before_placeholder_var
            _history_len_before_placeholder_var.set(len(self.context.history))
        except (ImportError, AttributeError):
            pass  # 修复前：变量不存在，跳过记录
        self.context.history.append({"role": "user", "content": user_input})
        self.context.history.append({"role": "assistant", "content": greeting_reply})
        return ProcessResult(reply=greeting_reply)

    # 绑定 mock 到 proc
    proc._run_main_process_path = lambda *a, **kw: mock_run_main(proc, *a, **kw)

    ctx = RequestContext()
    result = await MessageProcessorMixin._process_impl(
        proc, ctx, placeholder, "qq_123", "qq", "123", "session-1",
        None, None, is_master=True, system_context="（场景：早上好）",
    )

    # 占位符不应残留在 history 中
    user_msgs = [m for m in proc.context.history if m.get("role") == "user"]
    placeholder_msgs = [m for m in user_msgs if placeholder in m.get("content", "")]
    assert len(placeholder_msgs) == 0, (
        f"内部场景的占位符 '{placeholder}' 应从 history 清除，"
        f"但 history 中仍有：{proc.context.history}"
    )

    # 助手回复应保留（agent 记得自己说过问候）
    asst_msgs = [m for m in proc.context.history if m.get("role") == "assistant"]
    assert len(asst_msgs) == 1, f"助手回复应保留，history={proc.context.history}"
    assert greeting_reply in asst_msgs[0]["content"]


@pytest.mark.asyncio
async def test_normal_user_message_kept_in_history():
    """正常用户消息（system_context 为空）→ user_input 应保留在 history 中。"""
    proc = _make_processor_with_history()
    _mock_pre_main_path(proc)

    user_msg = "你好呀，今天天气怎么样？"
    reply = "今天天气挺好的～"

    async def mock_run_main(self, ctx, user_input, clean_input, user_id, source,
                            user_openid, session_id, status_callback, image_data,
                            is_master, force_voice, chat_targets, trace):
        try:
            from agent_core.message_processor import _history_len_before_placeholder_var
            _history_len_before_placeholder_var.set(len(self.context.history))
        except (ImportError, AttributeError):
            pass
        self.context.history.append({"role": "user", "content": user_input})
        self.context.history.append({"role": "assistant", "content": reply})
        return ProcessResult(reply=reply)

    proc._run_main_path = lambda *a, **kw: mock_run_main(proc, *a, **kw)
    proc._run_main_process_path = lambda *a, **kw: mock_run_main(proc, *a, **kw)

    ctx = RequestContext()
    result = await MessageProcessorMixin._process_impl(
        proc, ctx, user_msg, "qq_123", "qq", "123", "session-1",
        None, None, is_master=True, system_context="",  # 正常用户消息
    )

    # 正常用户消息应保留在 history 中
    user_msgs = [m for m in proc.context.history if m.get("role") == "user"]
    assert len(user_msgs) == 1, f"正常用户消息应保留，history={proc.context.history}"
    assert user_msg in user_msgs[0]["content"]
