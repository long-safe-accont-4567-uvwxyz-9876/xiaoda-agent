"""Harness Engineering 优化三轮渐进式冒烟验证测试。

Round 1: 单元层 — 模块导入与常量验证
Round 2: 集成层 — 组件交互验证
Round 3: 端到层 — 全链路验证
"""
import sys
import time
import inspect
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Round 1: 单元层 — 模块导入与常量验证 ──────────────────────
class TestRound1UnitSmoke:
    """Round 1: 单元层冒烟测试。"""

    def test_import_message_processor(self):
        """验证 MessageProcessorMixin 可正常导入。"""
        from agent_core.message_processor import MessageProcessorMixin
        assert MessageProcessorMixin is not None

    def test_import_tool_call_handler(self):
        """验证 ToolCallHandler 可正常导入。"""
        from tool_engine.tool_call_handler import ToolCallHandler
        assert ToolCallHandler is not None

    def test_import_tool_guardrails(self):
        """验证 ToolGuardrails 可正常导入。"""
        from tool_engine.tool_guardrails import ToolGuardrails
        assert ToolGuardrails is not None

    def test_import_prompt_builder(self):
        """验证 prompt_builder 关键符号可正常导入。"""
        from prompt_builder import (
            _MODULE_SCENE_PRIORITY, _SCENE_KEYWORDS,
            _classify_scene, build_scene_aware_prompt,
        )
        assert _MODULE_SCENE_PRIORITY is not None
        assert _SCENE_KEYWORDS is not None
        assert callable(_classify_scene)
        assert callable(build_scene_aware_prompt)

    def test_import_circuit_breaker(self):
        """验证熔断器相关符号可正常导入。"""
        from core.circuit_breaker import CircuitBreaker, CircuitState, CognitiveState
        assert CircuitBreaker is not None
        assert CircuitState is not None
        assert CognitiveState is not None

    def test_verification_loop_constants(self):
        """验证 Harness 验收循环常量值。"""
        from agent_core.message_processor import MessageProcessorMixin
        assert MessageProcessorMixin.MAX_VERIFICATION_TURNS == 8
        assert MessageProcessorMixin.VERIFICATION_WALL_TIMEOUT == 50
        assert MessageProcessorMixin.MAX_CONSECUTIVE_TOOL_FAILURES == 3
        assert MessageProcessorMixin.LLM_CALL_TIMEOUT == 30

    def test_skip_summarize_param(self):
        """验证 ToolCallHandler.handle 含 skip_summarize 参数。"""
        from tool_engine.tool_call_handler import ToolCallHandler
        sig = inspect.signature(ToolCallHandler.handle)
        assert "skip_summarize" in sig.parameters

    def test_validate_args_exists(self):
        """验证 ToolGuardrails 拥有 validate_args 方法。"""
        from tool_engine.tool_guardrails import ToolGuardrails
        assert hasattr(ToolGuardrails, "validate_args")

    def test_module_scene_priority_matrix(self):
        """验证场景优先级矩阵维度: 6 模块 x 6 场景。"""
        from prompt_builder import _MODULE_SCENE_PRIORITY
        assert len(_MODULE_SCENE_PRIORITY) == 6
        expected_scenes = {"default", "greeting", "task", "emotional", "identity", "tool"}
        for module, scene_map in _MODULE_SCENE_PRIORITY.items():
            assert len(scene_map) == 6, f"模块 {module} 场景数 != 6"
            assert set(scene_map.keys()) == expected_scenes, f"模块 {module} 场景键不匹配"

    def test_scene_keywords(self):
        """验证场景关键词覆盖 5 类场景。"""
        from prompt_builder import _SCENE_KEYWORDS
        assert len(_SCENE_KEYWORDS) == 5


# ── Round 2: 集成层 — 组件交互验证 ────────────────────────────
class TestRound2IntegrationSmoke:
    """Round 2: 集成层冒烟测试。"""

    def test_classify_scene_greeting(self):
        """场景分类: 问候。"""
        from prompt_builder import _classify_scene
        assert _classify_scene("你好呀") == "greeting"

    def test_classify_scene_task(self):
        """场景分类: 任务。"""
        from prompt_builder import _classify_scene
        assert _classify_scene("帮我写个脚本") == "task"

    def test_classify_scene_emotional(self):
        """场景分类: 情感。"""
        from prompt_builder import _classify_scene
        assert _classify_scene("今天好开心啊") == "emotional"

    def test_classify_scene_identity(self):
        """场景分类: 身份。"""
        from prompt_builder import _classify_scene
        assert _classify_scene("你是谁") == "identity"

    def test_classify_scene_tool(self):
        """场景分类: 工具。"""
        from prompt_builder import _classify_scene
        assert _classify_scene("查天气怎么样") == "tool"

    def test_classify_scene_default(self):
        """场景分类: 默认兜底。"""
        from prompt_builder import _classify_scene
        assert _classify_scene("random text") == "default"

    def test_build_scene_aware_prompt_returns_string(self):
        """场景感知 prompt 构建返回字符串且不崩溃。"""
        from prompt_builder import build_scene_aware_prompt
        result = build_scene_aware_prompt("你好呀", "爸爸")
        assert isinstance(result, str)

    def test_validate_args_valid(self):
        """护栏: 合法参数通过。"""
        from tool_engine.tool_guardrails import ToolGuardrails
        guardrails = ToolGuardrails()
        ok, reason = guardrails.validate_args("web_search", {"query": "test"})
        assert ok is True
        assert reason == ""

    def test_validate_args_dangerous_command(self):
        """护栏: 危险命令被 L3 拦截。"""
        from tool_engine.tool_guardrails import ToolGuardrails
        guardrails = ToolGuardrails()
        ok, reason = guardrails.validate_args("shell_command", {"command": "rm -rf /"})
        assert ok is False
        assert "L3" in reason

    def test_validate_args_empty_required(self):
        """护栏: 必填字段空值被 L1 拦截。"""
        from tool_engine.tool_guardrails import ToolGuardrails
        guardrails = ToolGuardrails()
        ok, reason = guardrails.validate_args("shell_command", {"command": ""})
        assert ok is False
        assert "L1" in reason

    def test_validate_args_bad_url(self):
        """护栏: 非法 URL 被 L2 拦截。"""
        from tool_engine.tool_guardrails import ToolGuardrails
        guardrails = ToolGuardrails()
        ok, reason = guardrails.validate_args("web_browse", {"url": "not-a-url"})
        assert ok is False
        assert "L2" in reason

    def test_validate_args_path_traversal(self):
        """护栏: 路径遍历被拦截。"""
        from tool_engine.tool_guardrails import ToolGuardrails
        guardrails = ToolGuardrails()
        ok, reason = guardrails.validate_args(
            "document_reader", {"file_path": "../../../etc/passwd"}
        )
        assert ok is False


# ── Round 3: 端到层 — 全链路验证 ──────────────────────────────
class TestRound3E2ESmoke:
    """Round 3: 端到层冒烟测试。"""

    def test_circuit_breaker_state_machine(self):
        """熔断器状态机: 失败累积 -> RED -> 冷却到期 -> HALF_OPEN。"""
        from core.circuit_breaker import CircuitBreaker, CircuitState, CognitiveState
        cb = CircuitBreaker(cooldown=1)
        state = CognitiveState()
        # 触发足够多的失败以进入 RED（consecutive_fails >= 8 触发 red 信号）
        for _ in range(8):
            cb.on_failure(state)
        assert cb.check(state) == CircuitState.RED
        # 等待冷却到期后应进入 HALF_OPEN 探测
        time.sleep(1.1)
        assert cb.check(state) == CircuitState.HALF_OPEN

    def test_tool_timeouts_count(self):
        """工具超时表至少 8 项（7 自定义 + default）。"""
        from tool_engine.tool_executor import ToolExecutor
        assert len(ToolExecutor.TOOL_TIMEOUTS) >= 8

    def test_stream_text_push_default(self):
        """流式文本推送默认开启。"""
        from config import STREAM_TEXT_PUSH
        assert STREAM_TEXT_PUSH == True

    def test_error_rule_strict_mode_default(self):
        """错误规则严格模式默认开启。"""
        from config import ERROR_RULE_STRICT_MODE
        assert ERROR_RULE_STRICT_MODE == True

    def test_klee_has_timeout_protection(self):
        """klee_agent.py 含 asyncio.wait_for 超时保护。"""
        klee_path = Path(__file__).parent.parent / "klee_agent.py"
        content = klee_path.read_text(encoding="utf-8")
        assert "asyncio.wait_for" in content

    def test_subagent_has_storm_detection(self):
        """agent_dispatcher.py 含风暴检测调用。"""
        dispatcher_path = Path(__file__).parent.parent / "agent_dispatcher.py"
        content = dispatcher_path.read_text(encoding="utf-8")
        assert "detect_storm" in content
