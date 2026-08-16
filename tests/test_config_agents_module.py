"""config.py Phase 3（agent 命名/display_name 块抽出）结构契约测试。

背景：config.py 的 agent 命名机制（默认显示名回退、display_name 带 mtime
缓存、agent key 目录扫描、deprecated_names 旧名映射、人格文件全局名称
替换/还原）抽为 config_agents.py，逐字节搬移。

契约：
    1. config_agents 独立可导入（仅依赖 config_paths，不 import config，
       无循环导入、不触发 config/web 依赖链）
    2. config 同名 re-export：from config import get_agent_display_name /
       agent_names / apply_agent_name_replacements 等既有用法不受影响（同对象）
    3. 行为契约：默认显示名回退、deprecated_names 兜底映射、
       display_name 缓存/清除、名称替换往返
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── 1/2. 独立导入 + re-export 同对象 ─────────────────────────────

def test_config_agents_imports_standalone():
    import importlib
    mod = importlib.import_module("config_agents")
    for name in ("_DEFAULT_DISPLAY_NAMES", "_display_name_cache",
                 "_FALLBACK_DEPRECATED_NAMES", "_deprecated_names_cache",
                 "clear_display_name_cache", "agent_names",
                 "get_agent_display_name", "_best_display_name",
                 "get_agent_deprecated_names", "get_all_deprecated_names",
                 "apply_agent_name_replacements",
                 "reverse_agent_name_replacements"):
        assert hasattr(mod, name), f"缺少符号 {name}"


def test_config_agents_standalone_import_chain():
    """干净子进程：import config_agents 不连带导入 config 或 web 依赖链。"""
    code = (
        "import sys\n"
        "import config_agents\n"
        "assert 'config' not in sys.modules, 'config 被连带导入'\n"
        "assert not any(m == 'web' or m.startswith('web.') for m in sys.modules), 'web 被连带导入'\n"
        "print('ok')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True,
        cwd=str(Path(__file__).parent.parent),
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


@pytest.mark.parametrize("name", [
    "_DEFAULT_DISPLAY_NAMES", "_display_name_cache",
    "_FALLBACK_DEPRECATED_NAMES", "_deprecated_names_cache",
    "_best_display_name",
    "clear_display_name_cache", "agent_names", "get_agent_display_name",
    "get_agent_deprecated_names", "get_all_deprecated_names",
    "apply_agent_name_replacements", "reverse_agent_name_replacements",
])
def test_config_reexports_same_objects(name):
    import config
    import config_agents
    assert hasattr(config, name), f"config 缺少兼容别名 {name}"
    assert getattr(config, name) is getattr(config_agents, name), name


def test_config_agents_does_not_import_config():
    import config_agents as mod
    assert "config" not in getattr(mod, "__dict__", {})
    assert "config_agents" not in getattr(mod, "__dict__", {})


# ── 3. 行为契约 ─────────────────────────────────────────────────

def test_default_display_name_fallback(tmp_path, monkeypatch):
    """无配置文件时回退 _DEFAULT_DISPLAY_NAMES；未知名回退 key 本身；空名返回空。"""
    import config_agents
    empty_dir = tmp_path / "agents_empty"
    empty_dir.mkdir()
    monkeypatch.setattr(config_agents, "AGENTS_CONFIG_DIR", empty_dir)
    assert config_agents.get_agent_display_name("xiaoda") == \
        config_agents._DEFAULT_DISPLAY_NAMES["xiaoda"] == "小妲"
    assert config_agents.get_agent_display_name("no_such_agent_zzz") == "no_such_agent_zzz"
    assert config_agents.get_agent_display_name("") == ""


def test_agent_names_source_fallback(tmp_path, monkeypatch):
    """外置 agents 目录为空时回退源码 config/agents/（源码资源兜底）。"""
    import config_agents
    empty_dir = tmp_path / "agents_empty2"
    empty_dir.mkdir()
    monkeypatch.setattr(config_agents, "AGENTS_CONFIG_DIR", empty_dir)
    names = config_agents.agent_names()
    assert "xiaoda" in names
    assert "xiaoli" in names


def test_display_name_cache_hit_and_clear(tmp_path, monkeypatch):
    """display_name 带缓存；clear_display_name_cache 单点/全量清除。"""
    import config_agents
    agents_dir = tmp_path / "agents_real"
    agents_dir.mkdir()
    (agents_dir / "t_agent.json").write_text(
        '{"name": "t_agent", "display_name": "测试体"}', encoding="utf-8"
    )
    monkeypatch.setattr(config_agents, "AGENTS_CONFIG_DIR", agents_dir)
    config_agents._display_name_cache.clear()
    assert config_agents.get_agent_display_name("t_agent") == "测试体"
    cached = config_agents._display_name_cache.get("t_agent")
    assert cached is not None and cached[1] == "测试体"
    config_agents.clear_display_name_cache("t_agent")
    assert "t_agent" not in config_agents._display_name_cache
    # 清除后重新读取会再次缓存，随后全量 clear 应清空
    assert config_agents.get_agent_display_name("t_agent") == "测试体"
    config_agents.clear_display_name_cache()
    assert not config_agents._display_name_cache


def test_deprecated_names_fallback_mapping(tmp_path, monkeypatch):
    """无配置文件时回退硬编码兜底映射；未知 agent 返回空列表。"""
    import config_agents
    empty_dir = tmp_path / "agents_empty3"
    empty_dir.mkdir()
    monkeypatch.setattr(config_agents, "AGENTS_CONFIG_DIR", empty_dir)
    assert config_agents.get_agent_deprecated_names("no_such_agent_zzz") == []
    for key in ("xiaoda", "xiaoli", "xiaolang", "xiaolian", "xiaoke"):
        fallback = [k for k, v in config_agents._FALLBACK_DEPRECATED_NAMES.items() if v == key]
        assert fallback, f"{key} 缺少兜底旧名"
        assert config_agents.get_agent_deprecated_names(key) == fallback


def test_deprecated_names_from_json(tmp_path, monkeypatch):
    """deprecated_names 从 agent json 读取（优先于硬编码兜底）。"""
    import config_agents
    agents_dir = tmp_path / "agents_dn"
    agents_dir.mkdir()
    (agents_dir / "t_agent.json").write_text(
        '{"name": "t_agent", "deprecated_names": ["旧名甲", "old-a"]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(config_agents, "AGENTS_CONFIG_DIR", agents_dir)
    assert config_agents.get_agent_deprecated_names("t_agent") == ["旧名甲", "old-a"]
    assert config_agents.get_all_deprecated_names()["旧名甲"] == "t_agent"


def test_name_replacements_roundtrip(monkeypatch):
    """apply 与 reverse 互为逆操作（隔离环境：固定 agent 集/显示名/旧名）。"""
    import config
    import config_agents
    monkeypatch.setattr(config_agents, "agent_names", lambda: ["xiaoda"])
    monkeypatch.setattr(config_agents, "get_agent_display_name", lambda k: "草神")
    monkeypatch.setattr(config_agents, "get_agent_deprecated_names", lambda k: ["纳西妲", "nahida"])
    content = "纳西妲 xiaoda 草神 nahida"
    applied = config.apply_agent_name_replacements(content)
    assert applied == "草神 草神 草神 草神"
    restored = config.reverse_agent_name_replacements(applied)
    assert restored == "xiaoda xiaoda xiaoda xiaoda"
    # 还原后的文本再次 apply 应稳定回到替换态
    assert config.apply_agent_name_replacements(restored) == applied
