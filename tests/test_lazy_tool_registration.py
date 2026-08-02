"""P0-1 工具延迟注册测试。

验证：
1. 冷启动 register_builtin_tools_lazy() 不 import 任何 tools.* 子模块或重依赖，
   但 to_openai_tools() 仍返回完整工具列表（含 web_browse 等已知工具）。
2. 首次调用懒注册工具时按需解析 func 并返回正确结果，二次调用复用缓存（_lazy 置 False）。
"""
import sys
import asyncio
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _fake_lazy_tool(x: int = 0):
    """测试用懒注册工具实现（同步函数）。"""
    from tool_engine.tool_registry import ToolResult
    return ToolResult.ok(f"called with x={x}")


def test_cold_start_does_not_import_tools_submodules():
    """子进程隔离验证：register_builtin_tools_lazy 不触发 tools.* / 重依赖 import。"""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(PROJECT_ROOT)!r})\n"
        "from tool_engine.tool_registry import register_builtin_tools_lazy, to_openai_tools\n"
        "register_builtin_tools_lazy()\n"
        "tools = to_openai_tools()\n"
        "names = [t['function']['name'] for t in tools]\n"
        "assert tools, 'to_openai_tools returned empty'\n"
        "assert 'web_browse' in names, f'web_browse missing: {names}'\n"
        "assert 'shell_command' in names, f'shell_command missing'\n"
        "assert 'mail_list' in names, f'mail_list missing'\n"
        # 关键：登记元数据不应 import 任何 tools.* 子模块
        "assert 'tools.web_browse_tools' not in sys.modules, 'web_browse_tools eagerly imported'\n"
        "assert 'tools.web_browse_enhanced' not in sys.modules, 'web_browse_enhanced eagerly imported'\n"
        "assert 'tools.agnes_tools' not in sys.modules, 'agnes_tools eagerly imported'\n"
        "assert 'tools.domestic_search_tools' not in sys.modules, 'domestic_search_tools eagerly imported'\n"
        "assert 'tools.mail_tools' not in sys.modules, 'mail_tools eagerly imported'\n"
        # 重依赖也不应在登记阶段被拉入
        "assert 'httpx' not in sys.modules, 'httpx eagerly imported'\n"
        "assert 'selenium' not in sys.modules, 'selenium eagerly imported'\n"
        "assert 'PIL' not in sys.modules, 'PIL eagerly imported'\n"
        "print('OK tools_count=' + str(len(tools)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, (
        f"subprocess failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "OK tools_count=" in result.stdout, result.stdout


def test_lazy_tool_resolve_and_cache():
    """首次调用懒注册工具解析 func 并返回结果，二次调用复用缓存。"""
    from tool_engine.tool_registry import (
        register_lazy_tool, get_tool, unregister_tool, ToolPermission,
    )
    from tool_engine.tool_executor import ToolExecutor

    tool_name = "_test_lazy_dummy_42"
    register_lazy_tool(
        name=tool_name,
        description="测试用懒注册工具",
        schema={
            "type": "object",
            "properties": {"x": {"type": "integer", "description": "参数"}},
            "required": [],
        },
        module_path=__name__,
        func_name="_fake_lazy_tool",
        permission=ToolPermission.READ_ONLY,
        category="general",
        max_frequency=10,
    )
    try:
        tool = get_tool(tool_name)
        assert tool is not None, "懒注册工具未登记"
        assert tool.get("_lazy") is True, "登记后应为懒标记"
        assert tool.get("func") is None, "懒注册 func 应为 None 占位"

        executor = ToolExecutor(db=None)

        # 首次调用：触发懒解析
        result1 = asyncio.run(executor.execute(tool_name, {"x": 7}))
        assert result1.success, f"首次调用失败: {result1.error}"
        assert "called with x=7" in str(result1.data)

        tool_after = get_tool(tool_name)
        assert tool_after.get("_lazy") is False, "首次调用后 _lazy 应置 False"
        assert tool_after.get("func") is not None, "首次调用后 func 应回填"

        # 二次调用：复用已解析的 func（不重新 import）
        result2 = asyncio.run(executor.execute(tool_name, {"x": 99}))
        assert result2.success, f"二次调用失败: {result2.error}"
        assert "called with x=99" in str(result2.data)
    finally:
        unregister_tool(tool_name)


def test_lazy_resolve_failure_returns_fail():
    """懒解析失败（模块不存在）时 ToolExecutor 返回 fail 而非抛异常。"""
    from tool_engine.tool_registry import (
        register_lazy_tool, unregister_tool, ToolPermission,
    )
    from tool_engine.tool_executor import ToolExecutor

    tool_name = "_test_lazy_missing_module"
    register_lazy_tool(
        name=tool_name,
        description="指向不存在模块的懒注册工具",
        schema={"type": "object", "properties": {}},
        module_path="tools.__nonexistent_module_xyz__",
        func_name="no_such_func",
        permission=ToolPermission.READ_ONLY,
        max_frequency=10,
    )
    try:
        executor = ToolExecutor(db=None)
        result = asyncio.run(executor.execute(tool_name, {}))
        assert not result.success, "不存在的模块应返回失败"
        assert result.error, "失败结果应带错误信息"
    finally:
        unregister_tool(tool_name)


def test_webui_override_blocks_executor_for_late_registered_tool():
    """WebUI 禁用的工具，即使 apply_tool_overrides 之后才注册，execute 也必须拒绝。

    场景：MCP 或懒加载工具在启动期 apply_tool_overrides 执行后才注册，此时
    工具 dict 的 enabled 字段未被覆写，只有 _webui_tool_overrides 中保存了禁用
    配置。to_openai_tools 会过滤掉它，但 LLM 仍可能通过上下文/注入直接调用工具名。
    ToolExecutor 必须同样依据 _webui_tool_overrides 拒绝执行，否则管理员禁用的
    工具仍会被执行。
    """
    import tool_engine.tool_registry as _registry
    from tool_engine.tool_registry import (
        register_tool, unregister_tool, set_tool_overrides,
        ToolPermission, to_openai_tools,
    )
    from tool_engine.tool_executor import ToolExecutor

    tool_name = "_test_override_late_disabled"
    old_overrides = dict(_registry._webui_tool_overrides)
    # 先设置 webui override 禁用，再注册工具（模拟 late registration）
    set_tool_overrides({tool_name: {"enabled": False}})

    @register_tool(
        name=tool_name,
        description="测试 webui override 禁用后注册的工具",
        schema={"type": "object", "properties": {}},
        permission=ToolPermission.READ_ONLY,
    )
    def _tool_impl():
        from tool_engine.tool_registry import ToolResult
        return ToolResult.ok("should not run")

    try:
        tool = _registry.get_tool(tool_name)
        assert tool is not None
        # 关键：late registered 工具的 dict 自身没有 enabled=False
        assert tool.get("enabled") is not False

        # to_openai_tools 应将其过滤
        openai_names = {t["function"]["name"] for t in to_openai_tools()}
        assert tool_name not in openai_names

        # ToolExecutor 也应拒绝执行，而不是实际运行
        executor = ToolExecutor(db=None)
        result = asyncio.run(executor.execute(tool_name, {}))
        assert not result.success, f"被禁用的工具不应执行: {result.data}"
        assert "停用" in result.error
    finally:
        set_tool_overrides(old_overrides)
        unregister_tool(tool_name)


def test_to_openai_tools_concurrent_registration_safe():
    """to_openai_tools 在并发注册新工具时不应因字典扩容而崩溃。"""
    import threading
    from tool_engine.tool_registry import register_tool, to_openai_tools, unregister_tool, ToolPermission

    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def _register_many():
        barrier.wait()
        for i in range(30):
            try:
                @register_tool(
                    name=f"_test_concurrent_{i}",
                    description="并发注册测试工具",
                    schema={"type": "object", "properties": {}},
                    permission=ToolPermission.READ_ONLY,
                )
                def _impl():
                    from tool_engine.tool_registry import ToolResult
                    return ToolResult.ok("ok")
            except Exception as e:
                errors.append(e)

    def _iterate_many():
        barrier.wait()
        for _ in range(60):
            try:
                to_openai_tools()
            except RuntimeError as e:
                errors.append(e)

    t1 = threading.Thread(target=_register_many)
    t2 = threading.Thread(target=_iterate_many)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    for i in range(30):
        unregister_tool(f"_test_concurrent_{i}")

    assert not errors, f"并发迭代期间出现错误: {errors[:3]}"


if __name__ == "__main__":
    pytest.main([__file__, "-x"])
