from pathlib import Path

ROOT = Path(__file__).parents[1]


def source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_mobile_sidebar_has_explicit_open_close_paths():
    layout = source("web/frontend/src/components/layout/AppLayout.vue")
    assert "desktopSidebarExpanded" in layout
    assert "mobileSidebarOpen" in layout
    assert '@click="closeMobileSidebar"' in layout
    assert "event.key === 'Escape'" in layout
    assert "watch(() => route.fullPath, closeMobileSidebar)" in layout


def test_topbar_and_sidebar_expose_accessible_navigation_controls():
    topbar = source("web/frontend/src/components/layout/TopBar.vue")
    sidebar = source("web/frontend/src/components/layout/SideBar.vue")
    assert "toggle-sidebar" in topbar
    assert ':aria-expanded="mobileSidebarOpen"' in topbar
    assert 'aria-controls="app-sidebar"' in topbar
    assert 'id="app-sidebar"' in sidebar
    assert ':aria-label="t(\'nav.mainNavigation\')"' in sidebar
    assert "emit('close')" in sidebar


def test_topbar_exposes_agent_and_connection_state_semantics():
    topbar = source("web/frontend/src/components/layout/TopBar.vue")
    assert "failedAvatars" in topbar
    assert ':aria-pressed="chat.currentAgent === a.name"' in topbar
    assert 'role="status"' in topbar
    assert "connectionStatusText" in topbar


def test_prompt_input_forwards_keydown_before_default_send():
    prompt = source("web/frontend/src/components/chat/PromptInput.vue")
    handler = prompt[prompt.index("function handleKeydown"):prompt.index("function handleSend")]
    assert "'keydown': [event: KeyboardEvent]" in prompt
    assert "emit('keydown', e)" in handler
    assert "if (e.defaultPrevented) return" in handler
    assert handler.index("emit('keydown', e)") < handler.index("if (e.defaultPrevented) return")


def test_slash_palette_exposes_listbox_semantics():
    palette = source("web/frontend/src/components/chat/SlashPalette.vue")
    assert 'role="listbox"' in palette
    assert 'role="option"' in palette
    assert ':aria-selected="i === activeIndex"' in palette
    assert "selectCurrent" in palette
    # aria-activedescendant 必须挂在拥有焦点的 textarea（combobox）上，而非 listbox 本身；
    # listbox 只需暴露稳定 id 供 aria-controls 引用，并向父层暴露当前高亮项 id
    assert ':id="listboxId"' in palette
    assert "listboxId," in palette
    assert "activeOptionId," in palette
    assert ':aria-activedescendant="activeOptionId"' not in palette


def test_prompt_textarea_is_a_combobox_bound_to_palette():
    prompt = source("web/frontend/src/components/chat/PromptInput.vue")
    assert 'role="combobox"' in prompt
    assert 'aria-autocomplete="list"' in prompt
    assert ':aria-expanded="comboboxExpanded"' in prompt
    assert ':aria-controls="comboboxControls"' in prompt
    assert ':aria-activedescendant="comboboxActiveOption"' in prompt


def test_chat_view_drives_combobox_state_from_palette():
    view = source("web/frontend/src/views/ChatView.vue")
    assert "paletteExpanded" in view
    assert "paletteListboxId" in view
    assert "paletteActiveOption" in view
    assert ':combobox-expanded="paletteExpanded"' in view
    assert ':combobox-controls="paletteListboxId"' in view
    assert ':combobox-active-option="paletteActiveOption"' in view


def test_palette_escape_stops_propagation_so_sidebar_is_not_also_closed():
    view = source("web/frontend/src/views/ChatView.vue")
    handler = view[view.index("function handleKeydown"):view.index("function selectCommand")]
    escape_line = next(l for l in handler.splitlines() if "Escape" in l)
    # 面板 Escape 必须同时阻止默认与冒泡，避免同一次 Escape 冒泡到 shell 关闭移动侧栏
    assert "e.preventDefault()" in escape_line
    assert "e.stopPropagation()" in escape_line


def test_shell_escape_only_consumes_when_mobile_sidebar_open():
    layout = source("web/frontend/src/components/layout/AppLayout.vue")
    handler = layout[layout.index("function onShellKeydown"):layout.index("watch(")]
    assert "mobileSidebarOpen.value" in handler


def test_mobile_sidebar_isolates_focus_when_closed():
    sidebar = source("web/frontend/src/components/layout/SideBar.vue")
    # 移动浮层关闭时必须以 inert 从 Tab 序与辅助技术中移除，仅靠 transform 位移不够
    assert "focusIsolated" in sidebar
    assert ':inert="focusIsolated"' in sidebar
    assert ':aria-hidden="focusIsolated || undefined"' in sidebar
    assert "isMobileViewport" in sidebar


def test_sidebar_hover_expand_requires_fine_pointer():
    sidebar = source("web/frontend/src/components/layout/SideBar.vue")
    # 触屏（粗指针）不应触发 hover 展开
    assert "(pointer: fine)" in sidebar
    assert "hoverCapable" in sidebar
    assert '@mouseenter="onEnter"' in sidebar
    assert '@mouseleave="onLeave"' in sidebar


def test_full_height_uses_dynamic_viewport_units():
    layout = source("web/frontend/src/components/layout/AppLayout.vue")
    sidebar = source("web/frontend/src/components/layout/SideBar.vue")
    assert "height: 100dvh" in layout
    assert "height: 100dvh" in sidebar


def test_chat_view_closes_palette_and_intercepts_navigation_keys():
    view = source("web/frontend/src/views/ChatView.vue")
    handler = view[view.index("function handleKeydown"):view.index("function selectCommand")]
    assert '@keydown="handleKeydown"' in view
    assert "paletteRef.value.move(1)" in handler
    assert "paletteRef.value.move(-1)" in handler
    assert "paletteRef.value.selectCurrent()" in handler
    assert "paletteDismissed" in view
    assert "e.key === 'Escape'" in handler


def test_responsive_tokens_and_reduced_motion_are_defined():
    tokens = source("web/frontend/src/styles/sumeru-tokens.css")
    layout = source("web/frontend/src/components/layout/AppLayout.vue")
    sidebar = source("web/frontend/src/components/layout/SideBar.vue")
    assert "--sidebar-mobile-width: min(82vw, 320px)" in tokens
    assert "--z-overlay: 70" in tokens
    assert "--z-sidebar: 80" in tokens
    assert "--z-palette: 90" in tokens
    assert "--motion-fast: 120ms" in tokens
    assert "--motion-normal: 220ms" in tokens
    assert "prefers-reduced-motion: reduce" in layout
    assert "prefers-reduced-motion: reduce" in sidebar
