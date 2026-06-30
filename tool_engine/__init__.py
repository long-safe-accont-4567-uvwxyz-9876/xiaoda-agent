"""tool_engine 包 — 工具注册与执行引擎.

内置工具模块 (tools.*) 的注册改为懒加载：包初始化时仅登记工具元数据
(``register_builtin_tools_lazy``)，不 import 任何 tools.* 子模块，避免冷启动
把 httpx/selenium/PIL/primp 等重依赖一次性拉进进程。重依赖推迟到首次工具
调用时由 ``resolve_tool_func`` 按需 import。

此前本包顶层 ``import tool_engine._builtin_tools`` 会急切导入全部 tools.* 模块，
现已移除；``_builtin_tools`` 保留为可选的显式全量导入入口（如需急切注册可手动 import）。
"""
from tool_engine.tool_registry import register_builtin_tools_lazy

# 包初始化时登记所有内置工具元数据（func 留空，首次调用时按需 import 实现模块）。
# 幂等：重复调用安全。
register_builtin_tools_lazy()
