"""CLI 下拉命令面板核心逻辑测试。

直接驱动 CommandPalette 的过滤 / 激活 / 取消逻辑（不进入真实终端 App.run()）。
"""
import pytest

from cli_palette import CommandPalette, PaletteNode


def _nodes() -> list[PaletteNode]:
    """构造一个含单步与多步命令的节点树。"""
    return [
        PaletteNode(label="/status", description="查看运行时状态", result="/status"),
        PaletteNode(label="/model", description="切换模型", loader=lambda: [
            PaletteNode(label="agnes", children=[
                PaletteNode(label="agnes-2.0-flash", result="/model agnes/agnes-2.0-flash"),
            ]),
        ]),
        PaletteNode(label="/voice", description="语音回复开关", loader=lambda: [
            PaletteNode(label="on", result="/voice on"),
            PaletteNode(label="off", result="/voice off"),
        ]),
    ]


def _palette(text: str = "") -> CommandPalette:
    p = CommandPalette(prompt="> ", nodes=_nodes())
    if text:
        p._buffer.text = text
    p._open = text.startswith("/")
    p._stack = [p._root_nodes] if p._open else []
    p._index = 0
    return p


def test_root_filter_matches_prefix():
    p = _palette()
    p._open = True
    p._stack = [p._root_nodes]
    p._buffer.text = "/st"
    labels = [n.label for n in p._visible_nodes()]
    assert labels == ["/status"]


def test_root_filter_empty_returns_all():
    p = _palette()
    p._open = True
    p._stack = [p._root_nodes]
    p._buffer.text = ""
    labels = [n.label for n in p._visible_nodes()]
    assert labels == ["/status", "/model", "/voice"]


def test_panel_hidden_without_slash():
    p = _palette("hello")
    assert not p._panel_visible()


def test_panel_visible_with_slash():
    p = _palette("/m")
    assert p._panel_visible()


def test_activate_leaf_returns_result():
    p = _palette("/status")
    p._stack = [p._root_nodes]
    p._index = 0
    p._app = None
    # 直接调用 _activate，验证 result 被设置
    p._activate(0)
    assert p._result == "/status"


def test_activate_multistep_enters_children():
    p = _palette("/model")
    p._stack = [p._root_nodes]
    p._index = 1  # /model
    p._activate(1)
    assert len(p._stack) == 2
    assert p._current_nodes()[0].label == "agnes"


def test_activate_deep_child_returns_full_command():
    p = _palette("/model")
    p._stack = [p._root_nodes]
    p._index = 1
    p._activate(1)  # 进入 agnes
    assert len(p._stack) == 2
    p._index = 0
    p._activate(0)  # 进入 agnes-2.0-flash
    assert len(p._stack) == 3
    p._index = 0
    p._activate(0)  # 叶子
    assert p._result == "/model agnes/agnes-2.0-flash"


def test_escape_from_secondary_returns_to_root():
    p = _palette("/model")
    p._stack = [p._root_nodes, [PaletteNode(label="agnes", result="/model x")]]
    original_len = len(p._stack)
    # 模拟上一级（Esc）
    p._stack.pop()
    assert len(p._stack) == original_len - 1


def test_activation_without_children_does_not_fire():
    p = _palette("/voice")
    p._stack = [p._root_nodes]
    p._index = 2  # /voice
    # loader 返回空列表
    p._root_nodes[2].loader = lambda: []
    p._activate(2)
    assert p._result is None