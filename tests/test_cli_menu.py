from cli_menu import MenuItem, select_from_menu


def test_menu_item_display_prefers_label():
    assert MenuItem(label="小妲", value="xiaoda").display() == "小妲"
    assert MenuItem(label="", value="7d").display() == "7d"


def test_select_from_menu_empty_returns_none():
    assert select_from_menu("t", []) is None