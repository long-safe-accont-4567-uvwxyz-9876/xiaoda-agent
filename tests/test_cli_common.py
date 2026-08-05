from cli_common import (
    STYLE,
    STATUS_MAP,
    AGENT_NAMES,
    status_translate,
    get_model_info,
    command_entries,
    address_term,
    cli_should_use_tui,
)


def test_style_has_key_colors():
    assert "leaf" in STYLE and "border" in STYLE and "assistant" in STYLE


def test_status_translate_maps_known():
    # 丰富版：thinking 映射为非空且含"小妲"（ACK 消息或默认文案）
    out = status_translate("thinking")
    assert out and "小妲" in out


def test_status_translate_falls_back():
    # 丰富版：未知消息返回 f"🌿 {msg}"，会包裹原串
    assert "zzz_unknown" in status_translate("zzz_unknown")


def test_get_model_info_fallback():
    # 无主进程时返回默认模型名
    assert get_model_info() == "mimo-v2.5"


def test_command_entries_include_help():
    public, owner = command_entries()
    assert any(n == "/help" for n, _ in public) or any(n == "/help" for n, _ in owner)


def test_address_term_fallback():
    assert address_term() in ("朋友", "爸爸")


def test_cli_should_use_tui_returns_bool():
    assert isinstance(cli_should_use_tui(), bool)