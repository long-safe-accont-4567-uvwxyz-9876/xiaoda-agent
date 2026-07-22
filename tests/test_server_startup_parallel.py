"""G16: web/server.py 启动并行化测试 (TDD)

验证 _start_services 中独立调度器（MemoryRecallScheduler / SpontaneousRecall /
GrowthNarrative / MailPoller）通过 asyncio.gather 并行初始化，且单点失败不影响其他模块。

参考 docs/performance_audit_2026-07-20.md G16 (Minor)。
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# 测试辅助：构造 mock app / core / 调度器类
# ============================================================

def _make_app() -> types.SimpleNamespace:
    """构造一个 mock app（仅需要 .state 属性）。"""
    return types.SimpleNamespace(state=types.SimpleNamespace())


def _make_core() -> types.SimpleNamespace:
    """构造一个 mock core（满足 _start_services 访问的属性）。"""
    return types.SimpleNamespace(
        _hook_engine=None,
        memory=None,
        kg=None,
        _mcp_manager=types.SimpleNamespace(_clients={}),
        router=types.SimpleNamespace(_current_chat_model=None),
    )


def _install_fake_module(monkeypatch, dotted_name: str, attrs: dict) -> types.ModuleType:
    """注入一个 fake 模块到 sys.modules，便于在 _start_services 内部 import 时被命中。"""
    mod = types.ModuleType(dotted_name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, dotted_name, mod)
    return mod


class _FakePluginManager:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def discover(self):
        pass


class _FakeMediaTaskQueue:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


class _FakeGreetingScheduler:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


def _patch_start_services_dependencies(monkeypatch) -> None:
    """Patch 掉 _start_services 的非测试目标依赖（保留 _init_* 函数与 asyncio.gather 真实运行）。"""
    from web import server as server_module

    # 顶层 _apply_model_overrides / _start_user_mcp_servers（同模块函数）
    async def _mock_apply_model_overrides(core):
        return None

    async def _mock_start_user_mcp_servers(core):
        return None

    monkeypatch.setattr(server_module, "_apply_model_overrides", _mock_apply_model_overrides)
    monkeypatch.setattr(server_module, "_start_user_mcp_servers", _mock_start_user_mcp_servers)

    # apply_tool_overrides
    _install_fake_module(monkeypatch, "web.routers.tools", {
        "apply_tool_overrides": lambda: None,
    })

    # ws_hub.manager / start_media_cleanup
    _install_fake_module(monkeypatch, "web.ws_hub", {
        "manager": types.SimpleNamespace(broadcast=lambda *a, **kw: None),
        "start_media_cleanup": lambda: None,
    })

    # config_service
    _install_fake_module(monkeypatch, "web.config_service", {
        "get_config_service": lambda: "fake_config_service",
    })

    # media_tasks / greeting_scheduler / plugins.manager
    _install_fake_module(monkeypatch, "web.media_tasks", {"MediaTaskQueue": _FakeMediaTaskQueue})
    _install_fake_module(monkeypatch, "web.greeting_scheduler", {
        "GreetingScheduler": _FakeGreetingScheduler,
    })
    _install_fake_module(monkeypatch, "plugins.manager", {"PluginManager": _FakePluginManager})
    # 注意：不替换 tool_engine.tool_registry —— 真实模块的导入会触发
    # tool_engine/__init__.py 重新执行（from tool_engine.tool_registry import ...），
    # 替换为空模块会引发 ImportError。由于 _FakePluginManager.discover() 是 no-op，
    # 真实的 tool_registry 模块仅被赋值给属性，不会被实际调用。

    # 关闭 QQ Bot 分支
    monkeypatch.delenv("QQBOT_APP_ID", raising=False)
    monkeypatch.delenv("ENABLE_QQ_BOT", raising=False)


# ============================================================
# 1. test_start_services_parallelizes_independent_schedulers
#    验证 4 个 _init_* 通过 asyncio.gather 并行运行
# ============================================================

async def test_start_services_parallelizes_independent_schedulers(monkeypatch):
    """G16: 4 个独立调度器应通过 asyncio.gather 并行初始化。

    并行验证策略：每个 _init_* mock 内 await asyncio.sleep(0.05)。
    若并行执行，总耗时 ~0.05s；若顺序执行，总耗时 ~0.2s。
    """
    from web import server as server_module

    _patch_start_services_dependencies(monkeypatch)

    start_times: list[float] = []

    async def _mock_init_recall(core):
        start_times.append(time.monotonic())
        await asyncio.sleep(0.05)
        return ("recall_scheduler", "recall_inst")

    async def _mock_init_spontaneous(core):
        start_times.append(time.monotonic())
        await asyncio.sleep(0.05)
        return ("spontaneous_recall", "spontaneous_inst")

    async def _mock_init_growth(core):
        start_times.append(time.monotonic())
        await asyncio.sleep(0.05)
        return ("growth_narrative", "growth_inst")

    async def _mock_init_mail(core, config_service):
        start_times.append(time.monotonic())
        await asyncio.sleep(0.05)
        return ("mail_poller", "mail_inst")

    monkeypatch.setattr(server_module, "_init_recall_scheduler", _mock_init_recall)
    monkeypatch.setattr(server_module, "_init_spontaneous_recall", _mock_init_spontaneous)
    monkeypatch.setattr(server_module, "_init_growth_narrative", _mock_init_growth)
    monkeypatch.setattr(server_module, "_init_mail_poller", _mock_init_mail)

    app = _make_app()
    core = _make_core()

    t0 = time.monotonic()
    await server_module._start_services(app, core)
    elapsed = time.monotonic() - t0

    # 4 个都跑了
    assert len(start_times) == 4, f"应有 4 个 _init_* 被调用，实际 {len(start_times)}"

    # 并行验证：所有启动时间应接近（差距 < 30ms），且总耗时 < 0.15s（顺序需 ~0.2s）
    start_spread = max(start_times) - min(start_times)
    assert start_spread < 0.03, (
        f"4 个 _init_* 启动时间差距 {start_spread*1000:.1f}ms 过大，未并行执行"
    )
    assert elapsed < 0.15, (
        f"_start_services 总耗时 {elapsed*1000:.1f}ms，疑似顺序执行（并行应 < 150ms）"
    )


# ============================================================
# 2. test_start_services_scheduler_failure_does_not_block_others
# ============================================================

async def test_start_services_scheduler_failure_does_not_block_others(monkeypatch):
    """G16: 单个调度器初始化失败（返回 None）不应阻塞其他调度器。"""
    from web import server as server_module

    _patch_start_services_dependencies(monkeypatch)

    called: list[str] = []

    async def _mock_init_recall(core):
        called.append("recall")
        # 模拟内部 try/except 已捕获异常，返回 None
        return ("recall_scheduler", None)

    async def _mock_init_spontaneous(core):
        called.append("spontaneous")
        return ("spontaneous_recall", "spontaneous_inst")

    async def _mock_init_growth(core):
        called.append("growth")
        return ("growth_narrative", "growth_inst")

    async def _mock_init_mail(core, config_service):
        called.append("mail")
        return ("mail_poller", "mail_inst")

    monkeypatch.setattr(server_module, "_init_recall_scheduler", _mock_init_recall)
    monkeypatch.setattr(server_module, "_init_spontaneous_recall", _mock_init_spontaneous)
    monkeypatch.setattr(server_module, "_init_growth_narrative", _mock_init_growth)
    monkeypatch.setattr(server_module, "_init_mail_poller", _mock_init_mail)

    app = _make_app()
    core = _make_core()

    await server_module._start_services(app, core)

    # recall 失败，但其他 3 个都被调用
    assert called == ["recall", "spontaneous", "growth", "mail"] or set(called) == {
        "recall", "spontaneous", "growth", "mail",
    }, f"其他调度器应仍被调用，实际 called={called}"

    # recall_scheduler 不应设置（保持属性不存在）；其他应设置
    assert not hasattr(app.state, "recall_scheduler") or app.state.recall_scheduler is None
    assert getattr(app.state, "spontaneous_recall", None) == "spontaneous_inst"
    assert getattr(app.state, "growth_narrative", None) == "growth_inst"
    assert getattr(app.state, "mail_poller", None) == "mail_inst"


# ============================================================
# 3. test_start_services_sets_app_state_attributes
# ============================================================

async def test_start_services_sets_app_state_attributes(monkeypatch):
    """G16: _start_services 应把成功的调度器实例写入 app.state 对应属性。"""
    from web import server as server_module

    _patch_start_services_dependencies(monkeypatch)

    async def _mock_init_recall(core):
        return ("recall_scheduler", "RECALL_OBJ")

    async def _mock_init_spontaneous(core):
        return ("spontaneous_recall", "SPONTANEOUS_OBJ")

    async def _mock_init_growth(core):
        return ("growth_narrative", "GROWTH_OBJ")

    async def _mock_init_mail(core, config_service):
        return ("mail_poller", "MAIL_OBJ")

    monkeypatch.setattr(server_module, "_init_recall_scheduler", _mock_init_recall)
    monkeypatch.setattr(server_module, "_init_spontaneous_recall", _mock_init_spontaneous)
    monkeypatch.setattr(server_module, "_init_growth_narrative", _mock_init_growth)
    monkeypatch.setattr(server_module, "_init_mail_poller", _mock_init_mail)

    app = _make_app()
    core = _make_core()

    await server_module._start_services(app, core)

    assert app.state.recall_scheduler == "RECALL_OBJ"
    assert app.state.spontaneous_recall == "SPONTANEOUS_OBJ"
    assert app.state.growth_narrative == "GROWTH_OBJ"
    assert app.state.mail_poller == "MAIL_OBJ"
    # 其他属性仍应设置
    assert app.state.plugin_manager is not None
    assert app.state.media_queue is not None
    assert app.state.greeting_scheduler is not None
    assert app.state.qq_task is None  # QQ Bot 未启用
    assert app.state.last_emotion is None


# ============================================================
# 4. test_init_recall_scheduler_failure_returns_none
#    直接对 _init_recall_scheduler 单元测试：模拟 ImportError 返回 (attr_name, None)
# ============================================================

async def test_init_recall_scheduler_failure_returns_none(monkeypatch):
    """G16: _init_recall_scheduler 在 ImportError 时返回 ('recall_scheduler', None)。"""
    from web import server as server_module

    # 让 from memory.recall_scheduler import MemoryRecallScheduler 抛 ImportError
    # 通过 sys.modules 注入一个会 raise ImportError 的模块（或直接删除）
    monkeypatch.setitem(sys.modules, "memory.recall_scheduler", None)  # None 触发 ImportError

    result = await server_module._init_recall_scheduler(_make_core())

    assert isinstance(result, tuple)
    assert result[0] == "recall_scheduler"
    assert result[1] is None


# ============================================================
# 5. test_init_mail_poller_passes_config_service
#    验证 _init_mail_poller 把 config_service 透传给 MailPoller 构造函数
# ============================================================

async def test_init_mail_poller_passes_config_service(monkeypatch):
    """G16: _init_mail_poller 应将 config_service 参数传给 MailPoller(core, config_service)。"""
    from web import server as server_module

    captured: dict[str, Any] = {}

    class _RecordingMailPoller:
        def __init__(self, core, config_service):
            captured["core"] = core
            captured["config_service"] = config_service

        def start(self):
            captured["started"] = True

    _install_fake_module(monkeypatch, "web.mail_poller", {
        "MailPoller": _RecordingMailPoller,
    })

    sentinel_core = _make_core()
    sentinel_cfg = object()  # 哨兵 config_service

    attr_name, instance = await server_module._init_mail_poller(sentinel_core, sentinel_cfg)

    assert attr_name == "mail_poller"
    assert isinstance(instance, _RecordingMailPoller)
    assert captured["core"] is sentinel_core
    assert captured["config_service"] is sentinel_cfg
    assert captured.get("started") is True
