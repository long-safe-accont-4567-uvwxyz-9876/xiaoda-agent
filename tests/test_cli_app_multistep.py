import pytest
from textual.widgets import ListView

from cli_app import _MultiStepPanel, fixed_arg_items, model_providers, model_items, agent_items


@pytest.mark.asyncio
async def test_multistep_panel_dismisses_on_select():
    """选中二级面板项即回调并关闭自身，不残留叠层。"""
    from cli_app import XiaodaApp

    app = XiaodaApp()
    selected = []
    async with app.run_test() as pilot:
        panel = _MultiStepPanel(
            "测试", [("value-1", "值1"), ("value-2", "值2")],
            lambda v: selected.append(v),
        )
        await app.push_screen(panel)
        await pilot.pause()
        assert app.screen is panel
        panel.query_one("#multistep-list", ListView).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert selected == ["value-1"]
        assert panel not in app.screen_stack
        assert app.screen is not panel


@pytest.mark.asyncio
async def test_model_two_level_no_stacking(monkeypatch):
    """/model provider 面板选中后关闭原生面板，再 push 模型面板，不叠两层。"""
    import cli_client
    from cli_app import XiaodaApp

    providers = [{
        "provider": "openai", "label": "OpenAI",
        "models": [{"id": "gpt-4o", "display_name": "GPT-4o"}],
    }]
    monkeypatch.setattr(cli_client, "discover_models", lambda token: providers)
    app = XiaodaApp()
    app._ws = object()  # 已连接状态，供多步面板正常弹出（连接守卫）
    async with app.run_test() as pilot:
        await app._open_multistep("/model", app.query_one("#chat"))
        await pilot.pause()
        provider_panel = app.screen
        assert isinstance(provider_panel, _MultiStepPanel)
        provider_panel.query_one("#multistep-list", ListView).focus()
        await pilot.press("enter")
        await pilot.pause()
        # provider 面板已关闭，当前为模型面板，且只叠了一层
        assert provider_panel not in app.screen_stack
        assert isinstance(app.screen, _MultiStepPanel)
        assert app.screen is not provider_panel


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