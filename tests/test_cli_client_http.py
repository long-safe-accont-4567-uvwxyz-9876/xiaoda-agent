import sys
from types import SimpleNamespace

import cli_client


def test_main_process_cmd_source_mode():
    # 非打包：用当前 python 解释器 + 同目录 agent.py --web
    cmd = cli_client._main_process_cmd(8090)
    assert cmd[-1] == "8090"
    assert "--web" in cmd
    assert "--port" in cmd
    assert cmd[0] == sys.executable
    assert cmd[1].endswith("agent.py")


def test_main_process_cmd_frozen_mode(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "C:/Xiaoda/xiaoda-agent.exe")
    cmd = cli_client._main_process_cmd(8082)
    assert cmd[0] == "C:/Xiaoda/xiaoda-agent.exe"
    assert "--web" in cmd
    assert "agent.py" not in cmd


def test_ensure_main_process_already_alive(monkeypatch):
    monkeypatch.setattr(cli_client, "main_process_alive", lambda *a, **k: True)
    launched = []
    monkeypatch.setattr(cli_client, "_launch_detached", lambda *a, **k: launched.append(1) or True)
    assert cli_client.ensure_main_process() is True
    assert launched == []


def test_ensure_main_process_fallback_detached(monkeypatch):
    # systemd 不可用（如 Windows/Docker），回退到直接后台拉起主进程
    monkeypatch.setattr(cli_client, "main_process_alive", lambda *a, **k: False)
    monkeypatch.setattr(cli_client, "_try_systemd_start", lambda *a, **k: False)
    monkeypatch.setattr(cli_client, "_launch_detached", lambda *a, **k: True)
    monkeypatch.setattr(cli_client, "_wait_main_process_alive", lambda *a, **k: True)
    assert cli_client.ensure_main_process() is True


def test_ensure_main_process_detached_failure(monkeypatch):
    monkeypatch.setattr(cli_client, "main_process_alive", lambda *a, **k: False)
    monkeypatch.setattr(cli_client, "_try_systemd_start", lambda *a, **k: False)
    monkeypatch.setattr(cli_client, "_launch_detached", lambda *a, **k: False)
    assert cli_client.ensure_main_process() is False


def test_launch_detached_missing_binary_returns_false():
    # 不存在的可执行文件：Popen 抛 OSError → 返回 False（不崩溃）
    assert cli_client._launch_detached(["/nonexistent/definitely-not-a-real-bin"]) is False


def test_resolve_port_reads_installed_systemd_service(monkeypatch):
    calls = []
    monkeypatch.delenv("WEBUI_PORT", raising=False)
    monkeypatch.setattr(cli_client, "_RESOLVED_PORT", None)

    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(stdout="ExecStart=/opt/xiaoda-agent --port 8091")

    monkeypatch.setattr(cli_client.subprocess, "run", run)
    assert cli_client._resolve_port() == 8091
    assert calls == [["systemctl", "cat", "xiaoda-agent"]]


def test_systemd_start_uses_installed_service_and_reports_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)

    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=5, stderr=b"unit not found")

    monkeypatch.setattr(cli_client.subprocess, "run", run)
    assert cli_client._try_systemd_start() is False
    assert calls == [["systemctl", "start", "xiaoda-agent"]]


def test_discover_models_parses_provider_list(monkeypatch):
    payload = {"data": [
        {"provider": "siliconflow", "label": "硅基流动",
         "models": [{"id": "Qwen2.5", "display_name": "Qwen2.5", "free": True}]},
        {"provider": "mimo", "models": [
            {"id": "mimo-v2.5", "display_name": "MiMo v2.5", "free": True}]},
    ]}
    monkeypatch.setattr(cli_client, "_http_get_json", lambda *a, **k: payload)
    providers = cli_client.discover_models("tok")
    assert providers == payload["data"]


def test_discover_models_returns_empty_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(cli_client, "_http_get_json", boom)
    assert cli_client.discover_models("tok") == []


def test_list_agents_parses_list(monkeypatch):
    payload = {"data": [{"name": "xiaoda", "display_name": "小妲"}]}
    monkeypatch.setattr(cli_client, "_http_get_json", lambda *a, **k: payload)
    assert cli_client.list_agents("tok") == [{"name": "xiaoda", "display_name": "小妲"}]


def test_list_agents_returns_empty_on_non_list(monkeypatch):
    monkeypatch.setattr(cli_client, "_http_get_json", lambda *a, **k: {"data": {}})
    assert cli_client.list_agents("tok") == []
