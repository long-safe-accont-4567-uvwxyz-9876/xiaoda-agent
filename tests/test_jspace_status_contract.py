"""J-Space status 契约回归（2026-08-26 运行时实测发现）。

Bug：GET /api/v1/jspace/status 的 data.intent_decomposer.active 恒为 False——
探测循环只覆盖 signal_stream/direction_registry/intervention_loop/
structured_blackboard/enhanced_router 五个组件，漏掉了惰性创建的
IntentDecomposer 单例。而 /jspace/decompose 实际可用，状态面板与真实
运行态矛盾。

修复：web/routers/jspace.py::jspace_status 现通过 get_intent_decomposer()
探测单例，active=True 时如实回填 use_llm。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_status_reports_active_intent_decomposer(monkeypatch):
    from core import j_space_bootstrap
    from core.intent_decomposition import IntentDecomposer
    from web.routers.jspace import jspace_status

    sentinel = IntentDecomposer(use_llm_decomposition=True)
    monkeypatch.setattr(j_space_bootstrap, "_intent_decomposer", sentinel)

    class _FakeRequest:  # jspace_status 只读 request.app.state.core 之外未用到 request
        pass

    import asyncio

    envelope = asyncio.run(jspace_status(_FakeRequest()))
    payload = envelope.data
    assert payload["intent_decomposer"]["active"] is True
    assert payload["intent_decomposer"]["use_llm"] is True


def test_status_reports_rule_mode_use_llm_false(monkeypatch):
    """节点 off（规则模式）时 active=True 且 use_llm 如实为 False，不误报 LLM。"""
    from core import j_space_bootstrap
    from core.intent_decomposition import IntentDecomposer
    from web.routers.jspace import jspace_status

    rule_singleton = IntentDecomposer(use_llm_decomposition=False)
    rule_singleton.set_backend("off")
    monkeypatch.setattr(j_space_bootstrap, "_intent_decomposer", rule_singleton)

    import asyncio

    envelope = asyncio.run(jspace_status(object()))
    payload = envelope.data
    assert payload["intent_decomposer"]["active"] is True
    assert payload["intent_decomposer"]["use_llm"] is False
