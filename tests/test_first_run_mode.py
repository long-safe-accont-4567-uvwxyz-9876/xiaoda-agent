"""首次运行模式选择守护测试。

防止 P0 回归：desktop 模式首次运行启动了 CLI 交互向导 wizard_main()，
但 desktop 模式 stdin 不可交互（watchdog 设为 DEVNULL），input() 永远
EOFError，向导无法接收输入；且 cmd 里"必填项未配置"警告会误导用户
以为"报错卡死，没进首次配置界面"。

用户需求：desktop 首次运行应直接进 WebUI setup 窗口（非 CLI）。
"""
import agent


def test_desktop_first_run_skips_cli_wizard(monkeypatch):
    """desktop 模式首次运行不应启动 CLI 向导（stdin 不可交互）。"""
    wizard_calls = []
    monkeypatch.setattr("setup_wizard.main", lambda: wizard_calls.append(1))

    agent._handle_first_run_mode("desktop")

    assert wizard_calls == [], "desktop 模式不应启动 CLI 向导"


def test_web_first_run_skips_cli_wizard(monkeypatch):
    """web 模式首次运行不应启动 CLI 向导。"""
    wizard_calls = []
    monkeypatch.setattr("setup_wizard.main", lambda: wizard_calls.append(1))

    agent._handle_first_run_mode("web")

    assert wizard_calls == [], "web 模式不应启动 CLI 向导"


def test_cli_first_run_uses_wizard(monkeypatch):
    """CLI 模式（非 web 非 desktop）首次运行启动交互向导。"""
    wizard_calls = []
    monkeypatch.setattr("setup_wizard.main", lambda: wizard_calls.append(1))
    monkeypatch.setattr("agent.load_dotenv", lambda *a, **k: None)

    agent._handle_first_run_mode("cli")

    assert wizard_calls == [1], "CLI 模式应启动交互向导"
