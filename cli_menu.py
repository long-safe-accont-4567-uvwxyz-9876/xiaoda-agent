"""CLI 交互式单选菜单（基于 prompt_toolkit）。

供 cli.py 的多步斜杠命令逐步选择（/model 选 provider→模型、/agent 选代理等）。
方向键 ↑/↓ 或 k/j 移动，Enter 确认，Esc/Ctrl+C 取消。样式与 nahida 配色一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.styles import Style


@dataclass
class MenuItem:
    label: str
    value: str = ""
    description: str = ""

    def display(self) -> str:
        return self.label or self.value


_MENU_STYLE = Style([
    ("menu.title", "bold ansigreen"),
    ("menu.current", "bold reverse"),
    ("menu.item", ""),
    ("menu.hint", "ansibrightblack"),
])


def select_from_menu(title: str, options: list[MenuItem], hint: str = "") -> str | None:
    """交互式单选菜单。

    入参：
      title   菜单标题（如 "/model · 选择模型"）
      options 选项列表
      hint    底部操作提示（为空时用默认 ↑/↓·Enter·Esc）
    返回：
      选中项 value（value 为空时用 label）；Esc/Ctrl+C 或空选项返回 None。
    """
    if not options:
        return None
    state: dict[str, Any] = {"index": 0, "result": None}

    def render() -> list[tuple[str, str]]:
        lines: list[tuple[str, str]] = [("class:menu.title", f"{title}\n")]
        for i, opt in enumerate(options):
            marker = "→" if i == state["index"] else " "
            line = f"  {marker} {opt.display()}"
            if opt.description:
                line += f"   · {opt.description}"
            cls = "class:menu.current" if i == state["index"] else "class:menu.item"
            lines.append((cls, f"{line}\n"))
        bottom = hint or "（↑/↓ 或 k/j 移动 · Enter 确认 · Esc 取消）"
        lines.append(("class:menu.hint", f"\n{bottom}"))
        return lines

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _up(_event: Any) -> None:
        state["index"] = (state["index"] - 1) % len(options)

    @kb.add("down")
    @kb.add("j")
    def _down(_event: Any) -> None:
        state["index"] = (state["index"] + 1) % len(options)

    @kb.add("enter")
    def _confirm(event: Any) -> None:
        state["result"] = options[state["index"]].value or options[state["index"]].label
        event.app.exit()

    @kb.add("escape")
    @kb.add("c-c")
    def _cancel(event: Any) -> None:
        event.app.exit()

    app = Application(
        layout=Layout(Window(FormattedTextControl(render), style="class:menu")),
        key_bindings=kb,
        style=_MENU_STYLE,
        full_screen=True,
    )
    app.run()
    return state["result"]