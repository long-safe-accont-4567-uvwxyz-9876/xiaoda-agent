"""前后端工具一致性守护测试。

防止 P0 bug 回归：前端禁用的工具仍通过 to_openai_tools() 传给 LLM。
根因：apply_tool_overrides() 在启动时调用一次，但懒加载工具和 MCP 工具
此时还没注册，禁用配置没被应用。to_openai_tools() 只读 _tools[name].enabled，
不读 webui_overrides，导致前后端不一致。

此测试模拟运行时场景：先注册部分工具 → 设置 overrides → 再注册剩余工具
（模拟懒加载/MCP 后注册）→ 验证 to_openai_tools() 正确应用禁用配置。
"""
import pytest

from tool_engine.tool_registry import (
    MAX_ENABLED_TOOLS,
    ToolPermission,
    _schema_cache,
    _tools,
    invalidate_tool_cache,
    register_tool_direct,
    set_tool_overrides,
    to_openai_tools,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """每个测试前后清理 registry 状态，避免互相污染。"""
    saved_tools = dict(_tools)
    saved_cache = _schema_cache
    _tools.clear()
    invalidate_tool_cache()
    yield
    _tools.clear()
    _tools.update(saved_tools)
    import tool_engine.tool_registry as tr
    tr._schema_cache = saved_cache
    tr._webui_tool_overrides.clear()


def _register_tool(name, category="general", source="builtin", enabled=True, max_frequency=10):
    """注册一个测试工具。"""
    register_tool_direct(
        name=name,
        description=f"Test tool {name}",
        func=lambda **kwargs: {"ok": True},
        parameters={"type": "object", "properties": {}},
        permission=ToolPermission.READ_ONLY,
        category=category,
        source=source,
    )
    _tools[name]["enabled"] = enabled
    _tools[name]["max_frequency"] = max_frequency


class TestToolOverridesConsistency:
    """前后端工具一致性守护。"""

    def test_disabled_tool_not_in_openai_tools(self):
        """前端禁用的工具不出现在 to_openai_tools() 返回值中。"""
        _register_tool("tool_a")
        _register_tool("tool_b")
        _register_tool("tool_c")

        # 前端禁用 tool_b
        set_tool_overrides({"tool_b": {"enabled": False}})

        tools = to_openai_tools()
        names = [t["function"]["name"] for t in tools]
        assert "tool_a" in names
        assert "tool_c" in names
        assert "tool_b" not in names, "前端禁用的工具泄漏到了 LLM 工具列表！"

    def test_override_applied_after_lazy_registration(self):
        """overrides 设置后注册的工具也被正确应用禁用配置。

        模拟懒加载/MCP 后注册场景：先设置 overrides，再注册工具。
        """
        # 先设置 overrides（此时 tool_lazy 还没注册）
        set_tool_overrides({"tool_lazy": {"enabled": False}})

        # 后注册工具（模拟懒加载/MCP 后注册）
        _register_tool("tool_normal")
        _register_tool("tool_lazy")

        invalidate_tool_cache()
        tools = to_openai_tools()
        names = [t["function"]["name"] for t in tools]
        assert "tool_normal" in names
        assert "tool_lazy" not in names, "后注册的工具没有被 overrides 禁用！"

    def test_agnes_image_generate_must_be_present(self):
        """agnes_image_generate 必须在工具列表中（当未被禁用时）。

        防止工具截断导致图片生成功能失效的 P0 bug 回归。
        根因修复：agnes_image_generate 归入 creative 分类（优先级 85），
        与 conversation 同级，高于 general（10），工具总数超 MAX_ENABLED_TOOLS
        时不会被截断。此测试用真实分类验证该不变量。
        """
        # 注册足够多的 general 工具来触发截断
        for i in range(MAX_ENABLED_TOOLS + 10):
            _register_tool(f"tool_{i:03d}", category="general")

        # agnes_image_generate 用 creative 分类（与 manifest 一致，优先级 85）
        _register_tool("agnes_image_generate", category="creative")

        # 不设置任何 overrides
        set_tool_overrides({})

        tools = to_openai_tools()
        names = [t["function"]["name"] for t in tools]
        assert "agnes_image_generate" in names, (
            "agnes_image_generate 被截断了！creative 分类未生效，工具总数超过上限时图片生成功能失效。"
            f" 工具数={len(_tools)}, 上限={MAX_ENABLED_TOOLS}, 返回={len(tools)}"
        )

    def test_general_tool_is_truncated_when_over_limit(self):
        """对照：general 分类的工具在超限时确实会被截断（验证截断机制本身有效）。

        这证明上面的测试不是因为截断失效而通过，而是因为 creative 优先级够高。
        """
        # general 工具数量远超上限
        for i in range(MAX_ENABLED_TOOLS + 10):
            _register_tool(f"gen_tool_{i:03d}", category="general")

        set_tool_overrides({})

        tools = to_openai_tools()
        # 截断后返回数 == 上限
        assert len(tools) == MAX_ENABLED_TOOLS, (
            f"截断机制失效：返回 {len(tools)} 个工具，上限 {MAX_ENABLED_TOOLS}"
        )

    def test_put_tool_updates_overrides_and_cache(self):
        """PUT /tools/{name} 更新后缓存被清空，下次 to_openai_tools() 反映最新配置。"""
        _register_tool("tool_x")
        _register_tool("tool_y")

        # 第一次调用生成缓存
        tools = to_openai_tools()
        names = [t["function"]["name"] for t in tools]
        assert "tool_x" in names

        # 模拟 PUT /tools/{name} 禁用 tool_x
        _tools["tool_x"]["enabled"] = False
        set_tool_overrides({"tool_x": {"enabled": False}})

        # 再次调用应该反映最新配置
        tools = to_openai_tools()
        names = [t["function"]["name"] for t in tools]
        assert "tool_x" not in names, "PUT 禁用后 to_openai_tools() 仍返回旧缓存！"
        assert "tool_y" in names

    def test_re_enable_tool_after_disable(self):
        """禁用后重新启用的工具重新出现在工具列表中。"""
        _register_tool("tool_toggle")

        # 禁用
        set_tool_overrides({"tool_toggle": {"enabled": False}})
        tools = to_openai_tools()
        names = [t["function"]["name"] for t in tools]
        assert "tool_toggle" not in names

        # 重新启用
        set_tool_overrides({"tool_toggle": {"enabled": True}})
        tools = to_openai_tools()
        names = [t["function"]["name"] for t in tools]
        assert "tool_toggle" in names, "重新启用后工具未出现在列表中！"

    def test_no_disabled_tool_leak_with_many_tools(self):
        """大量工具场景下，所有禁用的工具都不泄漏到 LLM。"""
        # 注册 80 个工具
        for i in range(80):
            _register_tool(f"bulk_tool_{i:03d}")

        # 禁用其中 30 个
        disabled_names = {f"bulk_tool_{i:03d}" for i in range(30)}
        overrides = {name: {"enabled": False} for name in disabled_names}
        set_tool_overrides(overrides)

        tools = to_openai_tools()
        names = {t["function"]["name"] for t in tools}

        leaked = disabled_names & names
        assert not leaked, f"{len(leaked)} 个禁用工具泄漏到 LLM: {list(leaked)[:5]}"
