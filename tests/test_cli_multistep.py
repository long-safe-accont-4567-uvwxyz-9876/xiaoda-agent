import cli
import cli_client


def _make_cli():
    obj = cli.CLIInterface.__new__(cli.CLIInterface)
    obj._token = "tok"
    return obj


def test_menu_model_builds_provider_model_command(monkeypatch):
    providers = [
        {"provider": "siliconflow", "label": "硅基",
         "models": [{"id": "Qwen2.5", "display_name": "Qwen2.5", "free": True}]},
        {"provider": "mimo", "models": [
            {"id": "mimo-v2.5", "display_name": "MiMo v2.5", "free": True}]},
    ]
    monkeypatch.setattr(cli_client, "discover_models", lambda *a, **k: providers)

    # 第一层选 provider（siliconflow），第二层选模型
    calls = {"n": 0}
    def fake_menu(title, items, hint=""):
        picks = [it.value for it in items]
        calls["n"] += 1
        if calls["n"] == 1:
            assert "选择模型提供方" in title
            return "siliconflow"
        assert picks[0] == "siliconflow/Qwen2.5"
        return "siliconflow/Qwen2.5"
    monkeypatch.setattr(cli, "select_from_menu", fake_menu)

    obj = _make_cli()
    assert obj._menu_model() == "/model siliconflow/Qwen2.5"
    assert calls["n"] == 2


def test_menu_model_returns_none_when_fetch_empty(monkeypatch):
    monkeypatch.setattr(cli_client, "discover_models", lambda *a, **k: [])
    obj = _make_cli()
    assert obj._menu_model() is None


def test_menu_agent_builds_command(monkeypatch):
    agents = [{"name": "xiaoda", "display_name": "小妲"},
              {"name": "xiaoli", "display_name": "小莉"}]
    monkeypatch.setattr(cli_client, "list_agents", lambda *a, **k: agents)
    monkeypatch.setattr(cli, "select_from_menu", lambda t, items, hint="": items[1].value)
    obj = _make_cli()
    assert obj._menu_agent() == "/agent xiaoli"


def test_try_expand_skips_when_arg_present(monkeypatch):
    obj = _make_cli()
    monkeypatch.setattr(cli, "select_from_menu", lambda *a, **k: None)
    assert obj._try_expand_multistep("/model", "siliconflow/Qwen2.5") is None