"""CLI 下拉命令面板（基于 prompt_toolkit）。

输入 `/` 的瞬间在输入行上方弹出全宽命令面板，列出全部命令 + 描述，
支持实时过滤、方向键选择、回车执行；多步命令（/model、/agent 等）在
面板内直接进入二级菜单，选完才执行，全程不离开输入行。

沿用纳西妲美术风格（叶子绿 LEAF / DGREEN / LGREEN），与 cli_menu 一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.filters import Condition
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.styles import Style


@dataclass
class PaletteNode:
    """一个可选项（叶子或可展开节点）。

    - `result` 非空 → 叶子，选中后直接返回该完整命令。
    - `children` 预置 → 选择时进入下一级。
    - `loader` 懒加载 → 进入下一级时调用返回子项列表。
    """
    label: str
    description: str = ""
    result: str | None = None
    children: list["PaletteNode"] | None = None
    loader: Callable[[], list["PaletteNode"]] | None = None

    @property
    def is_leaf(self) -> bool:
        return self.result is not None


_PALETTE_STYLE = Style([
    ("palette.title", "bold ansigreen"),
    ("palette.current", "bold reverse"),
    ("palette.item", ""),
    ("palette.desc", "ansibrightblack"),
    ("palette.hint", "ansibrightblack"),
])


class CommandPalette:
    """输入行 + 下拉命令面板一体的组合式输入界面。

    使用方式：
        palette = CommandPalette(prompt="🌿 爸爸: ", nodes=[...], history_path=...)
        chosen = palette.prompt()
        # chosen: 用户回车时的完整输入（普通消息或命令展开后的字符串）；取消返回 None
    """

    def __init__(
        self,
        prompt: str,
        nodes: list[PaletteNode],
        history_path: str | None = None,
        title: str = "📋 小妲的命令",
    ) -> None:
        self._prompt = prompt
        self._root_nodes = nodes
        self._title = title
        self._buffer = Buffer(
            history=FileHistory(history_path) if history_path else None
        )
        # on_text_changed 是 Event 对象，必须用 += 订阅（赋值会覆盖 Event 导致 redraw 崩溃）；
        # 在 __init__ 订阅一次即可，prompt() 不再重复订阅。
        self._buffer.on_text_changed += self._on_text_changed
        # state: 面板菜单栈（stack[-1] 为当前列表）、当前选中索引、面板是否打开
        self._stack: list[list[PaletteNode]] = []
        self._index = 0
        self._open = False
        self._result: str | None = None
        self._app: Application | None = None
        # 滚动：命令多于可视区时记录顶部偏移，选中项移动时自动跟随，超出屏幕也可见
        self._scroll = 0
        self._max_rows = 10  # 命令列表可视行数（不含标题行/滚动提示行）

    # ── 状态辅助 ────────────────────────────────────────────
    def _current_nodes(self) -> list[PaletteNode]:
        return self._stack[-1] if self._stack else []

    def _current_text(self) -> str:
        return self._buffer.text.lstrip()

    def _visible_nodes(self) -> list[PaletteNode]:
        """当前列表 + 实时过滤（仅根级按输入过滤，二级全量展示）。"""
        nodes = self._current_nodes()
        if len(self._stack) == 1:
            text = self._current_text()
            if text:
                return [n for n in nodes if n.label.startswith(text)]
        return nodes

    def _adjust_scroll(self) -> None:
        """选中项移动后把滚动偏移钳制到可视区，保证选中项始终可见。

        命令数不超过可视区时不滚动；超过时选中项越出上/下边界即整体平移。
        """
        total = len(self._visible_nodes())
        if total <= self._max_rows:
            self._scroll = 0
            return
        if self._index < self._scroll:
            self._scroll = self._index
        elif self._index >= self._scroll + self._max_rows:
            self._scroll = self._index - self._max_rows + 1

    def _panel_height(self) -> int:
        """面板总高度：标题 1 行 + 可视命令（≤_max_rows）+ 溢出时滚动提示 1 行。"""
        vis = len(self._visible_nodes())
        if vis == 0:
            return 2  # 标题 + "无匹配命令"提示
        if vis <= self._max_rows:
            return 1 + vis
        return 2 + self._max_rows

    def _panel_visible(self) -> bool:
        return self._open and self._current_text().startswith("/")

    # ── 键盘绑定 ────────────────────────────────────────────
    def _build_key_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("up")
        @kb.add("k")
        def _up(event: Any) -> None:
            if self._panel_visible():
                vis = self._visible_nodes()
                if vis:
                    self._index = (self._index - 1) % len(vis)
                    self._adjust_scroll()
                    self._app.invalidate()
            else:
                self._buffer.cursor_up()

        @kb.add("down")
        @kb.add("j")
        def _down(event: Any) -> None:
            if self._panel_visible():
                vis = self._visible_nodes()
                if vis:
                    self._index = (self._index + 1) % len(vis)
                    self._adjust_scroll()
                    self._app.invalidate()
            else:
                self._buffer.cursor_down()

        @kb.add("enter")
        def _enter(event: Any) -> None:
            if self._panel_visible():
                self._activate(self._index)
            else:
                text = self._buffer.text
                self._result = text if text else ""
                self._app.exit()

        @kb.add("escape")
        def _escape(event: Any) -> None:
            if len(self._stack) > 1:
                # 二级菜单：返回上一级
                self._stack.pop()
                self._index = 0
                self._app.invalidate()
            elif self._open:
                # 根级：关闭面板，保留已输入文本
                self._open = False
                self._index = 0
                self._app.invalidate()
            else:
                # 未打开：取消输入
                self._result = None
                self._app.exit()

        @kb.add("backspace")
        def _backspace(event: Any) -> None:
            if self._panel_visible():
                self._index = 0  # 输入变化时重置选中到顶部
            self._buffer.delete_before_cursor()

        return kb

    def _on_text_changed(self, _buf: Buffer) -> None:
        text = self._current_text()
        if text.startswith("/"):
            if not self._open:
                self._open = True
                self._stack = [self._root_nodes]
                self._index = 0
                self._scroll = 0
        else:
            # 删除 / 后回到普通输入
            self._open = False
            self._stack = []
            self._index = 0
            self._scroll = 0
        if self._app:
            self._app.invalidate()

    def _activate(self, index: int) -> None:
        vis = self._visible_nodes()
        if not vis:
            # 无匹配项（如输入了带参数的完整命令 /cmd xxx 或未知命令）：
            # 视为用户显式输入，直接返回原文，避免 Enter 被吞导致命令发不出去。
            self._result = self._current_text()
            if self._app:
                self._app.exit()
            return
        node = vis[index % len(vis)]
        if node.is_leaf:
            self._result = node.result
            if self._app:
                self._app.exit()
            return
        children = node.children
        if children is None and node.loader is not None:
            try:
                children = node.loader() or []
            except Exception:
                children = []
        if not children:
            # 无可选项：等同于取消，不误发
            return
        self._stack.append(children)
        self._index = 0
        self._scroll = 0
        if self._app:
            self._app.invalidate()

    # ── 渲染 ────────────────────────────────────────────────
    def _render_panel(self) -> list[tuple[str, str]]:
        # 每个 tuple 以 \n 结尾自成一行：FormattedTextControl 会把相邻不含 \n 的
        # tuple 拼在同一行，故 label 与 desc 合并为一行再换行，避免粘连与大段空白填充。
        lines: list[tuple[str, str]] = [("class:palette.title", f"{self._title}\n")]
        vis = self._visible_nodes()
        if not vis:
            lines.append(("class:palette.item", "  （无匹配命令，回车直接发送）\n"))
            return lines
        total = len(vis)
        start = self._scroll
        end = min(start + self._max_rows, total)
        for i in range(start, end):
            node = vis[i]
            cur = i == self._index % max(total, 1)
            marker = "→" if cur else " "
            cls = "class:palette.current" if cur else "class:palette.item"
            desc = f"   · {node.description}" if node.description else ""
            lines.append((cls, f"  {marker} {node.label}{desc}\n"))
        if end < total:
            # 底部还有未显示的命令：提示可继续滚动
            remaining = total - end
            lines.append(("class:palette.hint", f"  ··· 还有 {remaining} 项，继续 ↓\n"))
        return lines

    def _layout(self) -> Layout:
        # prompt 传的是 ANSI 转义字符串，须用 ANSI() 解析成 FormattedText，
        # 否则 prompt_toolkit 会把裸转义序列当纯文本原样输出（显示 ^[[32m 等）。
        prompt_ctrl = FormattedTextControl(text=ANSI(self._prompt))
        input_row = VSplit([
            # dont_extend_width：prompt 只占自身内容宽度，其余宽度全部留给输入框，避免中间大段空格
            Window(prompt_ctrl, height=1, dont_extend_height=True, dont_extend_width=True),
            Window(BufferControl(self._buffer), height=1),
        ])

        panel = HSplit([
            Window(
                FormattedTextControl(self._render_panel),
                style="class:palette",
                # 高度 = 标题 1 行 + 可视命令（≤_max_rows）+ 溢出时滚动提示 1 行；
                # 命令多于可视区时面板固定高度，选中项移动驱动 _scroll 平移内容。
                height=self._panel_height,
            ),
            Window(height=1, char="-"),
        ])

        body = HSplit([
            ConditionalContainer(
                panel,
                filter=Condition(lambda: self._panel_visible()),
            ),
            input_row,
        ])
        return Layout(body)

    def prompt(self, default: str = "") -> str | None:
        """运行输入界面，返回最终输入（命令展开后）或 None（取消）。

        `_inp` / `_out` 用于测试注入（PipeInput / DummyOutput），生产环境为 None。
        """
        if default:
            self._buffer.text = default
        self._buffer.text = ""
        self._open = False
        self._stack = []
        self._index = 0
        self._scroll = 0
        self._result: str | None = None

        self._app = Application(
            layout=self._layout(),
            key_bindings=self._build_key_bindings(),
            style=_PALETTE_STYLE,
            full_screen=False,
        )
        if getattr(self, "_inp", None) is not None:
            self._app.input = self._inp
        if getattr(self, "_out", None) is not None:
            self._app.output = self._out
        self._app.run()
        return self._result