"""内置工具模块集中注册 —— 从 tool_engine.tool_registry.to_openai_tools() 抽取.

此前 to_openai_tools() 函数内 `import tools.*` 14 行, 导致:
    tool_engine.tool_registry -> tools.* -> tool_engine.tool_registry
形成静态循环. 将所有内置工具模块的导入集中到本模块, 由 tool_engine/__init__.py
顶层触发一次注册, to_openai_tools() 不再需要 import tools.*.

本模块不定义任何符号, 仅用于副作用 (触发各 tools.* 模块顶层的 @register_tool).
"""
# 导入顺序与原 to_openai_tools() 保持一致
