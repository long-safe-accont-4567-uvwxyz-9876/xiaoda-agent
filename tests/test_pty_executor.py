"""PTY 执行器线程安全测试

覆盖缺陷：Windows pipe reader 后台线程直接调用 asyncio.Event.set()
导致竞态条件 / 事件丢失 / 事件循环损坏。
"""
import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from web.pty_executor import CommandState, _safe_set_event, feed_output


class TestSafeSetEvent:
    """_safe_set_event 线程安全性测试"""

    async def test_safe_set_event_from_event_loop_thread(self):
        """在事件循环线程内调用应通过 call_soon_threadsafe 设置事件"""
        state = CommandState(marker_id="test", loop=asyncio.get_running_loop())
        _safe_set_event(state)
        # call_soon_threadsafe 将回调放入队列，需让出一次事件循环才能生效
        await asyncio.sleep(0)
        assert state.event.is_set()

    async def test_safe_set_event_from_background_thread(self):
        """从后台线程调用应通过 call_soon_threadsafe 安全设置事件"""
        loop = asyncio.get_running_loop()
        state = CommandState(marker_id="test", loop=loop)

        def _bg():
            _safe_set_event(state)

        t = threading.Thread(target=_bg)
        t.start()
        t.join(timeout=2)

        # 让事件循环处理通过 call_soon_threadsafe 投递的回调
        await asyncio.sleep(0)
        assert state.event.is_set()

    async def test_safe_set_event_without_loop_fallback(self):
        """loop 为 None 时应回退到直接设置（兼容旧路径）"""
        state = CommandState(marker_id="test", loop=None)
        _safe_set_event(state)
        assert state.event.is_set()


class TestFeedOutputThreadSafety:
    """feed_output 从后台线程调用的线程安全性测试"""

    @pytest.fixture(autouse=True)
    def _clear_pending(self):
        """每个用例前清理全局 _pending_cmd"""
        import web.pty_executor as _mod
        with _mod._pending_lock:
            _mod._pending_cmd = None
        yield
        with _mod._pending_lock:
            _mod._pending_cmd = None

    async def test_feed_output_from_background_thread_sets_event(self):
        """模拟 Windows pipe reader：后台线程调用 feed_output 应正确触发事件"""
        import web.pty_executor as _mod

        loop = asyncio.get_running_loop()
        state = CommandState(marker_id="abc123", loop=loop)
        with _mod._pending_lock:
            _mod._pending_cmd = state

        # 先发送开始标记，使 collecting=True
        feed_output("_A_abc123_\n")
        assert state.collecting is True

        # 模拟后台线程推送包含结束标记的输出
        end_marker = f"\033[2m\033[38;2;6;14;10m_Z_abc123_\033[0m0\n"

        def _bg():
            feed_output(end_marker)

        t = threading.Thread(target=_bg)
        t.start()
        t.join(timeout=2)

        # 让事件循环处理 call_soon_threadsafe 投递的 set()
        await asyncio.sleep(0)
        assert state.event.is_set()
        assert state.exit_code == 0

    async def test_feed_output_cross_chunk_end_marker_from_thread(self):
        """跨 chunk 的结束标记从后台线程调用也应正确触发"""
        import web.pty_executor as _mod

        loop = asyncio.get_running_loop()
        state = CommandState(marker_id="xyz789", loop=loop)
        with _mod._pending_lock:
            _mod._pending_cmd = state

        # 先推送开始标记和部分输出
        feed_output("_A_xyz789_\nhello world\n")
        assert state.collecting is True

        # 后台线程推送跨 chunk 的结束标记（无换行符，走 line_buf 分支）
        end_part = f"\033[2m\033[38;2;6;14;10m_Z_xyz789_\033[0m1"

        def _bg():
            feed_output(end_part)

        t = threading.Thread(target=_bg)
        t.start()
        t.join(timeout=2)

        await asyncio.sleep(0)
        assert state.event.is_set()
        assert state.exit_code == 1
        assert "[exit code: 1]" in state.output_lines

    async def test_feed_output_no_crash_when_no_pending(self):
        """无 pending 命令时从后台线程调用不应崩溃"""
        import web.pty_executor as _mod

        with _mod._pending_lock:
            _mod._pending_cmd = None

        def _bg():
            feed_output("some random output\n")

        t = threading.Thread(target=_bg)
        t.start()
        t.join(timeout=2)
        # 无异常即通过
