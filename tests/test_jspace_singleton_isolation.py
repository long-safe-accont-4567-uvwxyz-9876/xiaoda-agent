"""J-Space 共享单例状态隔离回归：请求级 use_llm 不得泄漏进全局运行态。

背景：web/routers/jspace.py::_get_decomposer 曾直接改写共享单例的
_use_llm——一次 use_llm=False 的 /jspace/decompose 请求会把 bootstrap
注入的核心分解器永久降级为规则模式，直到下次 True 调用。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class _StubBackend:
    """最小 FreeModelBackend 替身：只记录 set_backend 归一化结果。"""

    def __init__(self):
        self.backend = "api"
        self.set_backend_calls: list[tuple] = []

    def set_backend(self, backend, local_model=None):
        self.backend = backend
        self.set_backend_calls.append((backend, local_model))


def _install_sentinel(monkeypatch):
    from core import j_space_bootstrap
    from core.intent_decomposition import IntentDecomposer

    sentinel = IntentDecomposer(use_llm_decomposition=True)
    injected_backend = _StubBackend()
    sentinel.set_free_backend(injected_backend)
    monkeypatch.setattr(j_space_bootstrap, "_intent_decomposer", sentinel)
    return sentinel, injected_backend


def test_use_llm_false_uses_request_scoped_instance(monkeypatch):
    from core.intent_decomposition import IntentDecomposer
    from web.routers.jspace import _get_decomposer

    sentinel, backend = _install_sentinel(monkeypatch)

    request_scoped = _get_decomposer(use_llm=False)

    assert isinstance(request_scoped, IntentDecomposer)
    assert request_scoped is not sentinel
    assert request_scoped.use_llm is False
    # 全局单例不被翻转：LLM 模式与注入后端原样保留
    assert sentinel.use_llm is True
    assert sentinel._free_backend is backend


def test_subsequent_default_request_still_gets_llm_singleton(monkeypatch):
    from web.routers.jspace import _get_decomposer

    sentinel, _ = _install_sentinel(monkeypatch)
    _get_decomposer(use_llm=False)

    again = _get_decomposer(use_llm=True)
    assert again is sentinel
    assert again.use_llm is True


def test_use_llm_false_reuses_singleton_already_in_rule_mode(monkeypatch):
    from core.intent_decomposition import IntentDecomposer
    from web.routers.jspace import _get_decomposer

    from core import j_space_bootstrap

    rule_singleton = IntentDecomposer(use_llm_decomposition=False)
    rule_singleton.set_backend("off")
    monkeypatch.setattr(j_space_bootstrap, "_intent_decomposer", rule_singleton)

    result = _get_decomposer(use_llm=False)
    assert result is rule_singleton
    assert result.use_llm is False


def test_node_off_caps_request_level_use_llm(monkeypatch):
    from core import j_space_bootstrap
    from core.intent_decomposition import IntentDecomposer
    from web.routers.jspace import _get_decomposer

    capped = IntentDecomposer(use_llm_decomposition=True)
    capped.set_backend("off")
    monkeypatch.setattr(j_space_bootstrap, "_intent_decomposer", capped)

    result = _get_decomposer(use_llm=True)
    assert result is capped
    assert result.use_llm is False


def test_set_intent_backend_records_public_node_backend(monkeypatch):
    from core import j_space_bootstrap

    sentinel, backend = _install_sentinel(monkeypatch)

    j_space_bootstrap.set_intent_backend("auto")
    decomposer = j_space_bootstrap.get_intent_decomposer()
    assert decomposer is sentinel
    assert decomposer.node_backend == "api"
    assert decomposer.use_llm is True
    assert not hasattr(decomposer, "_node_backend")
    assert backend.set_backend_calls[-1] == ("api", None)

    j_space_bootstrap.set_intent_backend("off")
    assert decomposer.node_backend == "off"
    assert decomposer.use_llm is False
    # 注入的后端实例不被替换/清除，仅收到归一化后的档位
    assert decomposer._free_backend is backend
    assert backend.set_backend_calls[-1] == ("off", None)
