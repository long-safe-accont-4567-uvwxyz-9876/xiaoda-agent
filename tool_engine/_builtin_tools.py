"""内置工具模块集中注册 —— 从 tool_engine.tool_registry.to_openai_tools() 抽取.

此前 to_openai_tools() 函数内 `import tools.*` 14 行, 导致:
    tool_engine.tool_registry -> tools.* -> tool_engine.tool_registry
形成静态循环. 将所有内置工具模块的导入集中到本模块, 由 tool_engine/__init__.py
顶层触发一次注册, to_openai_tools() 不再需要 import tools.*.

本模块不定义任何符号, 仅用于副作用 (触发各 tools.* 模块顶层的 @register_tool).
"""
# 导入顺序与原 to_openai_tools() 保持一致
import tools.file_tools_v2  # noqa: F401
import tools.code_tools_v2  # noqa: F401
import tools.web_tools_v2  # noqa: F401
import tools.document_tools  # noqa: F401
import tools.web_browse_tools  # noqa: F401
import tools.web_browse_enhanced  # noqa: F401
import tools.multi_search_tools  # noqa: F401
import tools.agnes_tools  # noqa: F401
import tools.hardware_tools  # noqa: F401
import tools.system_tools  # noqa: F401
import tools.vision_tools  # noqa: F401
import tools.memory_tool  # noqa: F401
import tools.nudge_tool  # noqa: F401
import tools.domestic_search_tools  # noqa: F401
import tools.secrets_tool  # noqa: F401
