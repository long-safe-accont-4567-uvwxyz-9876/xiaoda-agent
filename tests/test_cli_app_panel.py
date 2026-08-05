import pytest

from cli_app import XiaodaApp, SlashPanel, build_command_groups


@pytest.mark.asyncio
async def test_slash_panel_filters_by_query():
    app = XiaodaApp()
    async with app.run_test() as pilot:
        panel = SlashPanel(on_select=lambda cmd, chat: None)
        await app.push_screen(panel)
        await pilot.pause()
        # 过滤 /model：应只剩模型分组
        panel.set_filter("/model")
        await pilot.pause()
        assert panel.visible_count() >= 1


def test_every_command_has_group():
    from slash_commands import COMMAND_DESCRIPTIONS

    all_names = {it["name"] for g in build_command_groups() for it in g["items"]}
    for name in COMMAND_DESCRIPTIONS:
        assert name in all_names, f"{name} 缺少分组归属"