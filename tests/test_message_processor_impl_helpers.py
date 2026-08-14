"""_parse_mode_markers / _call_with_timeout 单元测试。"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from agent_core.message_processor import MessageProcessorMixin


def _make_proc():
    proc = MagicMock(spec=MessageProcessorMixin)
    proc._think_mode = False
    proc._search_mode = False
    proc._system_context = ""
    return proc


def test_parse_mode_markers_plain():
    proc = _make_proc()
    assert MessageProcessorMixin._parse_mode_markers(proc, "你好") == "你好"
    assert proc._think_mode is False and proc._search_mode is False
    assert proc._system_context == ""


def test_parse_mode_markers_search():
    proc = _make_proc()
    assert MessageProcessorMixin._parse_mode_markers(proc, "[Search: 今天天气]") == "今天天气"
    assert proc._search_mode is True
    assert "web_search" in proc._system_context


def test_parse_mode_markers_doc():
    proc = _make_proc()
    out = MessageProcessorMixin._parse_mode_markers(proc, "总结一下\n[Doc: /tmp/a.md]")
    assert out == "总结一下"
    assert "document_reader" in proc._system_context


@pytest.mark.asyncio
async def test_call_with_timeout_timeout():
    proc = MagicMock(spec=MessageProcessorMixin)
    async def slow():
        await asyncio.sleep(1)
    await MessageProcessorMixin._call_with_timeout(
        proc, slow(), timeout=0.01,
        timeout_log="agent.test_timeout", error_log="agent.test_error",
        timeout_kwargs={"user_id": "u1"},
    )


@pytest.mark.asyncio
async def test_call_with_timeout_exception():
    proc = MagicMock(spec=MessageProcessorMixin)
    async def boom():
        raise ValueError("x")
    await MessageProcessorMixin._call_with_timeout(
        proc, boom(), timeout=1.0,
        timeout_log="agent.test_timeout", error_log="agent.test_error",
        timeout_kwargs={"user_id": "u1"},
    )
