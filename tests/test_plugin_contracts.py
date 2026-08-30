"""插件隔离、生命周期、配置与依赖编排契约测试。"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config as config_module
import plugins.context as context_module
import plugins.manager as manager_module
from core.bootstrap import AgentCoreBootstrapper
from hooks import HookEngine
from plugins.manager import PluginManager, PluginState
from tool_engine.tool_registry import get_tool, unregister_tool
from web.routers.auth import get_current_user
from web.routers.plugins import router as plugins_router


def _write_plugin(
    root: Path,
    plugin_id: str,
    *,
    code: str | None = None,
    entrypoint: str = "shared:FixturePlugin",
    manifest_extra: str = "",
) -> Path:
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        f"id: {plugin_id}\n"
        f"name: {plugin_id}\n"
        "version: 1.0.0\n"
        f"entrypoint: '{entrypoint}'\n"
        f"{manifest_extra}",
        encoding="utf-8",
    )
    if code is not None:
        (plugin_dir / "shared.py").write_text(code, encoding="utf-8")
    return plugin_dir


def _tool_plugin_code(value: str, *, fail_enable: bool = False) -> str:
    enable_body = "raise RuntimeError('enable exploded')" if fail_enable else "return None"
    return f'''from plugins.sdk import Plugin, register_tool

class FixturePlugin(Plugin):
    async def on_enable(self):
        {enable_body}

    @register_tool("value")
    async def value(self):
        return {value!r}
'''


@pytest.fixture(autouse=True)
def _disable_plugin_tofu(monkeypatch):
    monkeypatch.setenv("PLUGINS_TRUST_MODE", "off")


@pytest.mark.asyncio
async def test_same_entrypoint_module_is_isolated_and_unloads_only_owner(tmp_path):
    _write_plugin(tmp_path, "isolated_a", code=_tool_plugin_code("A"))
    _write_plugin(tmp_path, "isolated_b", code=_tool_plugin_code("B"))
    manager = PluginManager(tool_registry=object())
    manager.discover([tmp_path])

    try:
        assert await manager.load("isolated_a")
        assert await manager.enable("isolated_a")
        assert await manager.load("isolated_b")
        assert await manager.enable("isolated_b")
        tool_a = get_tool("isolated_a__value")
        tool_b = get_tool("isolated_b__value")
        assert tool_a is not None and await tool_a["func"]() == "A"
        assert tool_b is not None and await tool_b["func"]() == "B"

        record_a = manager.get_plugin("isolated_a")
        record_b = manager.get_plugin("isolated_b")
        assert record_a is not None and record_b is not None
        assert record_a.loaded_module_names.isdisjoint(record_b.loaded_module_names)

        assert await manager.unload("isolated_a")
        remaining = get_tool("isolated_b__value")
        assert remaining is not None and await remaining["func"]() == "B"
    finally:
        unregister_tool("isolated_a__value")
        unregister_tool("isolated_b__value")


@pytest.mark.asyncio
async def test_legacy_absolute_sibling_imports_are_isolated_and_leave_no_bare_module(
    tmp_path,
):
    original_helper = sys.modules.pop("helper", None)
    path_before = list(sys.path)
    manager = PluginManager(tool_registry=object())
    try:
        for plugin_id, marker in (("legacy_a", "A"), ("legacy_b", "B")):
            plugin_dir = _write_plugin(
                tmp_path,
                plugin_id,
                code='''import helper
from plugins.sdk import Plugin, register_tool

class FixturePlugin(Plugin):
    @register_tool("value")
    async def value(self):
        return helper.MARKER
''',
            )
            (plugin_dir / "helper.py").write_text(
                f"MARKER = {marker!r}\n",
                encoding="utf-8",
            )
        manager.discover([tmp_path])

        assert await manager.load("legacy_a")
        assert sys.path == path_before
        assert "helper" not in sys.modules
        assert await manager.enable("legacy_a")
        assert await manager.load("legacy_b")
        assert sys.path == path_before
        assert "helper" not in sys.modules
        assert await manager.enable("legacy_b")

        tool_a = get_tool("legacy_a__value")
        tool_b = get_tool("legacy_b__value")
        assert tool_a is not None and await tool_a["func"]() == "A"
        assert tool_b is not None and await tool_b["func"]() == "B"

        assert await manager.unload("legacy_a")
        remaining = get_tool("legacy_b__value")
        assert remaining is not None and await remaining["func"]() == "B"
        assert "helper" not in sys.modules
    finally:
        await manager.shutdown_all()
        unregister_tool("legacy_a__value")
        unregister_tool("legacy_b__value")
        sys.modules.pop("helper", None)
        if original_helper is not None:
            sys.modules["helper"] = original_helper


@pytest.mark.asyncio
async def test_legacy_import_temporarily_evicts_and_restores_existing_bare_module(
    tmp_path,
):
    plugin_dir = _write_plugin(
        tmp_path,
        "legacy_restore",
        code='''import helper
from plugins.sdk import Plugin

class FixturePlugin(Plugin):
    marker = helper.MARKER
''',
    )
    (plugin_dir / "helper.py").write_text("MARKER = 'local'\n", encoding="utf-8")
    previous = sys.modules.get("helper")
    external = types.ModuleType("helper")
    external.MARKER = "external"
    sys.modules["helper"] = external
    manager = PluginManager()
    manager.discover([tmp_path])

    try:
        assert await manager.load("legacy_restore")
        record = manager.get_plugin("legacy_restore")
        assert record.instance.marker == "local"
        assert sys.modules["helper"] is external
    finally:
        await manager.unload("legacy_restore")
        if previous is None:
            sys.modules.pop("helper", None)
        else:
            sys.modules["helper"] = previous


@pytest.mark.asyncio
async def test_single_file_relative_imports_isolate_colliding_sanitized_plugin_ids(
    tmp_path,
):
    for plugin_id, marker in (("a-b", "dash"), ("a_b", "underscore")):
        plugin_dir = _write_plugin(
            tmp_path,
            plugin_id,
            entrypoint="module:FixturePlugin",
        )
        (plugin_dir / "helper.py").write_text(
            f"MARKER = {marker!r}\n",
            encoding="utf-8",
        )
        (plugin_dir / "module.py").write_text(
            "from plugins.sdk import Plugin, register_tool\n"
            "from .helper import MARKER\n\n"
            "class FixturePlugin(Plugin):\n"
            "    @register_tool('value')\n"
            "    async def value(self):\n"
            "        return MARKER\n",
            encoding="utf-8",
        )
    manager = PluginManager(tool_registry=object())
    manager.discover([tmp_path])

    try:
        for plugin_id in ("a-b", "a_b"):
            assert await manager.load(plugin_id)
            assert await manager.enable(plugin_id)
        dash = manager.get_plugin("a-b")
        underscore = manager.get_plugin("a_b")
        assert dash.module_namespace != underscore.module_namespace
        assert dash.loaded_module_names.isdisjoint(underscore.loaded_module_names)
        assert await get_tool("a-b__value")["func"]() == "dash"
        assert await get_tool("a_b__value")["func"]() == "underscore"
    finally:
        await manager.shutdown_all()
        unregister_tool("a-b__value")
        unregister_tool("a_b__value")


@pytest.mark.asyncio
async def test_dotted_entrypoint_rejects_external_parent_package_symlink(tmp_path):
    plugin_dir = _write_plugin(
        tmp_path,
        "symlink_parent",
        entrypoint="package.entry:FixturePlugin",
    )
    package_dir = plugin_dir / "package"
    package_dir.mkdir()
    outside_init = tmp_path / "outside_init.py"
    outside_init.write_text("RAISED = True\n", encoding="utf-8")
    (package_dir / "__init__.py").symlink_to(outside_init)
    (package_dir / "entry.py").write_text(
        "from plugins.sdk import Plugin\n"
        "class FixturePlugin(Plugin):\n"
        "    pass\n",
        encoding="utf-8",
    )
    manager = PluginManager()
    manager.discover([tmp_path])

    assert not await manager.load("symlink_parent")
    record = manager.get_plugin("symlink_parent")
    assert record.state == PluginState.ERROR
    assert "inside the plugin directory" in record.error_message


@pytest.mark.asyncio
async def test_package_entrypoint_supports_relative_plugin_imports(tmp_path):
    plugin_dir = _write_plugin(
        tmp_path,
        "package_fixture",
        entrypoint="package:FixturePlugin",
    )
    package_dir = plugin_dir / "package"
    package_dir.mkdir()
    (package_dir / "helper.py").write_text("VALUE = 'package-ok'\n", encoding="utf-8")
    (package_dir / "__init__.py").write_text(
        "from plugins.sdk import Plugin, register_tool\n"
        "from .helper import VALUE\n\n"
        "class FixturePlugin(Plugin):\n"
        "    @register_tool('value')\n"
        "    async def value(self):\n"
        "        return VALUE\n",
        encoding="utf-8",
    )
    manager = PluginManager(tool_registry=object())
    manager.discover([tmp_path])

    try:
        assert await manager.load("package_fixture")
        assert await manager.enable("package_fixture")
        tool = get_tool("package_fixture__value")
        assert tool is not None and await tool["func"]() == "package-ok"
    finally:
        unregister_tool("package_fixture__value")


@pytest.mark.asyncio
async def test_registrations_follow_enable_disable_reenable_lifecycle(tmp_path):
    _write_plugin(tmp_path, "lifecycle_fixture", code=_tool_plugin_code("live"))
    manager = PluginManager(tool_registry=object())
    manager.discover([tmp_path])

    try:
        assert await manager.load("lifecycle_fixture")
        assert get_tool("lifecycle_fixture__value") is None
        assert await manager.enable("lifecycle_fixture")
        assert get_tool("lifecycle_fixture__value") is not None
        assert await manager.disable("lifecycle_fixture")
        assert get_tool("lifecycle_fixture__value") is None
        assert await manager.enable("lifecycle_fixture")
        assert get_tool("lifecycle_fixture__value") is not None
    finally:
        unregister_tool("lifecycle_fixture__value")


@pytest.mark.asyncio
async def test_real_hook_engine_subscriptions_follow_plugin_lifecycle(tmp_path):
    _write_plugin(
        tmp_path,
        "hook_fixture",
        code='''from plugins.sdk import Plugin, subscribe

class FixturePlugin(Plugin):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def on_load(self):
        self.ctx.subscribe("post_tool_use", self.on_post_tool)

    @subscribe("pre_tool_use")
    async def on_pre_tool(self, context):
        self.calls.append(("async", context["tool_name"]))

    def on_post_tool(self, context):
        self.calls.append(("sync", context["tool_name"]))
''',
    )
    engine = HookEngine()
    manager = PluginManager(hook_engine=engine)
    manager.discover([tmp_path])

    assert await manager.load("hook_fixture")
    instance = manager.get_plugin("hook_fixture").instance
    await engine.fire_pre_tool_use("before-enable", {})
    await engine.fire_post_tool_use("before-enable", {}, "")
    assert instance.calls == []

    assert await manager.enable("hook_fixture")
    await engine.fire_pre_tool_use("enabled", {})
    await engine.fire_post_tool_use("enabled", {}, "")
    assert instance.calls == [("async", "enabled"), ("sync", "enabled")]

    assert await manager.disable("hook_fixture")
    await engine.fire_pre_tool_use("disabled", {})
    await engine.fire_post_tool_use("disabled", {}, "")
    assert len(instance.calls) == 2

    assert await manager.enable("hook_fixture")
    await engine.fire_pre_tool_use("re-enabled", {})
    await engine.fire_post_tool_use("re-enabled", {}, "")
    assert instance.calls == [
        ("async", "enabled"),
        ("sync", "enabled"),
        ("async", "re-enabled"),
        ("sync", "re-enabled"),
    ]


@pytest.mark.asyncio
async def test_repeated_load_and_enable_are_idempotent(tmp_path):
    _write_plugin(
        tmp_path,
        "idempotent_fixture",
        code='''from plugins.sdk import Plugin, subscribe

class FixturePlugin(Plugin):
    def __init__(self):
        super().__init__()
        self.load_calls = 0
        self.enable_calls = 0
        self.event_calls = 0

    async def on_load(self):
        self.load_calls += 1

    async def on_enable(self):
        self.enable_calls += 1

    @subscribe("pre_tool_use")
    async def on_pre_tool(self, context):
        self.event_calls += 1
''',
    )
    engine = HookEngine()
    manager = PluginManager(hook_engine=engine)
    manager.discover([tmp_path])

    assert await manager.load("idempotent_fixture")
    instance = manager.get_plugin("idempotent_fixture").instance
    assert await manager.load("idempotent_fixture")
    assert instance.load_calls == 1

    assert await manager.enable("idempotent_fixture")
    assert await manager.enable("idempotent_fixture")
    assert await manager.load("idempotent_fixture")
    assert instance.load_calls == 1
    assert instance.enable_calls == 1
    await engine.fire_pre_tool_use("once", {})
    assert instance.event_calls == 1


@pytest.mark.asyncio
async def test_on_load_registrations_stay_hidden_until_enable(tmp_path):
    _write_plugin(
        tmp_path,
        "manual_registration",
        code='''from plugins.sdk import Plugin

class FixturePlugin(Plugin):
    async def on_load(self):
        self.ctx.register_tool("manual", self.manual)

    async def manual(self):
        return "manual-live"
''',
    )
    manager = PluginManager(tool_registry=object())
    manager.discover([tmp_path])

    try:
        assert await manager.load("manual_registration")
        assert get_tool("manual_registration__manual") is None
        assert await manager.enable("manual_registration")
        tool = get_tool("manual_registration__manual")
        assert tool is not None and await tool["func"]() == "manual-live"
        assert await manager.disable("manual_registration")
        assert get_tool("manual_registration__manual") is None
        assert await manager.enable("manual_registration")
        assert get_tool("manual_registration__manual") is not None
    finally:
        unregister_tool("manual_registration__manual")


@pytest.mark.asyncio
async def test_disable_failure_clears_registrations(tmp_path):
    _write_plugin(
        tmp_path,
        "disable_failure",
        code='''from plugins.sdk import Plugin, register_tool

class FixturePlugin(Plugin):
    async def on_disable(self):
        raise RuntimeError("disable exploded")

    @register_tool("value")
    async def value(self):
        return "live"
''',
    )
    manager = PluginManager(tool_registry=object())
    manager.discover([tmp_path])

    assert await manager.load("disable_failure")
    assert await manager.enable("disable_failure")
    assert not await manager.disable("disable_failure")
    assert manager.get_plugin("disable_failure").state == PluginState.ERROR
    assert get_tool("disable_failure__value") is None


@pytest.mark.asyncio
async def test_on_load_failure_cleans_context_tasks_and_modules(tmp_path):
    _write_plugin(
        tmp_path,
        "load_failure",
        code='''import asyncio
from plugins.sdk import Plugin

class FixturePlugin(Plugin):
    async def on_load(self):
        self.ctx.spawn_task("pending", asyncio.sleep(60))
        raise RuntimeError("load exploded")
''',
    )
    manager = PluginManager()
    manager.discover([tmp_path])

    assert not await manager.load("load_failure")
    record = manager.get_plugin("load_failure")
    assert record.state == PluginState.ERROR
    assert record.instance is None and record.context is None
    assert not record.loaded_module_names
    assert not any(name.startswith("_xiaoda_plugin_load_failure_") for name in sys.modules)
    await asyncio.sleep(0)
    assert not [
        task for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and task.get_name() == "plugin:load_failure:pending"
        and not task.done()
    ]


@pytest.mark.asyncio
async def test_enable_and_activation_failures_leave_no_registrations(tmp_path, monkeypatch):
    _write_plugin(
        tmp_path,
        "enable_failure",
        code=_tool_plugin_code("hidden", fail_enable=True),
    )
    _write_plugin(tmp_path, "activation_failure", code=_tool_plugin_code("hidden"))
    manager = PluginManager(tool_registry=object())
    manager.discover([tmp_path])

    assert await manager.load("enable_failure")
    assert not await manager.enable("enable_failure")
    assert manager.get_plugin("enable_failure").state == PluginState.ERROR
    assert get_tool("enable_failure__value") is None

    assert await manager.load("activation_failure")
    instance = manager.get_plugin("activation_failure").instance
    original_activate = instance.activate_registrations

    def _partial_activation() -> None:
        original_activate()
        raise RuntimeError("activation exploded")

    monkeypatch.setattr(instance, "activate_registrations", _partial_activation)
    assert not await manager.enable("activation_failure")
    assert manager.get_plugin("activation_failure").state == PluginState.ERROR
    assert get_tool("activation_failure__value") is None


def _plugin_api_client(manager: PluginManager) -> TestClient:
    app = FastAPI()
    app.include_router(plugins_router)
    app.state.plugin_manager = manager
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    return TestClient(app)


def test_plugin_config_routes_and_frontend_use_same_contract(tmp_path, monkeypatch):
    config_root = tmp_path / "plugin-config"
    monkeypatch.setattr(config_module, "PLUGINS_CONFIG_DIR", config_root)
    _write_plugin(
        tmp_path,
        "config_fixture",
        code=_tool_plugin_code("unused"),
        manifest_extra=(
            "config:\n"
            "  greeting: hello\n"
            "config_schema:\n"
            "  greeting:\n"
            "    type: string\n"
            "    label: Greeting\n"
        ),
    )
    manager = PluginManager()
    manager.discover([tmp_path])
    client = _plugin_api_client(manager)

    response = client.get("/plugins/config_fixture/config")
    assert response.status_code == 200
    assert response.json()["data"] == {
        "schema": {"greeting": {"type": "string", "label": "Greeting"}},
        "values": {"greeting": "hello"},
    }
    assert client.put(
        "/plugins/config_fixture/config",
        json={"config": {"greeting": "updated"}},
    ).status_code == 200
    assert manager.get_plugin_config("config_fixture") == {"greeting": "updated"}

    frontend = (
        Path(__file__).resolve().parents[1]
        / "web/frontend/src/views/PluginsView.vue"
    ).read_text(encoding="utf-8")
    assert "{ config: configValues.value }" in frontend


def test_plugin_storage_paths_do_not_depend_on_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGINS_TRUST_MODE", "on")
    plugin_root = tmp_path / "fixtures"
    config_root = tmp_path / "stable-config"
    cwd_a = tmp_path / "cwd-a"
    cwd_b = tmp_path / "cwd-b"
    cwd_a.mkdir()
    cwd_b.mkdir()
    _write_plugin(
        plugin_root,
        "path_fixture",
        code=_tool_plugin_code("unused"),
        manifest_extra=(
            "permissions:\n"
            "  plugin_data:\n"
            "    read: true\n"
            "    write: true\n"
        ),
    )
    monkeypatch.setattr(config_module, "PLUGINS_CONFIG_DIR", config_root)

    monkeypatch.chdir(cwd_a)
    manager = PluginManager()
    manager.discover([plugin_root])
    record = manager.get_plugin("path_fixture")
    assert record is not None
    context = context_module.PluginContext(
        record.manifest,
        manager_module.PermissionChecker("path_fixture", record.manifest.permissions),
    )
    context.plugin_data_set("cwd", "independent")
    assert manager._verify_integrity("path_fixture", record.plugin_dir) is None
    manager.set_plugin_config("path_fixture", {"cwd": "independent"})

    monkeypatch.chdir(cwd_b)
    assert manager.get_plugin_config("path_fixture") == {"cwd": "independent"}
    assert context.plugin_data_get("cwd") == "independent"
    assert manager._load_trust_store()["path_fixture"]
    assert context._data_dir == config_root / "path_fixture" / "data"
    assert not (cwd_a / "config").exists()
    assert not (cwd_b / "config").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("constraint_field", ["xiaoda_bot_version", "sdk_version"])
async def test_direct_load_rejects_incompatible_versions(
    tmp_path,
    monkeypatch,
    constraint_field,
):
    monkeypatch.setattr(manager_module, "HOST_VERSION", "1.2.3")
    monkeypatch.setattr(manager_module, "PLUGIN_SDK_VERSION", "1.2.3")
    _write_plugin(
        tmp_path,
        "version_fixture",
        code=_tool_plugin_code("unused"),
        manifest_extra=f"{constraint_field}: '>=999'\n",
    )
    manager = PluginManager()
    manager.discover([tmp_path])

    assert not await manager.load("version_fixture")
    record = manager.get_plugin("version_fixture")
    assert record.state == PluginState.ERROR
    assert constraint_field in record.error_message


@pytest.mark.asyncio
async def test_orchestration_reports_missing_and_cyclic_dependencies(tmp_path):
    _write_plugin(
        tmp_path,
        "missing_dep",
        code=_tool_plugin_code("unused"),
        manifest_extra="depends_on:\n  - id: absent\n",
    )
    _write_plugin(
        tmp_path,
        "cycle_a",
        code=_tool_plugin_code("unused"),
        manifest_extra="depends_on:\n  - id: cycle_b\n",
    )
    _write_plugin(
        tmp_path,
        "cycle_b",
        code=_tool_plugin_code("unused"),
        manifest_extra="depends_on:\n  - id: cycle_a\n",
    )
    _write_plugin(
        tmp_path,
        "cycle_descendant",
        code=_tool_plugin_code("unused"),
        manifest_extra="depends_on:\n  - id: cycle_a\n",
    )
    manager = PluginManager()
    manager.discover([tmp_path])

    statuses = await manager.load_and_enable_all()

    assert statuses == {
        "missing_dep": False,
        "cycle_a": False,
        "cycle_b": False,
        "cycle_descendant": False,
    }
    assert "absent" in manager.get_plugin("missing_dep").error_message
    assert "cycle" in manager.get_plugin("cycle_a").error_message.lower()
    assert "cycle" in manager.get_plugin("cycle_b").error_message.lower()
    descendant_error = manager.get_plugin("cycle_descendant").error_message.lower()
    assert "dependency cycle detected" not in descendant_error
    assert "dependency unavailable" in descendant_error


@pytest.mark.asyncio
async def test_orchestration_respects_dependencies_and_load_phase(tmp_path):
    order: list[str] = []
    specs: list[tuple[str, str]] = [
        ("a_post", "load_phase: post-agent\n"),
        ("b_pre", "load_phase: pre-agent\n"),
        ("c_post_dep", "load_phase: post-agent\ndepends_on:\n  - id: a_post\n"),
    ]
    for plugin_id, extra in specs:
        _write_plugin(
            tmp_path,
            plugin_id,
            manifest_extra=extra,
            code=f'''from plugins.sdk import Plugin

class FixturePlugin(Plugin):
    async def on_enable(self):
        self.ctx._agent_core.append({plugin_id!r})
''',
        )
    manager = PluginManager(agent_core=order)
    manager.discover([tmp_path])

    statuses = await manager.load_and_enable_all()

    assert all(statuses.values())
    assert order == ["b_pre", "a_post", "c_post_dep"]


@pytest.mark.asyncio
async def test_orchestration_rejects_dependency_version_mismatch(tmp_path):
    _write_plugin(tmp_path, "dependency", code=_tool_plugin_code("unused"))
    _write_plugin(
        tmp_path,
        "consumer",
        code=_tool_plugin_code("unused"),
        manifest_extra="depends_on:\n  - id: dependency\n    version: '>=2'\n",
    )
    manager = PluginManager()
    manager.discover([tmp_path])

    statuses = await manager.load_and_enable_all()

    assert statuses["dependency"] is True
    assert statuses["consumer"] is False
    assert "dependency" in manager.get_plugin("consumer").error_message
    assert ">=2" in manager.get_plugin("consumer").error_message


@pytest.mark.asyncio
async def test_orchestration_fails_closed_for_invalid_specifier(tmp_path):
    _write_plugin(
        tmp_path,
        "invalid_specifier",
        code=_tool_plugin_code("unused"),
        manifest_extra="sdk_version: 'not a valid specifier'\n",
    )
    manager = PluginManager()
    manager.discover([tmp_path])

    statuses = await manager.load_and_enable_all()

    record = manager.get_plugin("invalid_specifier")
    assert statuses == {"invalid_specifier": False}
    assert record.state == PluginState.ERROR
    assert "invalid sdk_version constraint" in record.error_message.lower()


@pytest.mark.asyncio
async def test_dependency_enable_failure_marks_dependent_error(tmp_path):
    _write_plugin(
        tmp_path,
        "failing_dependency",
        code=_tool_plugin_code("unused", fail_enable=True),
    )
    _write_plugin(
        tmp_path,
        "blocked_consumer",
        code=_tool_plugin_code("unused"),
        manifest_extra="depends_on:\n  - id: failing_dependency\n",
    )
    manager = PluginManager()
    manager.discover([tmp_path])

    statuses = await manager.load_and_enable_all()

    assert statuses == {"failing_dependency": False, "blocked_consumer": False}
    assert manager.get_plugin("failing_dependency").state == PluginState.ERROR
    consumer = manager.get_plugin("blocked_consumer")
    assert consumer.state == PluginState.ERROR
    assert "failing_dependency" in consumer.error_message
    assert "not enabled" in consumer.error_message


@pytest.mark.asyncio
async def test_bootstrap_auto_enable_calls_manager_orchestration_once(monkeypatch):
    class RecordingManager:
        def __init__(self):
            self.calls = 0

        async def load_and_enable_all(self):
            self.calls += 1
            return {}

    manager = RecordingManager()
    monkeypatch.setattr(manager_module, "get_active_plugin_manager", lambda: manager)
    monkeypatch.setattr(
        "tool_engine.tool_registry.invalidate_schema_cache",
        lambda: None,
    )
    monkeypatch.setattr("tool_engine.tool_registry.to_openai_tools", lambda: [])
    core = types.SimpleNamespace(
        tool_repair=types.SimpleNamespace(_allowed_tools={"stale"}),
    )

    await AgentCoreBootstrapper(core)._auto_enable_plugins()

    assert manager.calls == 1
    assert core.tool_repair._allowed_tools == set()
