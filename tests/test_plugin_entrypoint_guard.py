"""插件 entrypoint 任意模块导入守卫测试。

覆盖:
- validate_entrypoint 拒绝标准库模块 entrypoint (os:system / subprocess:Popen / pathlib:Path)
- validate_entrypoint 接受插件目录内的相对模块 entrypoint (echo_plugin:EchoPlugin)
- PluginManager._resolve_entrypoint_module 拒绝任意模块名，接受插件目录内相对模块
- 真实加载: echo 插件可加载，危险 entrypoint 插件被拒绝
"""

import pytest
from pydantic import ValidationError

from plugins.manager import PluginManager, PluginState
from plugins.manifest import PluginManifest

DANGEROUS_ENTRYPOINTS = [
    "os:system",
    "subprocess:Popen",
    "pathlib:Path",
    "sys:exit",
    "importlib:import_module",
]


class TestValidateEntrypoint:
    @pytest.mark.parametrize("entrypoint", DANGEROUS_ENTRYPOINTS)
    def test_rejects_stdlib_module_entrypoints(self, entrypoint):
        with pytest.raises(ValidationError):
            PluginManifest(id="evil", name="evil", entrypoint=entrypoint)

    @pytest.mark.parametrize(
        "entrypoint",
        ["echo_plugin:EchoPlugin", "plugins.echo.echo_plugin:EchoPlugin"],
    )
    def test_accepts_plugin_relative_entrypoints(self, entrypoint):
        manifest = PluginManifest(id="echo", name="echo", entrypoint=entrypoint)
        assert manifest.entrypoint == entrypoint


class TestEntrypointModuleGuard:
    @pytest.mark.parametrize("module_path", ["os", "subprocess", "pathlib", "sys", "importlib"])
    def test_rejects_stdlib_modules(self, module_path, tmp_path):
        with pytest.raises(ValueError):
            PluginManager._resolve_entrypoint_module(module_path, tmp_path)

    def test_rejects_module_outside_plugin_dir(self, tmp_path):
        with pytest.raises(ValueError):
            PluginManager._resolve_entrypoint_module("requests", tmp_path)

    def test_accepts_module_inside_plugin_dir(self, tmp_path):
        plugin_dir = tmp_path / "echo"
        plugin_dir.mkdir()
        (plugin_dir / "echo_plugin.py").write_text("class EchoPlugin: pass\n", encoding="utf-8")
        assert PluginManager._resolve_entrypoint_module("echo_plugin", plugin_dir) == "echo_plugin"

    @pytest.mark.parametrize("module_path", ["os.path", ".hidden", "a..b", "os/../x"])
    def test_rejects_invalid_module_names(self, module_path, tmp_path):
        with pytest.raises(ValueError):
            PluginManager._resolve_entrypoint_module(module_path, tmp_path)


class TestRealLoad:
    @pytest.mark.asyncio
    async def test_echo_plugin_loads(self, monkeypatch):
        monkeypatch.setenv("PLUGINS_TRUST_MODE", "off")
        manager = PluginManager()
        manager.discover()
        assert await manager.load("echo") is True
        assert manager.get_plugin("echo").state == PluginState.LOADED

    @pytest.mark.asyncio
    async def test_dangerous_entrypoint_rejected_by_discovery(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PLUGINS_TRUST_MODE", "off")
        plugin_dir = tmp_path / "evil"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.yaml").write_text(
            "id: evil\n"
            "name: Evil\n"
            "version: 0.1.0\n"
            "entrypoint: 'os:system'\n",
            encoding="utf-8",
        )
        manager = PluginManager()
        manager.discover([str(tmp_path)])
        # discovery 会因 manifest 校验失败而跳过该插件
        assert manager.get_plugin("evil") is None
