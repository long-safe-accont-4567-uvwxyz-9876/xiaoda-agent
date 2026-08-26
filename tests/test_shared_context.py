"""多平台共用上下文：跨平台共享上下文的 user_id 重映射逻辑测试。

功能：系统设置中可选择哪些平台（web/cli/qq/wechat）共用上下文。
被选中的平台在恢复/写入历史时统一映射到共享上下文键，从而跨平台读同一份历史。
默认关闭（shared_platforms 为空）→ 各平台保持独立会话。
QQ 群聊非主人对话始终独立，不纳入共享。
"""
from __future__ import annotations

import json

from agent_core.core import AgentCore
from web.config_service import ConfigService


def _make_core() -> AgentCore:
    """构造裸 AgentCore（跳过重的 __init__），仅测试共享上下文解析。"""
    return AgentCore.__new__(AgentCore)


def _make_cfg(tmp_path, shared_platforms, shared_key="shared") -> ConfigService:
    overrides = tmp_path / "webui_overrides.json"
    overrides.write_text(json.dumps({
        "context": {"shared_platforms": shared_platforms, "shared_key": shared_key},
    }), encoding="utf-8")
    return ConfigService(path=overrides)


def _resolve(core: AgentCore, user_id: str, source: str, is_master: bool,
             cfg: ConfigService, monkeypatch) -> str:
    import web.config_service as cs
    monkeypatch.setattr(cs, "get_config_service", lambda: cfg)
    return core._resolve_shared_context_id(user_id, source, is_master)


# ── 默认关闭：各平台独立 ──────────────────────────────────────
def test_default_off_keeps_independent_user_ids(tmp_path, monkeypatch):
    """shared_platforms 为空（默认）→ 各平台 user_id 保持不变。"""
    core = _make_core()
    cfg = _make_cfg(tmp_path, [])
    assert _resolve(core, "webui", "web", True, cfg, monkeypatch) == "webui"
    assert _resolve(core, "cli_owner", "cli", True, cfg, monkeypatch) == "cli_owner"
    assert _resolve(core, "qq_ABC", "qq_c2c", True, cfg, monkeypatch) == "qq_ABC"
    assert _resolve(core, "wechat_XYZ", "wechat_c2c", True, cfg, monkeypatch) == "wechat_XYZ"


# ── 选中平台共享为统一键 ──────────────────────────────────────
def test_selected_platforms_map_to_shared_key(tmp_path, monkeypatch):
    """web 与 cli 被选中 → 两者统一映射到同一个共享上下文键，qq 保持独立。"""
    core = _make_core()
    cfg = _make_cfg(tmp_path, ["web", "cli"])

    web_id = _resolve(core, "webui", "web", True, cfg, monkeypatch)
    cli_id = _resolve(core, "cli_owner", "cli", True, cfg, monkeypatch)
    qq_id = _resolve(core, "qq_ABC", "qq_c2c", True, cfg, monkeypatch)

    assert web_id == "shared_context:shared"
    assert cli_id == "shared_context:shared"
    assert web_id == cli_id, "被选中的 web/cli 必须共用同一上下文键"
    assert qq_id == "qq_ABC", "未选中的 qq 应保持独立会话"


def test_custom_shared_key_used(tmp_path, monkeypatch):
    """自定义 shared_key 时，共享键使用该值。"""
    core = _make_core()
    cfg = _make_cfg(tmp_path, ["qq", "wechat"], shared_key="family")
    qq_id = _resolve(core, "qq_ABC", "qq_c2c", True, cfg, monkeypatch)
    wechat_id = _resolve(core, "wechat_XYZ", "wechat_c2c", True, cfg, monkeypatch)
    assert qq_id == "shared_context:family"
    assert wechat_id == "shared_context:family"


# ── 平台来源归一化：qq_c2c / qq_group / wechat_c2c 取前缀 ─────
def test_source_prefix_normalization(tmp_path, monkeypatch):
    """qq_c2c 与 qq_group 都归为 qq 平台，wechat_c2c 归为 wechat。"""
    core = _make_core()
    cfg = _make_cfg(tmp_path, ["qq", "wechat"])
    assert _resolve(core, "qq_ABC", "qq_c2c", True, cfg, monkeypatch) == "shared_context:shared"
    assert _resolve(core, "qq_ABC", "qq_group", True, cfg, monkeypatch) == "shared_context:shared"
    assert _resolve(core, "wechat_XYZ", "wechat_c2c", True, cfg, monkeypatch) == "shared_context:shared"


# ── QQ 群聊非主人：不纳入共享 ─────────────────────────────────
def test_qq_group_non_owner_excluded(tmp_path, monkeypatch):
    """QQ 群聊非主人（is_master=False）即使 qq 被选中也保持独立，不共享上下文。"""
    core = _make_core()
    cfg = _make_cfg(tmp_path, ["qq", "web"])

    # 群聊非主人 → 独立
    assert _resolve(core, "qq_friend", "qq_group", False, cfg, monkeypatch) == "qq_friend"
    # 群聊主人 → 共享
    assert _resolve(core, "qq_owner", "qq_group", True, cfg, monkeypatch) == "shared_context:shared"
    # 单聊（qq_c2c）无论 is_master → 共享
    assert _resolve(core, "qq_owner", "qq_c2c", True, cfg, monkeypatch) == "shared_context:shared"


# ── 配置读取异常时安全降级 ────────────────────────────────────
def test_config_error_falls_back_to_original(tmp_path, monkeypatch):
    """get_config_service 抛异常时不阻断，保持原 user_id 独立。"""
    core = _make_core()
    import web.config_service as cs

    def _boom():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(cs, "get_config_service", _boom)
    assert core._resolve_shared_context_id("cli_owner", "cli", True) == "cli_owner"
