from cli_app import fixed_arg_items, model_providers, model_items, agent_items


def test_fixed_arg_items_from_meta():
    assert fixed_arg_items("/voice") == ["on", "off"]


def test_model_providers_flatten():
    providers = [
        {"provider": "openai", "label": "OpenAI", "models": [{"id": "gpt-4o"}]},
        {"provider": "", "id": "mimo", "models": [{"id": "mimo-v2.5"}]},
        {"models": []},
    ]
    got = model_providers(providers)
    assert ("openai", "OpenAI") in got
    assert ("mimo", "mimo") in got
    assert len(got) == 2


def test_model_items_use_display_name():
    models = [{"id": "gpt-4o", "display_name": "GPT-4o"}, {"id": "x"}]
    assert model_items(models) == [("gpt-4o", "GPT-4o"), ("x", "x")]


def test_agent_items():
    agents = [{"name": "xiaoli", "display_name": "小莉"}, {"name": "x"}]
    assert agent_items(agents) == [("xiaoli", "小莉"), ("x", "x")]