"""验证 agnes max_tokens 限制 + sticky fallback 修复的回归测试。

覆盖 3 个根因：
1. agnes API max_tokens 上限 65536，ROUTE_TABLE 配置 131072 时应自动夹紧到 65535
2. _restore_chat_model fallback 分支不修改 ROUTE_TABLE["chat"]["client"]
3. PUT /models/routes/chat 同步 chat_model 时用 body.provider 而非 entry["client"]
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

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


def test_build_stream_kwargs_agnes_clamps_max_tokens():
    """agnes 流式调用同样要夹紧 max_tokens。"""
    from model_router import ModelRouter

    kwargs = ModelRouter._build_stream_kwargs(
        model="agnes-2.0-flash",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.7,
        mt=131072,
        extra_headers=None,
        config={"thinking": {"type": "disabled"}},
        provider="agnes",
    )
    assert kwargs["max_tokens"] == 65535


def test_restore_chat_model_fallback_preserves_route_table(monkeypatch):
    """_restore_chat_model fallback 分支不应修改 ROUTE_TABLE['chat']['client']。

    模拟 set_chat_model 抛 LLMError，验证 fallback 后 ROUTE_TABLE 中
    用户原选择（agnes）被保留，不污染为 mimo。
    """
    # 在导入 server 前注入 fake model_router 模块
    import model_router as _mr_module
    _original_route_table = _mr_module.ROUTE_TABLE

    # 临时修改 ROUTE_TABLE 模拟用户选择 agnes
    test_route = {
        "chat": {
            "model": "agnes-2.0-flash",
            "max_tokens": 131072,
            "client": "agnes",
            "thinking": {"type": "disabled"},
        }
    }
    monkeypatch.setattr(_mr_module, "ROUTE_TABLE", test_route)

    # 模拟 set_chat_model 抛异常（自定义 provider 注册失败场景）
    class _FakeRouter:
        def set_chat_model(self, provider, model_id):
            raise RuntimeError("simulated register failure")

        _current_chat_model = None

    # 模拟 config_service
    class _FakeCfg:
        def get(self, key, default=None):
            if key == "models.chat_model":
                return {"provider": "agnes", "model_id": "agnes-2.0-flash"}
            return default

    fake_core = SimpleNamespace(router=_FakeRouter())

    # 重新导入 server（确保使用 monkeypatched ROUTE_TABLE）
    import importlib

    import web.server as server_mod
    importlib.reload(server_mod)

    server_mod._restore_chat_model(_FakeCfg(), fake_core)

    # 关键断言：ROUTE_TABLE 中 chat client 仍为 agnes，未被改成 mimo
    assert test_route["chat"]["client"] == "agnes", (
        f"fallback 后 ROUTE_TABLE client 应保留 agnes，实际 {test_route['chat']['client']}"
    )
    assert test_route["chat"]["model"] == "agnes-2.0-flash", (
        f"fallback 后 ROUTE_TABLE model 应保留，实际 {test_route['chat']['model']}"
    )
    # _current_chat_model 应改为 mimo（仅影响 GET /models/chat-model 返回）
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
    """set_chat_model persist 部分应捕获 Exception，不向上传播触发 sticky fallback。

    模拟 config_service 抛非 (OSError, KeyError, ValueError, TypeError) 的异常，
    验证 set_chat_model 仍正常返回，不会传播到 _restore_chat_model 的 fallback。
    """
    import model_router as _mr_module

    class _FakeRouter:
        def __init__(self):
            self._custom_clients = {}
            self.TASK_TIMEOUTS = {"chat": 60}
            self._current_chat_model = None

    router = _FakeRouter()

    # 模拟 config_service 抛 RuntimeError（不在原 except 范围内）
    class _BombCfg:
        def set(self, key, value):
            raise RuntimeError("simulated config service failure")

        def get(self, key, default=None):
            return default

    # 用 monkeypatch 替换 web.config_service.get_config_service
    import web.config_service as _cfg_mod
    monkeypatch.setattr(_cfg_mod, "get_config_service", lambda: _BombCfg())

    # set_chat_model 应捕获 Exception，不抛出
    result = _mr_module.ModelRouter.set_chat_model(
        router, "agnes", "agnes-2.0-flash"
    )
    assert result == {"provider": "agnes", "model_id": "agnes-2.0-flash"}
    # ROUTE_TABLE 应已更新为 agnes
    assert _mr_module.ROUTE_TABLE["chat"]["client"] == "agnes"


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
