"""工具分层注入测试：常驻精简 + search_tools 按需检索 + 一键回退。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config_constants as cc
import tool_engine.tool_registry as reg


def _reset(tiering: bool):
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(cc, "TOOL_TIERING_ENABLED", tiering)
    # registry 模块内是函数级延迟导入，需同时钉住来源
    import config_constants  # noqa: F401
    reg._schema_cache = None
    return monkeypatch


@pytest.fixture
def registered(monkeypatch):
    reg.register_builtin_tools_lazy()
    reg._schema_cache = None
    reg._tiering_wired = False  # 接线状态逐用例复位，防跨用例泄漏

    import tool_engine.tool_search as tsm
    monkeypatch.setattr(tsm, "_engine", tsm.ToolSearchEngine())
    yield


def _names():
    return {t["function"]["name"] for t in reg.to_openai_tools()}


def test_tiering_on_defers_mail_and_exposes_search_tools(registered, monkeypatch):
    monkeypatch.setattr(cc, "TOOL_TIERING_ENABLED", True)
    reg._schema_cache = None
    names = _names()
    assert "mail_search" not in names and "mail_send" not in names
    assert "search_tools" in names
    assert "calculator" in names  # 常驻核心不受精炼影响


def test_tiering_off_keeps_full_set(registered, monkeypatch):
    monkeypatch.setattr(cc, "TOOL_TIERING_ENABLED", False)
    reg._schema_cache = None
    names = _names()
    assert "mail_search" in names and "search_tools" not in names


def test_deferred_tool_still_executable_by_name(registered, monkeypatch):
    """被延迟的工具必须仍可执行（executor 查全量 _tools）。"""
    monkeypatch.setattr(cc, "TOOL_TIERING_ENABLED", True)
    reg._schema_cache = None
    t = reg.get_tool("retrieve_context")
    assert t is not None, "延迟工具不得从 _tools 移除，仅从注入列表排除"


async def test_search_tools_impl_finds_mail(monkeypatch):
    from core.async_delegation import BackgroundDelegation  # noqa: F401 环境预热
    reg.register_builtin_tools_lazy()
    reg._tiering_wired = False
    monkeypatch.setattr(cc, "TOOL_TIERING_ENABLED", True)
    reg._ensure_tiering_wiring()

    from tool_engine.tool_executor import ToolResult
    impl = reg.get_tool("search_tools")["func"]
    res: ToolResult = await impl(query="搜索我的邮件", top_k=5)
    assert res.success
    assert "mail_" in res.data

    res2: ToolResult = await impl(query="完全不存在的量子魔法工具xyz")
    assert isinstance(res2, ToolResult)
