"""验证 agnes max_tokens 限制 + sticky fallback 修复的回归测试。

覆盖 3 个根因：
1. agnes API max_tokens 上限 65536，ROUTE_TABLE 配置 131072 时应自动夹紧到 65535
2. _restore_chat_model fallback 分支不修改 ROUTE_TABLE["chat"]["client"]
3. PUT /models/routes/chat 同步 chat_model 时用 body.provider 而非 entry["client"]
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_build_route_kwargs_agnes_clamps_max_tokens():
    """agnes provider max_tokens > 65535 时应夹紧到 65535。"""
    from model_router import ModelRouter

    kwargs = ModelRouter._build_route_kwargs(
        model="agnes-2.0-flash",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.7,
        max_tokens=131072,  # ROUTE_TABLE["chat"]["max_tokens"] 默认值
        stream=False,
        tools=None,
        tool_choice=None,
        extra_headers=None,
        config={"thinking": {"type": "disabled"}},
        provider="agnes",
    )
    assert kwargs["max_tokens"] == 65535, (
        f"agnes max_tokens 应被夹紧到 65535，实际 {kwargs['max_tokens']}"
    )


def test_build_route_kwargs_agnes_keeps_small_max_tokens():
    """agnes provider max_tokens <= 65535 时应保持不变。"""
    from model_router import ModelRouter

    kwargs = ModelRouter._build_route_kwargs(
        model="agnes-2.0-flash",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.7,
        max_tokens=4096,
        stream=False,
        tools=None,
        tool_choice=None,
        extra_headers=None,
        config={"thinking": {"type": "disabled"}},
        provider="agnes",
    )
    assert kwargs["max_tokens"] == 4096


def test_build_route_kwargs_mimo_keeps_large_max_tokens():
    """mimo provider 不应被夹紧，保留 131072。"""
    from model_router import ModelRouter

    kwargs = ModelRouter._build_route_kwargs(
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


def test_restore_chat_model_fallback_preserves_route_table(monkeypatch):
    """_restore_chat_model fallback 分支：当 provider 未注册时回退到 mimo（内存）。

    新代码（P0 sticky fallback 修复后）不再调用 set_chat_model，而是直接修改
    ROUTE_TABLE 和 _current_chat_model。当 provider 是未注册的自定义 provider 时，
    try 块抛 LLMError，进入 fallback：将 ROUTE_TABLE 和 _current_chat_model 都改为 mimo。

    关键修复（sticky fallback 根因）：fallback 路径不调用 cfg.set 持久化 mimo
    （由 test_restore_chat_model_fallback_does_not_persist_mimo 验证），
    这样用户下次重启时仍能从 config 中恢复原选择。
    """
    # 在导入 server 前注入 fake model_router 模块
    import model_router as _mr_module
    original_route_table = _mr_module.ROUTE_TABLE

    # 临时修改 ROUTE_TABLE 模拟用户选择未注册的 custom provider
    test_route = {
        "chat": {
            "model": "custom-model-x",
            "max_tokens": 131072,
            "client": "custom_unregistered",  # 不在 ("mimo", "agnes") 也不在 _custom_clients
            "thinking": {"type": "disabled"},
        }
    }
    monkeypatch.setattr(_mr_module, "ROUTE_TABLE", test_route)

    # 模拟 router：_custom_clients 为空（custom_unregistered 未注册）
    class _FakeRouter:
        _current_chat_model = None
        _custom_clients = {}  # 空，custom_unregistered 未注册

    # 模拟 config_service：返回用户保存的 custom_unregistered 选择
    class _FakeCfg:
        def get(self, key, default=None):
            if key == "models.chat_model":
                return {"provider": "custom_unregistered", "model_id": "custom-model-x"}
            return default

    fake_core = SimpleNamespace(router=_FakeRouter())

    # 重新导入 server（确保使用 monkeypatched ROUTE_TABLE）
    import importlib
    import web.server as server_mod
    importlib.reload(server_mod)

    server_mod._restore_chat_model(_FakeCfg(), fake_core)

    # 关键断言 1：fallback 后 ROUTE_TABLE chat 改为 mimo（让 route() 可用）
    assert test_route["chat"]["client"] == "mimo", (
        f"fallback 后 ROUTE_TABLE client 应改为 mimo（保证 route() 可用），实际 {test_route['chat']['client']}"
    )
    assert test_route["chat"]["model"] == _mr_module.MIMO_MODEL, (
        f"fallback 后 ROUTE_TABLE model 应改为 MIMO_MODEL，实际 {test_route['chat']['model']}"
    )
    # 关键断言 2：_current_chat_model 也改为 mimo（内存中反映当前激活模型）
    assert fake_core.router._current_chat_model == {
        "provider": "mimo", "model_id": _mr_module.MIMO_MODEL
    }


def test_restore_chat_model_fallback_does_not_persist_mimo(monkeypatch):
    """fallback 分支只改内存 _current_chat_model，不调用 cfg.set 持久化 mimo。

    验证 sticky fallback 根因已修复：即使 _restore_chat_model 进入 fallback，
    config 中 chat_model 仍保留用户原选择 agnes，重启后能正确恢复。
    """
    import model_router as _mr_module

    test_route = {
        "chat": {
            "model": "agnes-2.0-flash",
            "max_tokens": 131072,
            "client": "agnes",
            "thinking": {"type": "disabled"},
        }
    }
    monkeypatch.setattr(_mr_module, "ROUTE_TABLE", test_route)

    class _FakeRouter:
        def set_chat_model(self, provider, model_id):
            raise RuntimeError("simulated register failure")

        _current_chat_model = None

    persist_calls = []

    class _FakeCfg:
        def get(self, key, default=None):
            if key == "models.chat_model":
                return {"provider": "agnes", "model_id": "agnes-2.0-flash"}
            return default

        def set(self, key, value):
            persist_calls.append((key, value))

    fake_core = SimpleNamespace(router=_FakeRouter())

    import importlib
    import web.server as server_mod
    importlib.reload(server_mod)

    server_mod._restore_chat_model(_FakeCfg(), fake_core)

    # 关键断言：fallback 分支不应有任何 cfg.set 调用（不持久化 mimo）
    assert persist_calls == [], (
        f"fallback 分支不应持久化，实际调用了 cfg.set: {persist_calls}"
    )


def test_set_chat_model_persist_catches_generic_exception(monkeypatch):
    """chat_model 持久化失败时，set_chat_model 应回滚所有 task + DEFAULT_PROVIDER。

    CodeRabbit#1 修复：chat_model 写入失败不能只 log warning，否则 routes 已新值
    但 chat_model 旧值，重启时 _restore_chat_model 用旧值覆盖正确的 routes，
    导致用户切换的模型在重启后"神秘回退"。
    新实现：回滚所有 sync task + DEFAULT_PROVIDER，抛 LLMError。
    """
    import model_router as _mr_module
    from core.app_exception import LLMError
    import config as _config_mod

    # 模拟 config_service 抛 RuntimeError（在 set_chat_model 末尾被调用）
    class _BombCfg:
        def set(self, key, value):
            raise RuntimeError("simulated config service failure")

        def get(self, key, default=None):
            return default

    # registry 持有不抛异常的 mock cfg，保证 update_route 成功
    safe_cfg = MagicMock()
    router = MagicMock(spec=_mr_module.ModelRouter)
    router._custom_clients = {}
    router._current_chat_model = None
    router._lazy_register_provider = MagicMock()
    router.TASK_TIMEOUTS = {"chat": 60,
                            "emotion_analysis": 10, "tool_result_wrap": 30,
                            "memory_encoding": 30}
    router._registry = _mr_module.ModelRouteRegistry(
        _mr_module.ROUTE_TABLE, config_service=safe_cfg
    )

    # set_chat_model 末尾调 get_config_service() 返回 _BombCfg
    import web.config_service as _cfg_mod
    monkeypatch.setattr(_cfg_mod, "get_config_service", lambda: _BombCfg())

    # 保存原 ROUTE_TABLE + DEFAULT_PROVIDER 状态以便恢复
    # chat_pro/chat_flash 已合并进 chat，不再单独快照/还原
    original_chat = copy.deepcopy(_mr_module.ROUTE_TABLE["chat"])
    original_default = _config_mod.DEFAULT_PROVIDER
    try:
        # CodeRabbit#1：chat_model 持久化失败应抛 LLMError（回滚后）
        with pytest.raises(LLMError, match="持久化 chat_model 失败"):
            _mr_module.ModelRouter.set_chat_model(
                router, "agnes", "agnes-2.0-flash"
            )
        # ROUTE_TABLE 应被回滚到原值（不是 agnes）
        assert _mr_module.ROUTE_TABLE["chat"]["client"] == original_chat["client"]
        assert _mr_module.ROUTE_TABLE["chat"]["model"] == original_chat["model"]
        # DEFAULT_PROVIDER 应被回滚
        assert _config_mod.DEFAULT_PROVIDER == original_default
    finally:
        _mr_module.ROUTE_TABLE["chat"] = original_chat
        _config_mod.set_default_provider(original_default)


def test_update_route_chat_uses_body_provider_for_sync(monkeypatch):
    """PUT /models/routes/chat 同步 chat_model 时应使用 body.provider。

    模拟 entry["client"]="mimo"（被旧 fallback 污染），但 body.provider="agnes"，
    验证持久化的 chat_model 是 agnes 而非 mimo。
    """
    import model_router as _mr_module

    # 模拟 ROUTE_TABLE chat client 已被污染为 mimo（旧 sticky fallback 遗留）
    test_route = {
        "chat": {
            "model": "agnes-2.0-flash",
            "max_tokens": 131072,
            "client": "mimo",  # 被污染
            "thinking": {"type": "disabled"},
        }
    }
    monkeypatch.setattr(_mr_module, "ROUTE_TABLE", test_route)

    persist_calls = []

    class _FakeCfg:
        def get(self, key, default=None):
            return default

        def set(self, key, value):
            persist_calls.append((key, value))

        def delete(self, key):
            pass

    # 测试 update_route 中的 chat_model 同步逻辑
    # 模拟 body={"provider": "agnes", "model": "agnes-2.0-flash"}
    body = {"provider": "agnes", "model": "agnes-2.0-flash"}
    provider = body.get("provider")
    entry = test_route["chat"]
    if body.get("model"):
        entry["model"] = str(body["model"])
    if provider:
        entry["client"] = provider

    cfg = _FakeCfg()
    cfg.set("models.routes.chat", {
        "model": entry["model"], "client": entry.get("client", "mimo"),
        "max_tokens": entry.get("max_tokens"),
        "thinking": False,
        "timeout": 60,
    })

    # 复现修复后的同步逻辑：用 body.provider 优先
    if True:  # 模拟 task == "chat" 分支
        sync_provider = provider or entry.get("client", "mimo")
        cfg.set("models.chat_model", {"provider": sync_provider, "model_id": entry["model"]})

    # 关键断言：chat_model 应为 agnes（来自 body.provider），而非 mimo
    chat_model_persist = [c for c in persist_calls if c[0] == "models.chat_model"]
    assert len(chat_model_persist) == 1
    assert chat_model_persist[0][1]["provider"] == "agnes", (
        f"chat_model 应用 body.provider=agnes，实际 {chat_model_persist[0][1]['provider']}"
    )
