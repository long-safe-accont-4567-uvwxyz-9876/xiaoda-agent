"""model_router Phase 6（流式执行链 Mixin 抽出）结构契约测试。

背景：chat_stream / _stream_local_chat / _classify_error / _build_route_kwargs /
_create_completion / _handle_route_response / _handle_route_exception /
_route_with_retry / _route_for_continuation 与 _cap_max_tokens 抽为
llm_gateway/router_execution.ExecutionMixin，方法体逐字节搬移
（唯一一行偏差：_build_route_kwargs 内原 ModelRouter._cap_max_tokens 裸引用
改为 ExecutionMixin._cap_max_tokens，二者经 MRO 为同一对象）。
MAX_RETRIES 常量随链搬入 mixin，model_router 顶部同名 re-export。

契约：
    1. 本模块不得 import model_router（防循环依赖）
    2. ModelRouter(ExecutionMixin) 继承：方法经 MRO 命中 Mixin 实现
    3. model_router 同名 re-export（ExecutionMixin / MAX_RETRIES）
    4. 行为语义不变（错误分类 / agnes max_tokens 裁剪走 Mixin 内 _cap_max_tokens）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import model_router
from llm_gateway.router_execution import MAX_RETRIES, ExecutionMixin

MOVED_METHODS = (
    "chat_stream", "_stream_local_chat", "_classify_error",
    "_build_route_kwargs", "_create_completion", "_handle_route_response",
    "_handle_route_exception", "_route_with_retry", "_route_for_continuation",
    "_cap_max_tokens",
)


# ── 1. 独立可导入 + 无循环依赖 ──────────────────────────────────

def test_mixin_imports_standalone():
    import importlib
    mod = importlib.import_module("llm_gateway.router_execution")
    for name in MOVED_METHODS:
        assert hasattr(mod.ExecutionMixin, name), f"缺少方法 {name}"
    assert mod.MAX_RETRIES == 1


def test_mixin_does_not_import_model_router():
    import llm_gateway.router_execution as mod
    assert "model_router" not in getattr(mod, "__dict__", {})


# ── 2. ModelRouter 继承 + MRO 命中 Mixin 实现 ────────────────────

def test_model_router_inherits_mixin():
    assert issubclass(model_router.ModelRouter, ExecutionMixin)
    for name in MOVED_METHODS:
        assert (getattr(model_router.ModelRouter, name)
                is getattr(ExecutionMixin, name)), f"{name} 未命中 Mixin 实现"


def test_model_router_reexports_moved_symbols():
    assert model_router.ExecutionMixin is ExecutionMixin
    assert model_router.MAX_RETRIES is MAX_RETRIES
    assert model_router.MAX_RETRIES == 1


# ── 3. 行为语义不变（搬移后行为不变） ────────────────────────────

def test_classify_error_classification():
    """_classify_error 分类逻辑搬移后不变（staticmethod 经 MRO 调用）。"""
    assert model_router.ModelRouter._classify_error(RuntimeError("rate limit hit")) == "rate_limit"
    assert model_router.ModelRouter._classify_error(RuntimeError("429 too many")) == "rate_limit"
    assert model_router.ModelRouter._classify_error(RuntimeError("connection refused")) == "connection_error"
    assert model_router.ModelRouter._classify_error(RuntimeError("read timeout")) == "timeout"
    assert model_router.ModelRouter._classify_error(RuntimeError("boom")) == "unknown"


def test_build_route_kwargs_clamps_via_mixin_cap():
    """_build_route_kwargs 内 ExecutionMixin._cap_max_tokens 引用可执行（agnes 上限裁剪）。"""
    kwargs = model_router.ModelRouter._build_route_kwargs(
        model="agnes-2.0-flash",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.7,
        max_tokens=131072,
        stream=False,
        tools=None,
        tool_choice=None,
        extra_headers=None,
        config={"thinking": {"type": "disabled"}},
        provider="agnes",
    )
    assert kwargs["max_tokens"] == 65535


def test_build_route_kwargs_mimo_uncapped():
    """mimo 无上限裁剪：131072 原样透传（走 Mixin 内 _cap_max_tokens）。"""
    kwargs = model_router.ModelRouter._build_route_kwargs(
        model="mimo-v2.5",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.7,
        max_tokens=131072,
        stream=False,
        tools=None,
        tool_choice=None,
        extra_headers=None,
        config={"thinking": {"type": "disabled"}},
        provider="mimo",
    )
    assert kwargs["max_tokens"] == 131072
