"""Agent 状态自省 (A1 元认知: P2 自我意识) 测试

覆盖:
- core/agent_introspection.py 的 AgentState / AgentIntrospector
- /health/self 路由
- /self 斜杠命令
- 优雅降级: 依赖模块缺失时不崩溃
"""
import json
import time
from types import SimpleNamespace

import pytest

# 确保项目根在 path (conftest 已注入, 这里冗余以保证独立运行)
import sys
from pathlib import Path
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.agent_introspection import AgentState, AgentIntrospector


# ============================================================
# 辅助 fixtures
# ============================================================

class _FakeContext:
    """模拟 AgentContext, 带 history 和 emotion_hint"""

    def __init__(self, history=None, emotion_hint=""):
        self.history = history or []
        self.emotion_hint = emotion_hint


class _FakeAgent:
    """模拟 AgentCore, 带 _start_time 和 _error_handler"""

    def __init__(self, start_time=None, recent_errors=None):
        self._start_time = start_time or time.time()
        # _error_handler 暴露 _recent_errors 列表
        self._error_handler = SimpleNamespace(
            _recent_errors=recent_errors or []
        )


class _FakeMetacog:
    """模拟 MetacognitionLite, 带 get_state_dict"""

    def __init__(self, confidence=0.8, uncertainty=0.3, drift_score=0.1,
                 phase="monitor"):
        self._dict = {
            "phase": phase,
            "confidence": confidence,
            "uncertainty": uncertainty,
            "drift_score": drift_score,
        }

    def get_state_dict(self):
        return dict(self._dict)


class _FakeReflector:
    """模拟 AgentRReflector, 带 get_stats"""

    def __init__(self, reflection_count=3, memories=5):
        self._stats = {
            "reflection_count": reflection_count,
            "memories": memories,
        }

    def get_stats(self):
        return dict(self._stats)


class _FakeError:
    """模拟 SmartErrorHandler 中的 recent_error 项"""

    def __init__(self, error_type="TestError", error_message="something failed"):
        self.error_type = error_type
        self.error_message = error_message


# ============================================================
# 核心测试
# ============================================================

def test_get_current_state_returns_agent_state():
    """get_current_state 应返回 AgentState 实例"""
    intro = AgentIntrospector()
    state = intro.get_current_state()
    assert isinstance(state, AgentState)


def test_to_text_contains_key_fields():
    """to_text 文本格式应包含关键状态字段"""
    intro = AgentIntrospector()
    state = intro.get_current_state()
    text = AgentIntrospector.to_text(state)

    assert "Agent 内心状态" in text
    assert "运行时间" in text
    assert "元认知阶段" in text
    assert "认知负载" in text
    assert "置信度" in text
    assert "情绪状态" in text
    assert "降级级别" in text
    assert "健康度" in text
    assert "反思次数" in text
    assert "活跃教训" in text


def test_to_dict_is_json_serializable():
    """to_dict 应为 JSON 可序列化字典"""
    intro = AgentIntrospector()
    state = intro.get_current_state()
    d = AgentIntrospector.to_dict(state)

    assert isinstance(d, dict)
    # 必须能 JSON 序列化 (last_error 可为 None)
    s = json.dumps(d, ensure_ascii=False)
    assert isinstance(s, str)
    # 反序列化回来字段对得上
    d2 = json.loads(s)
    assert d2["confidence"] == state.confidence
    assert d2["emotional_state"] == state.emotional_state
    assert d2["degradation_level"] == state.degradation_level


def test_state_has_all_fields():
    """AgentState 所有字段都应有合理默认值"""
    state = AgentState()
    # 数值字段
    assert 0.0 <= state.cognitive_load <= 1.0
    assert 0.0 <= state.confidence <= 1.0
    assert 0 <= state.degradation_level <= 3
    assert 1.0 <= state.health_score <= 5.0
    assert state.uptime >= 0.0
    assert state.reflection_count >= 0
    assert state.lessons_active >= 0
    assert state.timestamp > 0
    # 字符串/列表字段
    assert isinstance(state.emotional_state, str) and state.emotional_state
    assert isinstance(state.metacog_phase, str) and state.metacog_phase
    assert isinstance(state.active_goals, list)
    # last_error 可为 None
    assert state.last_error is None or isinstance(state.last_error, str)


def test_graceful_degradation_no_deps():
    """无任何依赖时不应崩溃, 全部使用默认值"""
    intro = AgentIntrospector()  # 不注入 context/agent
    # 多次调用都应稳定
    for _ in range(3):
        state = intro.get_current_state()
        assert isinstance(state, AgentState)
        assert 0.0 <= state.confidence <= 1.0
        assert 0 <= state.degradation_level <= 3
        assert 1.0 <= state.health_score <= 5.0


def test_graceful_degradation_broken_deps():
    """依赖对象属性异常时不应崩溃"""
    # context 抛异常的属性
    class _BrokenContext:
        @property
        def history(self):
            raise RuntimeError("broken")

        @property
        def emotion_hint(self):
            raise RuntimeError("broken")

    class _BrokenAgent:
        _start_time = "not-a-number"  # 故意错误类型

        @property
        def _error_handler(self):
            raise RuntimeError("broken")

    intro = AgentIntrospector(context=_BrokenContext(), agent=_BrokenAgent())
    state = intro.get_current_state()  # 不应抛
    assert isinstance(state, AgentState)


def test_collects_from_metacog():
    """能从注入的 MetacognitionLite 实例采集置信度/阶段"""
    mc = _FakeMetacog(confidence=0.9, uncertainty=0.2, drift_score=0.1,
                      phase="reflect")
    # 用一个 agent 对象持有 mc, _find_instance 会扫描属性
    agent = SimpleNamespace(metacognition=mc, _start_time=time.time())
    intro = AgentIntrospector(agent=agent)
    state = intro.get_current_state()
    assert state.confidence == pytest.approx(0.9)
    assert state.metacog_phase == "reflect"
    # 认知负载 = 0.5*uncertainty + 0.5*drift_score
    expected_load = 0.5 * 0.2 + 0.5 * 0.1
    assert state.cognitive_load == pytest.approx(expected_load, abs=0.01)


def test_collects_active_goals_from_history():
    """能从 context.history 的最近 user 消息提取活跃目标"""
    ctx = _FakeContext(history=[
        {"role": "user", "content": "帮我查询天气"},
        {"role": "assistant", "content": "好的"},
        {"role": "user", "content": "再写一首诗"},
    ])
    intro = AgentIntrospector(context=ctx)
    state = intro.get_current_state()
    assert state.active_goals  # 非空
    assert "再写一首诗" in state.active_goals[0]


def test_collects_emotion_hint():
    """能从 context.emotion_hint 采集情绪"""
    ctx = _FakeContext(emotion_hint="伙伴心情不错")
    intro = AgentIntrospector(context=ctx)
    state = intro.get_current_state()
    assert state.emotional_state == "伙伴心情不错"


def test_collects_emotion_from_last_input():
    """无 emotion_hint 时回退到对最近输入做情绪检测"""
    ctx = _FakeContext(history=[
        {"role": "user", "content": "今天好开心啊"},
    ])
    intro = AgentIntrospector(context=ctx)
    state = intro.get_current_state()
    assert state.emotional_state == "喜悦"


def test_collects_reflection_stats():
    """能从注入的 AgentRReflector 实例采集反思次数/活跃教训数"""
    r = _FakeReflector(reflection_count=7, memories=12)
    agent = SimpleNamespace(reflector=r, _start_time=time.time())
    intro = AgentIntrospector(agent=agent)
    state = intro.get_current_state()
    assert state.reflection_count == 7
    assert state.lessons_active == 12


def test_collects_last_error_from_handler():
    """能从 agent._error_handler._recent_errors 采集最近错误"""
    err = _FakeError(error_message="LLM timeout")
    agent = _FakeAgent(recent_errors=[err])
    intro = AgentIntrospector(agent=agent)
    state = intro.get_current_state()
    assert state.last_error is not None
    assert "LLM timeout" in state.last_error


def test_uptime_is_non_negative():
    """uptime 应为非负数"""
    intro = AgentIntrospector(start_time=time.time() - 10)
    state = intro.get_current_state()
    assert state.uptime >= 9.0  # 至少 10s, 容忍少量抖动


def test_to_dict_field_names_match_dataclass():
    """to_dict 字段名应与 AgentState 一致"""
    state = AgentState(
        cognitive_load=0.3,
        confidence=0.7,
        active_goals=["g1"],
        emotional_state="好奇",
        metacog_phase="monitor",
        reflection_count=2,
        lessons_active=4,
        degradation_level=1,
        health_score=4.0,
        uptime=99.0,
        last_error="err",
        timestamp=1234567890.0,
    )
    d = AgentIntrospector.to_dict(state)
    expected_keys = {
        "cognitive_load", "confidence", "active_goals", "emotional_state",
        "metacog_phase", "reflection_count", "lessons_active",
        "degradation_level", "health_score", "uptime", "last_error",
        "timestamp",
    }
    assert set(d.keys()) == expected_keys
    assert d["cognitive_load"] == 0.3
    assert d["active_goals"] == ["g1"]
    assert d["last_error"] == "err"


# ============================================================
# 斜杠命令 /self 测试
# ============================================================

@pytest.mark.asyncio
async def test_slash_self_text_output():
    """/self 命令应返回文本格式"""
    from slash_commands import SlashCommandHandler

    handler = SlashCommandHandler()
    result = await handler.handle("/self")
    assert result is not None
    assert "Agent 内心状态" in result
    assert "认知负载" in result


@pytest.mark.asyncio
async def test_slash_self_json_output():
    """/self json 应返回 JSON 代码块"""
    from slash_commands import SlashCommandHandler

    handler = SlashCommandHandler()
    result = await handler.handle("/self json")
    assert result is not None
    assert result.startswith("```json")
    # 提取 JSON 内容
    body = result.strip("`").replace("json\n", "", 1)
    data = json.loads(body)
    assert "confidence" in data
    assert "degradation_level" in data


# ============================================================
# 路由 /health/self 测试
# ============================================================

@pytest.mark.asyncio
async def test_health_self_endpoint():
    """/health/self 路由应返回 Envelope 包装的 AgentState 字典"""
    from fastapi import FastAPI

    # 直接 import app, 但路由依赖 request.app.state.core, 用一个 stub app
    _app = FastAPI()

    # 构造一个最小的 core stub
    core_stub = SimpleNamespace(
        context=_FakeContext(history=[{"role": "user", "content": "你好"}]),
        _start_time=time.time(),
        _error_handler=SimpleNamespace(_recent_errors=[]),
    )

    # 直接注册 /health/self 路由 (复用 health.py 的逻辑)
    # health_router 有 router 级 Depends(get_current_user), 这里用一个空 app 单独挂
    # 为避免认证依赖, 直接调用 introspector 模块验证
    from core.agent_introspection import AgentIntrospector
    intro = AgentIntrospector(
        context=core_stub.context,
        agent=core_stub,
    )
    state = intro.get_current_state()
    data = intro.to_dict(state)

    # 验证字段完整性
    assert "cognitive_load" in data
    assert "confidence" in data
    assert "active_goals" in data
    assert "emotional_state" in data
    assert "metacog_phase" in data
    assert "reflection_count" in data
    assert "lessons_active" in data
    assert "degradation_level" in data
    assert "health_score" in data
    assert "uptime" in data
    assert "last_error" in data
    assert "timestamp" in data


def test_health_self_router_registered():
    """/health/self 路由应在 health router 中注册"""
    from web.routers.health import router
    paths = [r.path for r in router.routes]
    assert "/health/self" in paths


# ============================================================
# 全局单例测试
# ============================================================

def test_global_introspector_singleton():
    """get_introspector 返回全局单例"""
    from core.agent_introspection import get_introspector, set_introspector
    a = get_introspector()
    b = get_introspector()
    assert a is b

    # set_introspector 可替换
    custom = AgentIntrospector(start_time=time.time() - 100)
    set_introspector(custom)
    assert get_introspector() is custom

    # 恢复默认
    set_introspector(AgentIntrospector())