import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_global_button_skin_does_not_guess_lightweight_naive_variants():
    css = source("web/frontend/src/styles/components.css")
    assert ".n-button--quaternary" not in css
    assert ".n-button--tertiary" not in css
    assert ".n-button--text-type" not in css
    assert "body .n-button--primary-type:not(" not in css
    assert ":has(> .n-button__border)" in css
    assert "borderRadiusMedium: '10px 5px 10px 5px'" in source("web/frontend/src/App.vue")
    assert "linear-gradient(116deg" in css


def test_view_title_rule_uses_background_rule_not_flex_pseudo_element():
    css = source("web/frontend/src/styles/components.css")
    assert "background-size: 36px 2px" in css
    assert ".view-title::after" not in css
    assert ".view-header h2::after" not in css


def test_chat_markdown_class_matches_the_scoped_style_contract():
    chat = source("web/frontend/src/views/ChatView.vue")
    assert 'class="message-content md-body streaming-text"' in chat
    assert 'class="message-content md-body"' in chat
    assert "md_body" not in chat
    assert ":deep(.md-body pre.hljs)" in chat


def test_mcp_registers_the_loading_component_used_by_market_tab():
    mcp = source("web/frontend/src/views/McpView.vue")
    imports = mcp[mcp.index("import {"):mcp.index("} from 'naive-ui'")]
    assert "NSpin" in imports
    assert "<n-spin" in mcp


def test_high_density_views_define_mobile_layouts():
    paths = (
        "web/frontend/src/views/ModelsView.vue",
        "web/frontend/src/views/McpView.vue",
        "web/frontend/src/views/RetrievalView.vue",
        "web/frontend/src/views/SettingsView.vue",
        "web/frontend/src/views/DashboardView.vue",
        "web/frontend/src/views/ChatView.vue",
        "web/frontend/src/components/chat/PromptInput.vue",
    )
    for path in paths:
        text = source(path)
        assert "@media (max-width:" in text, path


def test_sidebar_navigation_icons_all_exist_in_sumeru_icon_registry():
    sidebar = source("web/frontend/src/components/layout/SideBar.vue")
    icon_source = source("web/frontend/src/components/fx/SumeruIcon.vue")
    nav_block = sidebar[sidebar.index("const navItems = ["):sidebar.index("]", sidebar.index("const navItems = ["))]
    path_block = icon_source[icon_source.index("const PATHS:"):icon_source.index("const FILLS:")]
    nav_icons = set(re.findall(r"icon:\s*'([^']+)'", nav_block))
    registered_icons = set(re.findall(r"^\s{2}([a-zA-Z0-9_]+):\s*'", path_block, re.MULTILINE))
    assert nav_icons
    assert nav_icons <= registered_icons
