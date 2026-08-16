"""B3 契约测试：clear_tools() 清理语义一致性。

审计结论：tool_registry 的模块级全局（_tools/_schema_cache/
_webui_tool_overrides/_schema_version）是单例注册表，全项目无外部直接访问
（均走函数接口），且已有 _schema_lock 线程安全保护——无需改单例类。
唯一真实隐患：clear_tools() 此前只清 _tools，不清 _webui_tool_overrides，
导致测试/运行时清理语义不一致（overrides 残留可能让新注册工具被误禁用）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_clear_tools_resets_overrides():
    """clear_tools() 应同时清空 _webui_tool_overrides，保持清理语义一致。"""
    import tool_engine.tool_registry as tr

    saved_tools = tr.get_all_tool_dicts()
    try:
        # 设置一个 overrides + 注册一个工具
        tr.set_tool_overrides({"some_tool": {"enabled": False}})
        tr.register_tool_direct(
            name="b3_test_tool",
            description="test",
            func=lambda: None,
            parameters={"properties": {}},
        )
        assert tr._webui_tool_overrides == {"some_tool": {"enabled": False}}

        # clear 后 overrides 应为空
        tr.clear_tools()
        assert tr._webui_tool_overrides == {}, \
            "clear_tools() 应清空 _webui_tool_overrides"
        assert tr.get_tool("b3_test_tool") is None
    finally:
        tr.clear_tools()
        tr._tools.update(saved_tools)
        tr.invalidate_schema_cache()


def test_set_tool_overrides_then_clear_does_not_filter_new_tools():
    """clear 后新注册的工具不应被残留 overrides 误禁用。"""
    import tool_engine.tool_registry as tr

    saved_tools = tr.get_all_tool_dicts()
    try:
        tr.set_tool_overrides({"future_tool": {"enabled": False}})
        tr.clear_tools()
        # 重新注册同名工具，不应被 overrides 禁用
        tr.register_tool_direct(
            name="future_tool",
            description="after clear",
            func=lambda: None,
            parameters={"properties": {}},
        )
        tools = tr.to_openai_tools()
        names = [t["function"]["name"] for t in tools]
        assert "future_tool" in names, \
            "残留 overrides 导致 clear 后注册的工具被误禁用"
    finally:
        tr.clear_tools()
        tr._tools.update(saved_tools)
        tr.invalidate_schema_cache()
