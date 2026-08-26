"""agent_dispatcher.py 拆分结构契约测试。

背景：agent_dispatcher.py（1392 行，6 个类 + 5 个顶层符号）按类拆为：
  - agent_core/tool_call_extractors.py（ExtractedToolCall / StandardExtractor /
    DsmlExtractor / ResourceBackend / ToolCallExtractor）
  - agent_core/sub_agent.py（SubAgentConfig / SubAgent + 常量 + 辅助函数 + J-Space hook）
  - agent_dispatcher.py 留 AgentDispatcher + _AGENT_TO_TASK_TYPE + re-export

契约：
    1. 两个新模块独立可导入
    2. agent_dispatcher 同名 re-export（同对象）
    3. 外部消费者（web/agent_registry.py / core/bootstrap.py 等）不受影响
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

# ── tool_call_extractors ────────────────────────────────────────

class TestToolCallExtractorsModule:
    def test_standalone_import(self):
        import importlib
        mod = importlib.import_module("agent_core.tool_call_extractors")
        for name in ("ExtractedToolCall", "StandardExtractor", "DsmlExtractor",
                      "ToolCallExtractor", "ResourceBackend"):
            assert hasattr(mod, name)

    def test_extracted_tool_call_parse_arguments(self):
        from agent_core.tool_call_extractors import ExtractedToolCall
        tc = ExtractedToolCall(id="1", name="test", arguments_json='{"a": 1}')
        assert tc.parse_arguments() == {"a": 1}
        tc2 = ExtractedToolCall(id="2", name="bad", arguments_json="not json")
        assert tc2.parse_arguments() == {}

    def test_resource_backend_is_protocol(self):
        from agent_core.tool_call_extractors import ResourceBackend
        # _is_protocol 是布尔属性而非可调用（runtime_checkable Protocol 的标志）
        assert getattr(ResourceBackend, "_is_protocol", False) is True

    @pytest.mark.parametrize("name", [
        "ExtractedToolCall", "StandardExtractor", "DsmlExtractor",
        "ToolCallExtractor", "ResourceBackend",
    ])
    def test_dispatcher_reexports_same_objects(self, name):
        import agent_core.tool_call_extractors
        import agent_dispatcher
        assert getattr(agent_dispatcher, name) is getattr(agent_core.tool_call_extractors, name)


# ── sub_agent ────────────────────────────────────────────────────

class TestSubAgentModule:
    def test_standalone_import(self):
        import importlib
        mod = importlib.import_module("agent_core.sub_agent")
        for name in ("SubAgentConfig", "SubAgent", "DELEGATE_BLOCKED_TOOLS",
                      "_RESOURCE_PATH_TOOLS", "SUB_AGENT_PROFILE_TOOLS",
                      "_safe_log_path", "_read_env_key", "_is_tool_unsupported_error"):
            assert hasattr(mod, name), f"missing {name}"

    def test_sub_agent_config_is_dataclass(self):
        from agent_core.sub_agent import SubAgentConfig
        assert hasattr(SubAgentConfig, "__dataclass_fields__")

    def test_constants_values(self):
        from agent_core.sub_agent import DELEGATE_BLOCKED_TOOLS
        assert "delegate_task" in DELEGATE_BLOCKED_TOOLS

    def test_safe_log_path(self):
        from agent_core.sub_agent import _safe_log_path
        assert "\n" not in _safe_log_path("a\nb\x00c")

    @pytest.mark.parametrize("name", [
        "SubAgentConfig", "SubAgent", "DELEGATE_BLOCKED_TOOLS",
        "_RESOURCE_PATH_TOOLS", "_safe_log_path", "_read_env_key",
        "_is_tool_unsupported_error",
    ])
    def test_dispatcher_reexports_same_objects(self, name):
        import agent_core.sub_agent
        import agent_dispatcher
        assert getattr(agent_dispatcher, name) is getattr(agent_core.sub_agent, name)

    def test_j_space_hook_globals_exist(self):
        import agent_core.sub_agent as mod
        assert hasattr(mod, "_signal_stream")
        assert hasattr(mod, "_intervention_loop")
