"""config.py 名称替换/还原与显示名回退 helper 的回归测试。"""
from __future__ import annotations

import config


def test_best_display_name_returns_display_name(monkeypatch):
    monkeypatch.setattr(config, "get_agent_display_name", lambda k: "纳西妲")
    assert config._best_display_name("xiaoda") == "纳西妲"


def test_best_display_name_falls_back_to_key(monkeypatch):
    monkeypatch.setattr(config, "get_agent_display_name", lambda k: "")
    assert config._best_display_name("xiaoda") == "xiaoda"


def test_apply_agent_name_replacements(monkeypatch):
    monkeypatch.setattr(config, "get_all_deprecated_names", lambda: {"纳西妲": "xiaoda"})
    monkeypatch.setattr(config, "agent_names", lambda: ["xiaoda"])
    monkeypatch.setattr(config, "get_agent_display_name", lambda k: "草神")
    assert config.apply_agent_name_replacements("纳西妲 xiaoda") == "草神 草神"


def test_reverse_agent_name_replacements(monkeypatch):
    monkeypatch.setattr(config, "agent_names", lambda: ["xiaoda"])
    monkeypatch.setattr(config, "get_agent_display_name", lambda k: "草神")
    monkeypatch.setattr(config, "get_agent_deprecated_names", lambda k: ["纳西妲"])
    # 注意：reverse 先做 display_name → agent_key（"草神" → "xiaoda"），
    # 再做 display_name → 中文旧名（"草神" → "纳西妲"），但此时内容已是 "xiaoda"，
    # 第二步不再命中，因此最终结果是 "xiaoda"。这里按既有行为断言，防止回归。
    assert config.reverse_agent_name_replacements("草神") == "xiaoda"
