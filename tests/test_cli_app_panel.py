import pytest
from textual.widgets import Input

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


@pytest.mark.asyncio
async def test_slash_panel_input_changes_filter_realtime():
    """输入 #panel-search 即触发 set_filter，实时缩小可见列表。"""
    app = XiaodaApp()
    async with app.run_test() as pilot:
        panel = SlashPanel(on_select=lambda cmd, chat: None)
        await app.push_screen(panel)
        await pilot.pause()
        panel.set_filter("")  # 先生成全量列表
        await pilot.pause()
        total = panel.visible_count()
        assert total > 0
        search = panel.query_one("#panel-search", Input)
        search.value = "/model"
        await pilot.pause()
        assert panel.visible_count() < total
        assert panel.visible_count() >= 1


@pytest.mark.asyncio
async def test_slash_panel_search_enter_does_not_bubble():
    """面板搜索框 Enter 不冒泡到 App 级：不 push 新面板、不新增聊天消息。"""
    app = XiaodaApp()
    async with app.run_test() as pilot:
        panel = SlashPanel(on_select=lambda cmd, chat: None)
        await app.push_screen(panel)
        await pilot.pause()
        screen_count_before = len(app.screen_stack)
        msg_count_before = len(app.query_one("#chat").children)
        search = panel.query_one("#panel-search", Input)
        search.value = "/model"
        search.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        # 面板仍为当前屏幕（未嵌套新面板）
        assert app.screen is panel
        assert len(app.screen_stack) == screen_count_before
        # 未被当作聊天消息发送
        assert len(app.query_one("#chat").children) == msg_count_before


def test_every_command_has_group():
    from slash_commands import COMMAND_DESCRIPTIONS

    all_names = {it["name"] for g in build_command_groups() for it in g["items"]}
    for name in COMMAND_DESCRIPTIONS:
        assert name in all_names, f"{name} 缺少分组归属"