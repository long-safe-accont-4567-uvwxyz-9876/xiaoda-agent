import cli
import cli_client


def _make_cli():
    obj = cli.CLIInterface.__new__(cli.CLIInterface)
    obj._token = "tok"
    return obj


def test_menu_model_builds_provider_model_command(monkeypatch):
    providers = [
        {"provider": "siliconflow", "label": "硅基",
         "models": [{"id": "Qwen2.5", "display_name": "Qwen2.5", "free": True}]},
        {"provider": "mimo", "models": [
            {"id": "mimo-v2.5", "display_name": "MiMo v2.5", "free": True}]},
    ]
    monkeypatch.setattr(cli_client, "discover_models", lambda *a, **k: providers)

    # 第一层选 provider（siliconflow），第二层选模型
    calls = {"n": 0}
    def fake_menu(title, items, hint=""):
        picks = [it.value for it in items]
        calls["n"] += 1
        if calls["n"] == 1:
            assert "选择模型提供方" in title
            return "siliconflow"
        assert picks[0] == "siliconflow/Qwen2.5"
        return "siliconflow/Qwen2.5"
    monkeypatch.setattr(cli, "select_from_menu", fake_menu)

    obj = _make_cli()
    assert obj._menu_model() == "/model siliconflow/Qwen2.5"
    assert calls["n"] == 2


def test_menu_model_returns_none_when_fetch_empty(monkeypatch):
    monkeypatch.setattr(cli_client, "discover_models", lambda *a, **k: [])
    obj = _make_cli()
    assert obj._menu_model() is None


def test_menu_agent_builds_command(monkeypatch):
    agents = [{"name": "xiaoda", "display_name": "小妲"},
              {"name": "xiaoli", "display_name": "小莉"}]
    monkeypatch.setattr(cli_client, "list_agents", lambda *a, **k: agents)
    monkeypatch.setattr(cli, "select_from_menu", lambda t, items, hint="": items[1].value)
    obj = _make_cli()
    assert obj._menu_agent() == "/agent xiaoli"


def test_try_expand_skips_when_arg_present(monkeypatch):
    obj = _make_cli()
    monkeypatch.setattr(cli, "select_from_menu", lambda *a, **k: None)
    assert obj._try_expand_multistep("/model", "siliconflow/Qwen2.5") is None


def test_multistep_cancel_does_not_send(monkeypatch):
    obj = cli.CLIInterface.__new__(cli.CLIInterface)
    obj._ws = object()  # 若误发，chat() 会 AttributeError
    monkeypatch.setattr(cli.CLIInterface, "_try_expand_multistep", lambda self, cmd, arg: None)
    monkeypatch.setattr(cli, "_HAS_MENU", True)
    obj._dispatch_slash_command("/model")  # 修复后不应调用 chat()


def test_palette_nodes_single_step_are_leaves():
    """单步命令带 result（选中后直接返回命令自身），多步命令带 loader。"""
    obj = cli.CLIInterface.__new__(cli.CLIInterface)
    nodes = obj._palette_nodes()
    by_label = {n.label: n for n in nodes}
    # 单步命令：is_leaf 且 result == 命令名，否则面板选中后 Enter 无反应
    for name in ("/status", "/memory", "/reset", "/forget", "/help"):
        assert name in by_label, name
        assert by_label[name].is_leaf, name
        assert by_label[name].result == name, name
    # 多步命令：非叶子、带 loader、无 result
    for name in ("/model", "/voice", "/doctor", "/cost", "/cam", "/agent"):
        assert name in by_label, name
        assert not by_label[name].is_leaf, name
        assert by_label[name].loader is not None, name
        assert by_label[name].result is None, name


def test_all_commands_activatable_from_palette(monkeypatch):
    """全部 21 个斜杠命令在面板中都能被激活：单步直接返回，多步进入二级。"""
    from cli_palette import CommandPalette
    from slash_commands import COMMAND_DESCRIPTIONS

    # mock 远程数据源：避免 /model、/agent 的 loader 真实网络调用
    monkeypatch.setattr(cli_client, "discover_models", lambda *a, **k: [
        {"provider": "agnes", "label": "AGNES",
         "models": [{"id": "agnes-2.0-flash", "display_name": "Agnes 2.0 Flash"}]},
    ])
    monkeypatch.setattr(cli_client, "list_agents", lambda *a, **k: [
        {"name": "xiaoli", "display_name": "小莉"},
    ])

    obj = cli.CLIInterface.__new__(cli.CLIInterface)
    obj._token = "tok"
    nodes = obj._palette_nodes()
    by_label = {n.label: n for n in nodes}
    assert set(by_label) == set(COMMAND_DESCRIPTIONS)

    for name in COMMAND_DESCRIPTIONS:
        p = CommandPalette(prompt="> ", nodes=nodes)
        p._buffer.text = name
        p._open = True
        p._stack = [p._root_nodes]
        vis = p._visible_nodes()
        idx = next(i for i, n in enumerate(vis) if n.label == name)
        p._index = idx
        p._app = None
        p._activate(idx)
        node = by_label[name]
        if node.is_leaf:
            assert p._result == name, f"{name} 单步命令应返回自身"
            assert len(p._stack) == 1
        else:
            # 多步命令：进入二级且不误发裸命令
            assert p._result is None, f"{name} 多步命令不应直接发送"
            assert len(p._stack) == 2, f"{name} 应进入二级"
            assert p._stack[1], f"{name} 二级选项不应为空"


def test_palette_no_match_sends_raw_text():
    """带参数命令/未知命令（面板无匹配）→ 回车直接发送原文，不被吞。"""
    from cli_palette import CommandPalette

    obj = cli.CLIInterface.__new__(cli.CLIInterface)
    nodes = obj._palette_nodes()
    for raw in ("/cost 7d", "/doctor fix", "/model agnes/agnes-2.0-flash",
                "/wf 报告生成", "/voice on", "/notacmd"):
        p = CommandPalette(prompt="> ", nodes=nodes)
        p._buffer.text = raw
        p._open = True
        p._stack = [p._root_nodes]
        p._index = 0
        p._app = None
        p._activate(0)
        assert p._result == raw, f"{raw}: 应发送原文, got {p._result!r}"


def test_try_expand_and_palette_multistep_sets_align():
    """面板 loader、_try_expand_multistep、_MULTI_STEP_COMMANDS 三者集合一致。"""
    import inspect
    import re

    obj = cli.CLIInterface.__new__(cli.CLIInterface)
    src = inspect.getsource(obj._try_expand_multistep)
    expand_cmds = set(re.findall(r'if cmd == "(/[a-z]+)"', src))
    assert expand_cmds == cli._MULTI_STEP_COMMANDS, (
        f"_try_expand_multistep 分支与 _MULTI_STEP_COMMANDS 不一致: "
        f"{expand_cmds} vs {cli._MULTI_STEP_COMMANDS}")
    loader_cmds = {n.label for n in obj._palette_nodes() if n.loader is not None}
    assert loader_cmds == cli._MULTI_STEP_COMMANDS, (
        f"面板 loader 与 _MULTI_STEP_COMMANDS 不一致: {loader_cmds} vs {cli._MULTI_STEP_COMMANDS}")
