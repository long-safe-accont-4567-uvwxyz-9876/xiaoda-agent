from cli_common import cli_should_use_tui


def test_cli_should_use_tui_rejects_dumb_term(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    assert cli_should_use_tui() is False


def test_cli_should_use_tui_ok_import(monkeypatch):
    # 强制 TERM 非 dumb；isatty 由环境决定，这里只验证不因导入异常崩溃
    monkeypatch.setenv("TERM", "xterm-256color")
    assert isinstance(cli_should_use_tui(), bool)