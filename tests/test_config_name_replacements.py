"""config.py 名称替换/还原与显示名回退 helper 的回归测试。

Phase 3（config_agents 拆分）后：apply/reverse/_best_display_name 的内部查找
（agent_names / get_agent_display_name / get_agent_deprecated_names /
get_all_deprecated_names）在 config_agents 模块全局作用域解析，因此
monkeypatch 目标由 config 改为 config_agents；config 上的调用点保持不变
（同名 re-export 是同一对象，见 tests/test_config_agents_module.py）。
"""
from __future__ import annotations

import config
import config_agents


def test_best_display_name_returns_display_name(monkeypatch):
    monkeypatch.setattr(config_agents, "get_agent_display_name", lambda k: "纳西妲")
    assert config._best_display_name("xiaoda") == "纳西妲"


def test_best_display_name_falls_back_to_key(monkeypatch):
    monkeypatch.setattr(config_agents, "get_agent_display_name", lambda k: "")
    assert config._best_display_name("xiaoda") == "xiaoda"


def test_apply_agent_name_replacements(monkeypatch):
    monkeypatch.setattr(config_agents, "get_all_deprecated_names", lambda: {"纳西妲": "xiaoda"})
    monkeypatch.setattr(config_agents, "agent_names", lambda: ["xiaoda"])
    monkeypatch.setattr(config_agents, "get_agent_display_name", lambda k: "草神")
    assert config.apply_agent_name_replacements("纳西妲 xiaoda") == "草神 草神"


def test_reverse_agent_name_replacements(monkeypatch):
    monkeypatch.setattr(config_agents, "agent_names", lambda: ["xiaoda"])
    monkeypatch.setattr(config_agents, "get_agent_display_name", lambda k: "草神")
    monkeypatch.setattr(config_agents, "get_agent_deprecated_names", lambda k: ["纳西妲"])
    # reverse 只做 display_name → agent key，绝不还原到旧名（如"纳西妲"）。
    assert config.reverse_agent_name_replacements("草神") == "xiaoda"


def test_reverse_does_not_restore_deprecated_name(monkeypatch):
    """reverse 不得把显示名还原为旧名（纳西妲），只还原为 agent key。"""
    monkeypatch.setattr(config_agents, "agent_names", lambda: ["xiaoda"])
    monkeypatch.setattr(config_agents, "get_agent_display_name", lambda k: "小妲")
    monkeypatch.setattr(config_agents, "get_agent_deprecated_names", lambda k: ["纳西妲", "nahida"])
    assert config.reverse_agent_name_replacements("小妲") == "xiaoda"
    assert "纳西妲" not in config.reverse_agent_name_replacements("小妲")
