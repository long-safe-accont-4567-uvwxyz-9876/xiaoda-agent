import cli_client


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