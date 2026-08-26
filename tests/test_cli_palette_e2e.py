"""CommandPalette 端到端测试：用 PipeInput 注入按键，驱动真实 Application 流程。

覆盖：输入 `/` 弹出面板 → 输入过滤 → 回车执行单步命令 → 多步命令进入二级选择。
"""
from cli_palette import CommandPalette, PaletteNode


def _nodes():
    return [
        PaletteNode(label="/status", description="查看运行时状态", result="/status"),
        PaletteNode(label="/model", description="切换模型", loader=lambda: [
            PaletteNode(label="agnes", children=[
                PaletteNode(label="agnes-2.0-flash", result="/model agnes/agnes-2.0-flash"),
            ]),
        ]),
    ]


def test_e2e_slash_opens_panel_and_leaf_executes():
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    p = CommandPalette(prompt="> ", nodes=_nodes())
    with create_pipe_input() as inp:
        inp.send_text("/status\r")
        p._inp = inp
        p._out = DummyOutput()
        result = p.prompt()
    assert result == "/status"


def test_e2e_multistep_enters_children():
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    p = CommandPalette(prompt="> ", nodes=_nodes())
    with create_pipe_input() as inp:
        # /model 是多步命令：回车进入二级(agnes) → 再回车进入模型列表 → 再回车选中叶子
        inp.send_text("/model\r\r\r")
        p._inp = inp
        p._out = DummyOutput()
        result = p.prompt()
    assert result == "/model agnes/agnes-2.0-flash"
