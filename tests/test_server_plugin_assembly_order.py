"""T5：插件装配顺序缺陷回归（后端可靠性小任务 B）。

原缺陷：web/server.py lifespan 先 `_init_lifespan_resources`（内部
core.init() → bootstrap._auto_enable_plugins），后 `_start_services` 才创建
PluginManager 并 discover + set_active_plugin_manager。core.init 时
get_active_plugin_manager() 为 None → 直接 return（仅 warning）——插件自动启用
静默失效，之后也无人补跑。

修复契约（lifespan 顺序调整）：
1. 先创建并注册 plugin manager（discover + set_active_plugin_manager），
   再执行 core.init()——保证 _auto_enable_plugins 恰好生效一次；
2. _start_services 不再重复创建 manager/discover（复用 app.state 上已注册的
   manager，避免二次 discover 语义漂移）；
3. 降级路径仍置 None。
"""
from __future__ import annotations

import sys
import types
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------

class _RecordingManager:
    """记录 discover/load/enable 调用顺序的 PluginManager 替身。"""

    instances: list["_RecordingManager"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.plugins: dict[str, Any] = {"echo": object()}
        self.discover_calls = 0
        _RecordingManager.instances.append(self)

    def discover(self, *a, **k):
        self.discover_calls += 1
        return list(self.plugins)


_active_holder: dict[str, Any] = {"pm": None}


def _install_fake_plugin_module(monkeypatch) -> None:
    mod = types.ModuleType("plugins.manager")

    class PluginManager(_RecordingManager):
        pass

    def set_active_plugin_manager(pm):
        _active_holder["pm"] = pm

    def get_active_plugin_manager():
        return _active_holder["pm"]

    mod.PluginManager = PluginManager
    mod.set_active_plugin_manager = set_active_plugin_manager
    mod.get_active_plugin_manager = get_active_plugin_manager
    monkeypatch.setitem(sys.modules, "plugins.manager", mod)
    _active_holder["pm"] = None


class _FakeCore:
    """记录 init 顺序的 core 替身；init 时读取 active plugin manager。"""

    order: list[str] = []

    def __init__(self):
        self._hook_engine = None
        self.memory = None
        self.kg = None
        # 注册必然发生在构造之后（manager 构造需要 core 实例）；
        # 生产语义是 init()/bootstrap 阶段读取，因此记录点在 init() 中
        self._manager_seen_at_init = None
        self._mcp_manager = types.SimpleNamespace(
            _clients={}, set_security_policy=lambda *a, **k: None,
        )
        self.router = types.SimpleNamespace(_current_chat_model=None)
        self.security = types.SimpleNamespace(add_owner_id=lambda *a: None)

    async def init(self):
        # 复刻 bootstrap._auto_enable_plugins 的行为：manager 未注册直接 return
        from plugins.manager import get_active_plugin_manager
        pm = get_active_plugin_manager()
        self._manager_seen_at_init = pm
        if not pm:
            _FakeCore.order.append("auto_enable_skipped")
            return
        for pid in list(pm.plugins):
            _FakeCore.order.append(f"enabled:{pid}")


# ---------------------------------------------------------------------------
# 1. lifespan 顺序：先注册 manager 再 core.init
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lifespan_registers_manager_before_core_init(monkeypatch):
    """_init_lifespan_resources 应在 core.init 前完成 manager 创建/注册，
    使 bootstrap 自动启用恰好生效一次。"""
    from web import server as server_module

    _install_fake_plugin_module(monkeypatch)
    # _init_lifespan_resources 在函数体内 from agent_core import AgentCore，
    # 必须打源模块属性补丁（打 web.server 命名空间对局部导入无效）
    monkeypatch.setattr("agent_core.AgentCore", _FakeCore, raising=False)

    # 其余 lifespan 依赖全部打桩
    async def _noop_async(*a, **k):
        return None

    monkeypatch.setattr("web.agent_registry.AgentRegistry",
                        lambda core: types.SimpleNamespace(load_persisted=_noop_async))
    monkeypatch.setattr("llm_gateway.provider_service.ProviderService",
                        lambda *a, **k: types.SimpleNamespace())
    monkeypatch.setattr("web.routers.local_ai.initialize_local_ai_services",
                        _noop_async)

    app = types.SimpleNamespace(state=types.SimpleNamespace())
    core, owns_core = await server_module._init_lifespan_resources(app)

    assert owns_core is True
    assert core._manager_seen_at_init is not None, (
        "core.init 时 active plugin manager 必须已注册"
    )
    assert app.state.plugin_manager is core._manager_seen_at_init
    assert _active_holder["pm"] is app.state.plugin_manager
