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
        """验证场景优先级矩阵维度: 4 模块 x 10 场景 (v4 分层架构).

        v4: Stable Prefix (IDENTITY/SOUL/TOOLS/skills/hardware) 不参与场景重排,
            仅 Scene-Aware Middle (AGENTS/USER/MEMORY/HEARTBEAT) 4 模块参与.
        Hecate 结构广度优化: 6→10 场景 (新增 time/debug/creative/learning).
        """
        from prompt_builder import _MODULE_SCENE_PRIORITY
        assert len(_MODULE_SCENE_PRIORITY) == 4, f"v4 应为 4 模块, 实际 {len(_MODULE_SCENE_PRIORITY)}"
        expected_scenes = {"default", "greeting", "task", "emotional", "identity", "tool",
                           "time", "debug", "creative", "learning"}
        for module, scene_map in _MODULE_SCENE_PRIORITY.items():
            assert len(scene_map) == 10, f"模块 {module} 场景数 != 10"
            assert set(scene_map.keys()) == expected_scenes, f"模块 {module} 场景键不匹配"

    def test_scene_keywords(self):
        """验证场景关键词覆盖 9 类场景 (6 原有 + 4 新增 - 1 default 不在关键词表)。"""
        from prompt_builder import _SCENE_KEYWORDS
        assert len(_SCENE_KEYWORDS) == 9


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
        """xiaoli_agent.py 含 asyncio.wait_for 超时保护。"""
        xiaoli_path = Path(__file__).parent.parent / "xiaoli_agent.py"
        content = klee_path.read_text(encoding="utf-8")
        assert "asyncio.wait_for" in content

    def test_subagent_has_storm_detection(self):
        """agent_dispatcher.py 含风暴检测调用。"""
        dispatcher_path = Path(__file__).parent.parent / "agent_dispatcher.py"
        content = dispatcher_path.read_text(encoding="utf-8")
        assert "detect_storm" in content


class TestSceneAwareV2:
    """场景感知 v2 新功能验证：加权混合检测 + 签名缓存 + 场景粘性。"""

    def test_blended_detection_single_scene(self):
        """单一场景输入应返回该场景权重=1.0"""
        from prompt_builder import _classify_scene_blended
        weights = _classify_scene_blended("帮我写个脚本")
        assert "task" in weights
        assert weights["task"] == 1.0

    def test_blended_detection_multi_scene(self):
        """多场景输入应返回混合权重"""
        from prompt_builder import _classify_scene_blended
        weights = _classify_scene_blended("好累啊，查天气怎么样")
        assert "emotional" in weights
        assert "tool" in weights
        # 两个场景都应有非零权重
        assert weights["emotional"] > 0
        assert weights["tool"] > 0

    def test_blended_detection_default(self):
        """无匹配输入应返回 default"""
        from prompt_builder import _classify_scene_blended
        weights = _classify_scene_blended("random xyz 12345")
        assert weights.get("default") == 1.0

    def test_scene_cache_hit(self):
        """相同场景的第二次调用应命中缓存"""
        from prompt_builder import reset_scene_cache, build_scene_aware_prompt, get_scene_cache_stats
        reset_scene_cache()
        build_scene_aware_prompt("帮我写脚本", "爸爸")
        stats1 = get_scene_cache_stats()
        assert stats1["misses"] >= 1
        build_scene_aware_prompt("帮我改代码", "爸爸")
        stats2 = get_scene_cache_stats()
        assert stats2["hits"] >= 1

    def test_scene_stickiness(self):
        """三级场景分级: S级立刻重排, B级保持当前排序 (替代旧版场景粘性)

        v3 设计: 删除场景粘性阈值, 改为三级场景分级
        - S 级 (核心事实): 立刻重排 (杜绝时间认知错乱)
        - B 级 (闲聊): 保持当前排序 (节省算力)
        """
        from prompt_builder import reset_scene_cache, build_scene_aware_prompt
        import prompt_builder
        reset_scene_cache()
        # 首次: A 级场景 task (功能桶排序)
        build_scene_aware_prompt("帮我写个脚本", "爸爸")
        sig_after_task = prompt_builder._current_scene_sig
        assert sig_after_task != ()
        # 第二次: S 级场景 time (必须立刻重排, 杜绝时间认知错乱)
        build_scene_aware_prompt("几点了", "爸爸")
        sig_after_time = prompt_builder._current_scene_sig
        # S 级必须立刻重排, 不能保持旧排序
        assert sig_after_time != sig_after_task, "S 级场景必须立刻重排"
        # 第三次: B 级场景 greeting (保持当前排序, 节省算力)
        build_scene_aware_prompt("你好", "爸爸")
        sig_after_greeting = prompt_builder._current_scene_sig
        # B 级保持当前排序 (不重排)
        assert sig_after_greeting == sig_after_time, "B 级场景应保持当前排序"

    def test_cache_stats_function(self):
        """缓存统计函数应返回正确结构"""
        from prompt_builder import reset_scene_cache, get_scene_cache_stats
        reset_scene_cache()
        stats = get_scene_cache_stats()
        assert "hits" in stats
        assert "misses" in stats
        assert "hit_rate" in stats
        assert "cached_signatures" in stats

    def test_reset_scene_cache(self):
        """重置后缓存应为空"""
        from prompt_builder import reset_scene_cache, build_scene_aware_prompt, get_scene_cache_stats
        build_scene_aware_prompt("测试", "爸爸")
        reset_scene_cache()
        stats = get_scene_cache_stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["cached_signatures"] == 0
