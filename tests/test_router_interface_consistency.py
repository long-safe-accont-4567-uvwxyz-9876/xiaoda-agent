"""路由接口一致性回归测试（2026-11-08：中等-3/4/5/6/7）。

对应三条修复：
- 切模型接口先探活，失败返回错误而不是 success；成功后 client 真的被物化；
- 空 model / 未注册 provider 回退后 provider/model/base_url 三元组一致；
- 默认 provider 不再在 import 时冻结，set_default_provider 同步 MODEL_NAME。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import types

import pytest

import config
import model_router
from config import get_builtin_providers, get_default_model_for_provider
from llm_gateway import provider_probe
from llm_gateway.agent_target import resolve_agent_target, runtime_default_provider
from llm_gateway.provider_probe import (
    ProviderProbeError,
    materialize_provider_client,
    provider_client_ready,
)
from llm_gateway.route_defaults import (
    factory_snapshot,
    install_route_table,
    refresh_route_defaults,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent


class FakeRouter:
    """只实现切模型路径会用到的那几个方法。"""

    def __init__(self) -> None:
        self.custom: dict = {}
        self.builtin: dict = {}

    def has_custom_client(self, provider):
        return provider in self.custom

    def get_custom_client(self, provider):
        return self.custom.get(provider)

    def set_custom_client(self, provider, client):
        self.custom[provider] = client

    def get_builtin_client(self, provider):
        return self.builtin.get(provider)

    def bind_builtin(self, provider, client):
        old = self.builtin.get(provider)
        self.builtin[provider] = client
        return old


def _make_agent():
    from agent_core.sub_agent import SubAgent

    agent = types.SimpleNamespace(
        config=types.SimpleNamespace(
            name="xiaolang",
            provider="agnes",
            model="agnes-2.5-flash",
            base_url="https://agnes.example/v1",
            api_key_env="AGNES_API_KEY",
        ),
        _router=FakeRouter(),
        _initialized=True,
        _degraded=True,
    )
    agent.reload_model_config = types.MethodType(SubAgent.reload_model_config, agent)
    return agent


def _registry_stub(agent, saved):
    return types.SimpleNamespace(
        _disabled=set(),
        _require=lambda name: agent,
        _resolve_provider_info=lambda provider: ("https://deepseek.example/v1", "DEEPSEEK_API_KEY"),
        _save_config=lambda cfg: saved.append(cfg.provider),
        _serialize=lambda cfg, enabled=True: {
            "name": cfg.name, "provider": cfg.provider, "model": cfg.model,
        },
    )


# ── 中等-3/4：切换接口 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_switch_fails_loudly_when_probe_fails(monkeypatch):
    """探活失败必须抛错，且不报 success、不落盘、不改字段。"""
    from web.agent_registry import AgentRegistry

    agent = _make_agent()
    saved: list = []
    stub = _registry_stub(agent, saved)

    async def _boom(*args, **kwargs):
        raise ProviderProbeError("凭证被拒（HTTP 401）")

    monkeypatch.setattr(provider_probe, "probe_provider_model", _boom)
    monkeypatch.setattr("web.agent_registry.probe_provider_model", _boom)

    with pytest.raises(ValueError) as exc:
        await AgentRegistry.set_agent_model(stub, "xiaolang", "deepseek", "deepseek-chat")
    assert "未切换" in str(exc.value)
    assert agent.config.provider == "agnes"
    assert agent.config.model == "agnes-2.5-flash"
    assert saved == []
    assert agent._router.custom == {}


@pytest.mark.asyncio
async def test_switch_success_rebuilds_client(monkeypatch):
    """探活通过后 client 被物化到 router，响应里带 client_rebuilt/probe。"""
    from web.agent_registry import AgentRegistry

    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key-not-a-secret")

    agent = _make_agent()
    saved: list = []
    stub = _registry_stub(agent, saved)

    async def _ok(*args, **kwargs):
        return {"probe": "models", "status": 200}

    monkeypatch.setattr(provider_probe, "probe_provider_model", _ok)
    monkeypatch.setattr("web.agent_registry.probe_provider_model", _ok)

    data = await AgentRegistry.set_agent_model(stub, "xiaolang", "deepseek", "deepseek-chat")
    assert data["client_rebuilt"] is True
    assert data["probe"]["probe"] == "models"
    assert agent.config.provider == "deepseek"
    assert agent.config.model == "deepseek-chat"
    assert provider_client_ready(agent._router, "deepseek") is True
    assert saved == ["deepseek"]


@pytest.mark.asyncio
async def test_reload_without_key_keeps_old_config():
    """缺 Key 时不得留半更新：config 一字不改，返回 False。"""
    agent = _make_agent()
    ok = await agent.reload_model_config(
        "nonexistent-provider", "some-model",
        "https://nowhere.example/v1", "PROVIDER_THIS_KEY_DOES_NOT_EXIST",
    )
    assert ok is False
    assert agent.config.provider == "agnes"
    assert agent.config.model == "agnes-2.5-flash"
    assert agent.config.api_key_env == "AGNES_API_KEY"


def test_materialize_registers_client(monkeypatch):
    monkeypatch.setenv("PROVIDER_UNITTEST_KEY", "sk-unit-test")
    router = FakeRouter()
    materialize_provider_client(router, "unittestprov", "https://u.example/v1", "PROVIDER_UNITTEST_KEY")
    assert provider_client_ready(router, "unittestprov") is True
    try:
        materialize_provider_client(router, "unittestprov", "https://u.example/v1", "PROVIDER_MISSING_KEY_XYZ")
    except ProviderProbeError as exc:
        assert "API Key" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("缺 Key 时必须抛 ProviderProbeError")


# ── 中等-5/6：三元组同源回退 ─────────────────────────────────


def _other_builtin_provider() -> str:
    default = runtime_default_provider()
    for provider in sorted(get_builtin_providers()):
        if provider != default and get_default_model_for_provider(provider):
            return provider
    return ""


def test_empty_model_uses_same_provider_default():
    """空 model 必须取该 provider 自己的默认模型，不是默认 provider 的。"""
    default_provider = runtime_default_provider()
    default_model = get_default_model_for_provider(default_provider)
    target = resolve_agent_target(
        {"model": ""},
        default_provider=default_provider, default_model=default_model, source="test",
    )
    assert target.provider == default_provider
    assert target.model == default_model

    other = _other_builtin_provider()
    if not other or not default_model:
        pytest.skip("没有第二个带默认模型的内置 provider")
    other_model = get_default_model_for_provider(other)
    target = resolve_agent_target(
        {"provider": other, "model": ""},
        default_provider=default_provider, default_model=default_model, source="test",
    )
    assert target.provider == other
    assert target.model == other_model
    assert target.model != default_model
    assert "model_empty" in target.fallback_reasons


def test_unregistered_provider_falls_back_whole_triple():
    """provider 不可解析时 model/base_url 一起回退，不留错配。"""
    target = resolve_agent_target(
        {
            "provider": "no-such-provider",
            "model": "ghost-model",
            "base_url": "https://evil.example/v1",
            "api_key_env": "GHOST_KEY",
        },
        default_provider=runtime_default_provider(),
        default_model=get_default_model_for_provider(runtime_default_provider()),
        source="test",
    )
    assert target.provider == runtime_default_provider()
    assert target.model == get_default_model_for_provider(runtime_default_provider())
    assert target.base_url != "https://evil.example/v1"
    assert target.api_key_env != "GHOST_KEY"
    assert "provider_unregistered" in target.fallback_reasons
    assert target.degraded is True


def test_empty_provider_uses_default_triple():
    default_provider = runtime_default_provider()
    default_model = get_default_model_for_provider(default_provider)
    target = resolve_agent_target(
        {"model": ""},
        default_provider=default_provider, default_model=default_model, source="test",
    )
    assert target.provider == runtime_default_provider()
    assert target.model == get_default_model_for_provider(runtime_default_provider())
    assert "provider_empty" in target.fallback_reasons


# ── 中等-7：默认值不再 import 期冻结 ─────────────────────────


def test_set_default_provider_syncs_model_name(monkeypatch):
    old_provider = config.DEFAULT_PROVIDER
    old_model = config.MODEL_NAME
    monkeypatch.setattr(config, "_MODEL_NAME_FROM_ENV", False)
    try:
        config.set_default_provider("mimo")
        assert config.DEFAULT_PROVIDER == "mimo"
        assert config.MODEL_NAME == (get_default_model_for_provider("mimo") or old_model)
        assert runtime_default_provider() == "mimo"
        assert runtime_default_provider() == "mimo"
    finally:
        config.set_default_provider(old_provider)
        config.MODEL_NAME = old_model


def test_refresh_route_defaults_follows_runtime_provider(monkeypatch):
    old_provider = config.DEFAULT_PROVIDER
    monkeypatch.setattr(config, "_MODEL_NAME_FROM_ENV", False)
    try:
        target_provider = _other_builtin_provider() or "mimo"
        config.set_default_provider(target_provider)
        refresh_route_defaults()
        chat = model_router.ROUTE_TABLE["chat"]
        assert chat["client"] == target_provider
        assert chat["model"] == get_default_model_for_provider(target_provider)
    finally:
        config.set_default_provider(old_provider)
        refresh_route_defaults()


def test_no_import_time_frozen_default_provider():
    """这几个模块不得再把默认 provider 在 import 时取成常量。"""
    for rel in (
        "model_router.py",
        "llm_gateway/fallback_chain.py",
        "llm_gateway/router_execution.py",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "_CFG_DEFAULT_PROVIDER" not in text, rel
    bootstrap = (ROOT / "core/bootstrap.py").read_text(encoding="utf-8")
    assert "DEFAULT_PROVIDER as _DEFAULT_PROVIDER" not in bootstrap


def test_bootstrap_uses_shared_resolver():
    """bootstrap 必须走同源解析，不再自己拼 provider/model/base_url。"""
    text = (ROOT / "core/bootstrap.py").read_text(encoding="utf-8")
    assert "resolve_agent_target" in text
    assert 'persisted.get("model") or _agent_model' not in text
    assert "_resolve_base_url(persisted)" not in text


# ── 中等-7 收尾（2026-11-08）：出厂快照必须在 import 期从代码常量取得 ──────


def test_factory_snapshot_holds_code_defaults_after_webui_override(monkeypatch):
    """WebUI 覆盖写进 ROUTE_TABLE 之后，出厂快照仍等于代码里的默认值。"""
    snapshot = factory_snapshot()
    entry = model_router.ROUTE_TABLE["chat"]
    monkeypatch.setitem(entry, "client", "webui-override-provider")
    monkeypatch.setitem(entry, "model", "webui-override-model")
    assert snapshot["chat"] == {
        "client": model_router._CFG_INITIAL_PROVIDER,
        "model": model_router._CFG_MODEL_NAME,
    }
    # 快照覆盖全部路由项，且是副本：表被改写后重新取快照仍是出厂值
    assert set(snapshot) == set(model_router.ROUTE_TABLE)
    assert entry["model"] == "webui-override-model"
    assert factory_snapshot()["chat"]["model"] != "webui-override-model"


def test_refresh_keeps_user_modified_route(monkeypatch):
    """改过的路由项刷新后不被默认 provider 覆盖；没改过的项跟着默认走。"""
    snapshot = factory_snapshot()
    table = {
        "chat": {"client": "user-provider", "model": "user-model"},
        "emotion_analysis": dict(snapshot["emotion_analysis"]),
    }
    install_route_table(table, snapshot)
    try:
        target = _other_builtin_provider() or "mimo"
        monkeypatch.setattr(
            "llm_gateway.route_defaults.runtime_default_provider", lambda: target
        )
        changed = refresh_route_defaults()
        assert table["chat"] == {"client": "user-provider", "model": "user-model"}
        assert table["emotion_analysis"]["client"] == target
        assert table["emotion_analysis"]["model"] == get_default_model_for_provider(target)
        assert changed == 1
    finally:
        install_route_table(model_router.ROUTE_TABLE, snapshot)


def test_factory_snapshot_installed_at_import_time():
    """快照必须在 ROUTE_TABLE 定义处安装，不能退回首次 refresh 惰性安装。"""
    code = (
        "import model_router\n"
        "from llm_gateway import route_defaults\n"
        "print('TASKS', len(route_defaults.factory_snapshot()))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    assert "TASKS 5" in proc.stdout
