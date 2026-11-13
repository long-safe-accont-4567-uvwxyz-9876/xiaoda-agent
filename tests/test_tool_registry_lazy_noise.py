# -*- coding: utf-8 -*-
"""duplicate_overwrite 噪音治理测试。

验证：
1. 懒加载占位被真实实现覆写（预期的解析路径）不再触发 WARNING，只记 debug。
2. 两次真实（非占位）注册同名工具仍然触发 duplicate_overwrite WARNING。
"""
import sys
from pathlib import Path

from loguru import logger

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _capture():
    records = []
    handler_id = logger.add(lambda m: records.append(m), level="DEBUG")
    return records, handler_id


def test_lazy_placeholder_overwrite_no_warning():
    """懒占位 → 装饰器覆写：不应产生 WARNING（原实现会刷 duplicate_overwrite）。"""
    from tool_engine.tool_registry import (
        ToolPermission,
        get_tool,
        register_lazy_tool,
        register_tool,
        unregister_tool,
    )

    name = "_test_lazy_noise_dummy"
    register_lazy_tool(
        name=name,
        description="测试占位",
        schema={"type": "object", "properties": {}, "required": []},
        module_path="tools.code_tools_v2",
        func_name="calculator",
        permission=ToolPermission.READ_ONLY,
        max_frequency=10,
    )
    records, handler_id = _capture()
    try:
        @register_tool(name=name, description="测试实现",
                       schema={"type": "object", "properties": {}, "required": []})
        def _impl(expression: str):
            return 1

        warnings = [r for r in records if r.record["level"].name == "WARNING"]
        assert not warnings, f"懒占位覆写不应告警: {[r.record['message'] for r in warnings]}"
        entry = get_tool(name)
        assert entry["func"] is _impl, "覆写后 func 应为真实实现"
        assert not entry.get("_lazy"), "覆写后不应保留 _lazy 标记"
    finally:
        logger.remove(handler_id)
        unregister_tool(name)


def test_real_double_registration_still_warns():
    """两次真实注册同名工具：仍必须触发 duplicate_overwrite WARNING。"""
    from tool_engine.tool_registry import register_tool, unregister_tool

    name = "_test_double_reg_dummy"

    @register_tool(name=name, description="第一份实现",
                   schema={"type": "object", "properties": {}, "required": []})
    def _impl_a(x: str = ""):
        return 1

    records, handler_id = _capture()
    try:
        @register_tool(name=name, description="第二份实现",
                       schema={"type": "object", "properties": {}, "required": []})
        def _impl_b(x: str = ""):
            return 2

        warn_msgs = [r.record["message"] for r in records
                     if r.record["level"].name == "WARNING"
                     and "duplicate_overwrite" in r.record["message"]]
        assert warn_msgs, "真实双注册必须保留 duplicate_overwrite 告警"
    finally:
        logger.remove(handler_id)
        unregister_tool(name)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-x"])
