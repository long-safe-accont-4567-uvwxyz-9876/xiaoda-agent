"""默认模型切 DeepSeek + 引导页二选一（前后端一致）的回归测试。

对应 2026-11-08 用户要求：默认模型从 MiMo 换成 DeepSeek；首次引导页
改成 DeepSeek / MiMo 二选一（默认预选 DeepSeek）；且不只改前端——
后端默认值、catalog 内置标记、首跑判断与落盘都要真的生效。
"""
from __future__ import annotations

import json
from pathlib import Path

from web.routers import setup_default_provider as sdp

REPO = Path(__file__).resolve().parents[1]


def _meta() -> dict:
    return json.loads((REPO / "config" / "provider_metadata.json").read_text(encoding="utf-8"))


def test_repo_default_provider_is_deepseek():
    assert _meta()["default_provider"] == "deepseek"


def test_deepseek_catalog_entry_complete():
    ds = _meta()["providers"]["deepseek"]
    assert ds["builtin"] is True
    assert ds["protocol"] == "openai_compatible"
    assert ds["base_url_default"] == "https://api.deepseek.com/v1"
    assert ds["chat_path"] == "/chat/completions"
    assert ds["models_path"] == "/models"
    assert ds["default_model"] == "deepseek-chat"


def test_mimo_still_builtin_and_available():
    mimo = _meta()["providers"]["mimo"]
    assert mimo["builtin"] is True
    assert mimo["base_url_default"] == "https://api.xiaomimimo.com/v1"


def test_choices_are_two_with_deepseek_first_and_default():
    choices = sdp.describe_provider_choices()
    assert [c["id"] for c in choices] == ["deepseek", "mimo"]
    assert choices[0]["default"] is True
    assert choices[1]["default"] is False
    assert choices[0]["key_env"] == "DEEPSEEK_API_KEY"
    assert choices[1]["key_env"] == "MIMO_API_KEY"


def test_normalize_rejects_unknown_provider():
    assert sdp.normalize_provider_choice("") == ""
    assert sdp.normalize_provider_choice(" DeepSeek ") == "deepseek"
    try:
        sdp.normalize_provider_choice("openai")
    except ValueError as exc:
        assert "openai" in str(exc)
    else:
        raise AssertionError("should reject unsupported provider")


def test_deepseek_without_key_is_rejected(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    reason = sdp.check_choice("deepseek", {})
    assert "DEEPSEEK_API_KEY" in reason
    env = sdp.validate_default_provider_choice({"default_provider": "deepseek"}, {})
    assert env is not None and env.ok is False
    assert getattr(env.error, "code", "") == "MISSING_PROVIDER_KEY"


def test_deepseek_with_key_in_payload_passes(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    updates = {"DEEPSEEK_API_KEY": "sk-test"}
    assert sdp.check_choice("deepseek", updates) == ""
    assert sdp.validate_default_provider_choice({"default_provider": "deepseek"}, updates) is None


def test_mimo_choice_still_supported(monkeypatch):
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    assert "MIMO_API_KEY" in sdp.check_choice("mimo", {})
    assert sdp.check_choice("mimo", {"MIMO_API_KEY": "k"}) == ""


def test_persist_writes_user_metadata(monkeypatch, tmp_path):
    target = tmp_path / "provider_metadata.json"
    monkeypatch.setattr(sdp, "user_metadata_path", lambda: target)
    sdp.persist_default_provider("mimo")
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["default_provider"] == "mimo"
    assert data["providers"]["deepseek"]["base_url_default"] == "https://api.deepseek.com/v1"


def test_apply_choice_persists_and_marks_runtime(monkeypatch, tmp_path):
    target = tmp_path / "provider_metadata.json"
    monkeypatch.setattr(sdp, "user_metadata_path", lambda: target)
    applied = []
    monkeypatch.setattr(sdp, "apply_runtime_default_provider", lambda pid: applied.append(pid) or "deepseek-chat")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    out = sdp.apply_default_provider_choice(
        {"default_provider": "deepseek"}, {"DEEPSEEK_API_KEY": "sk-test"}
    )
    assert out == "deepseek"
    assert applied == ["deepseek"]
    assert json.loads(target.read_text(encoding="utf-8"))["default_provider"] == "deepseek"


def test_apply_choice_skips_when_key_missing(monkeypatch, tmp_path):
    target = tmp_path / "provider_metadata.json"
    monkeypatch.setattr(sdp, "user_metadata_path", lambda: target)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert sdp.apply_default_provider_choice({"default_provider": "deepseek"}, {}) == ""
    assert not target.exists()


def test_wizard_required_keys_no_longer_force_mimo():
    import setup_wizard as sw

    required = [item["key"] for item in sw.REQUIRED_KEYS]
    optional = [item["key"] for item in sw.OPTIONAL_KEYS]
    assert "MIMO_API_KEY" not in required
    assert "DEEPSEEK_API_KEY" not in required
    assert "MIMO_API_KEY" in optional
    assert "DEEPSEEK_API_KEY" in optional


def test_first_run_accepts_either_llm_key(monkeypatch, tmp_path):
    import setup_wizard as sw

    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(sw, "ENV_PATH", str(env_file))
    base = {
        "QQBOT_APP_ID": "1",
        "QQBOT_APP_SECRET": "2",
        "SILICONFLOW_API_KEY": "3",
    }
    monkeypatch.setattr(sw, "_load_env_values", lambda: dict(base))
    assert sw.is_first_run() is True
    monkeypatch.setattr(sw, "_load_env_values", lambda: {**base, "DEEPSEEK_API_KEY": "sk"})
    assert sw.is_first_run() is False
    monkeypatch.setattr(sw, "_load_env_values", lambda: {**base, "MIMO_API_KEY": "k"})
    assert sw.is_first_run() is False


def test_setup_router_wires_default_provider():
    src = (REPO / "web" / "routers" / "setup.py").read_text(encoding="utf-8")
    assert "_default_provider.validate_default_provider_choice(body, updates)" in src
    assert "_default_provider.apply_default_provider_choice(body, updates)" in src
    assert '"provider_choices": _default_provider.describe_provider_choices()' in src
    assert '"default_provider": _default_provider.get_current_default_provider()' in src


def test_frontend_wizard_offers_both_providers():
    src = (REPO / "web" / "frontend" / "src" / "views" / "SetupWizardView.vue").read_text(encoding="utf-8")
    assert "default_provider" in src
    assert "deepseek" in src
    assert "mimo" in src
