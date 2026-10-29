"""可选 CLI 依赖缺失时的占位实现。

为什么单独一个模块：`cli.py` 属于巨型热点文件，仓库里有
`tests/test_giant_file_ratchet.py` 的止血液轮测试限制它继续增长。
这些占位类只在对应依赖缺失的降级路径上用到，放进本体等于让热点文件
为"降级分支"持续膨胀，所以集中到这里，`cli.py` 只做一次 import。

三组占位对应三种可选依赖：

1. `prompt_toolkit` 缺失 → `Completer` / `Completion`
   `cli.py` 里 `class _SlashCompleter(Completer)` 在 import 阶段就会求值基类，
   缺基类会直接 NameError，让整个 cli 模块导入失败，违背"优雅回退 readline"的约定。

2. `cli_menu` 缺失 → `MenuItem` / `select_from_menu`
   同理，保证模块级类型标注可求值；占位实现尽量贴近真实接口
   （label/value/description + kwargs 转属性），避免降级路径访问 `.value` 抛 AttributeError。

3. `cli_palette` 缺失 → `PaletteNode` / `CommandPalette`
   `def _palette_nodes(self) -> list[PaletteNode]` 的返回标注会在
   `CLIInterface` 类定义阶段求值，缺 `PaletteNode` 同样会 NameError。

所有占位类都不会被真正实例化：`cli.py` 里每条使用路径都有
`_HAS_PROMPT_TOOLKIT` / `_HAS_MENU` / `_HAS_PALETTE` 守卫。
"""

from __future__ import annotations

from collections.abc import Callable


# ── prompt_toolkit 占位 ────────────────────────────────────
class Completer:
    """`prompt_toolkit.completion.Completer` 缺失时的占位基类。"""


class Completion:
    """`prompt_toolkit.completion.Completion` 缺失时的占位补全项。"""

    def __init__(self, *args, **kwargs) -> None:
        pass


# ── cli_menu 占位 ──────────────────────────────────────────
class MenuItem:
    """`cli_menu.MenuItem` 缺失时的占位菜单项。"""

    label: str = ""
    value: str = ""
    description: str = ""

    def __init__(self, *args, **kwargs) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)

    def display(self) -> str:
        return self.label or self.value


def select_from_menu(*args, **kwargs):
    """`cli_menu.select_from_menu` 缺失时的占位选择器，恒返回 None。"""
    return None


# ── cli_palette 占位 ───────────────────────────────────────
class PaletteNode:
    """`cli_palette.PaletteNode` 缺失时的占位节点。"""

    label: str = ""
    description: str = ""
    result: str | None = None
    children: list | None = None
    loader: Callable | None = None

    def __init__(self, *args, **kwargs) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)

    @property
    def is_leaf(self) -> bool:
        return self.result is not None


class CommandPalette:
    """`cli_palette.CommandPalette` 缺失时的占位面板。"""

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs

    def prompt(self, *args, **kwargs):
        """降级路径：无交互面板时返回 None，由调用方回退到 readline 输入。"""
        return None


__all__ = [
    "CommandPalette",
    "Completer",
    "Completion",
    "MenuItem",
    "PaletteNode",
    "select_from_menu",
]
