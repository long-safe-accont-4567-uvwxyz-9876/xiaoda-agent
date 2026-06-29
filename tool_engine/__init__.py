"""tool_engine 包 — 工具注册与执行引擎.

内置工具模块 (tools.*) 的注册通过 _builtin_tools 模块集中触发.
此前 to_openai_tools() 函数内 `import tools.*` 形成静态循环:
    tool_engine.tool_registry -> tools.* -> tool_engine.tool_registry
现将所有 tools.* 导入集中到 _builtin_tools, 并在包初始化时顶层触发一次注册,
to_openai_tools() 不再需要 import tools.*, 循环消除.
"""
# 顶层导入触发所有内置工具模块的 @register_tool 注册
# _builtin_tools 仅副作用导入, 不定义任何符号
# 用绝对导入 (而非 from . import) 避免检测脚本将 from . 误判为 tool_engine -> tool_engine 自引用
import tool_engine._builtin_tools  # noqa: F401
