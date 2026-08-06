### Task 1: `cli_common.py` 共享助手 + textual 依赖

**Files:**
- Modify: `requirements.txt`
- Create: `cli_common.py`
- Create: `tests/test_cli_common.py`
- Modify: `cli.py`（把本地重复的助手函数改为从 `cli_common` 导入）

**Interfaces:**
- Produces:
  - `cli_common.STYLE: dict[str, str]`（纳西妲绿色主题色板）
  - `cli_common.status_translate(msg: str) -> str`
  - `cli_common.get_model_info(token: str = "") -> str`
  - `cli_common.command_entries() -> tuple[list[tuple[str,str]], list[tuple[str,str]]]`（public, owner）
  - `cli_common.address_term() -> str`
  - `cli_common.cli_should_use_tui() -> bool`

- [ ] **Step 1: 在 requirements.txt 新增 textual**

在 `requirements.txt` 的 `rich>=13.9.0` 附近新增一行：

```text
textual>=0.80.0
```

- [ ] **Step 2: 写失败测试 `tests/test_cli_common.py`**

```python
from cli_common import STYLE, status_translate, get_model_info, command_entries, address_term, cli_should_use_tui


def test_style_has_key_colors():
    assert "leaf" in STYLE and "border" in STYLE and "assistant" in STYLE


def test_status_translate_maps_known():
    assert status_translate("thinking") == "思考中…"


def test_status_translate_falls_back():
    assert status_translate("zzz_unknown") == "zzz_unknown"


def test_get_model_info_fallback():
    # 无主进程时返回默认模型名
    assert get_model_info() == "mimo-v2.5"


def test_command_entries_include_help():
    public, owner = command_entries()
    assert any(n == "/help" for n, _ in public) or any(n == "/help" for n, _ in owner)


def test_address_term_fallback():
    assert address_term() in ("朋友", "爸爸")


def test_cli_should_use_tui_returns_bool():
    assert isinstance(cli_should_use_tui(), bool)
```

- [ ] **Step 3: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_cli_common.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cli_common'`

- [ ] **Step 4: 创建 `cli_common.py`**

```python
"""CLI 共享助手与主题色板。

供 `cli.py`（prompt_toolkit 降级路径）与 `cli_app.py`（Textual TUI）共用，
避免两套终端界面各自维护重复的取色/翻译/元数据函数。
"""
from __future__ import annotations

import os
import sys

# 纳西妲绿色主题色板（与 WebUI 品牌色一致）
STYLE: dict[str, str] = {
    "bg": "black",
    "panel": "#1e2a1e",
    "border": "#8bc34a",
    "accent": "#ffd54f",
    "gold": "#ffd54f",
    "user": "#a5d6a7",
    "assistant": "#e8f5e9",
    "muted": "#6b8f6b",
    "leaf": "#8bc34a",
    "grass": "#33691e",
}


def status_translate(msg: str) -> str:
    """把主进程 WS 状态事件翻译成友好中文。未识别时原样返回。"""
    table = {
        "thinking": "思考中…",
        "tool_calling": "调用工具中…",
        "searching": "搜索中…",
        "writing": "书写中…",
        "done": "完成",
    }
    return table.get(msg, msg)


def get_model_info(token: str = "") -> str:
    """读取当前聊天模型显示名；无主进程时返回默认模型名。"""
    import cli_client
    if token:
        try:
            return cli_client.get_chat_model_label(token)
        except Exception:
            return "mimo-v2.5"
    return "mimo-v2.5"


def command_entries() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """从 slash_commands 权威数据源取命令表（公共, 主人级）。"""
    from slash_commands import COMMAND_DESCRIPTIONS, OWNER_ONLY_COMMANDS
    public: list[tuple[str, str]] = []
    owner: list[tuple[str, str]] = []
    for name, desc in COMMAND_DESCRIPTIONS.items():
        if name in OWNER_ONLY_COMMANDS:
            owner.append((name, desc))
        else:
            public.append((name, desc))
    return public, owner


def address_term() -> str:
    """读取当前用户称呼（USER.md 动态），未设置兜底"朋友"。"""
    try:
        from agent_core.core import AgentCore
        term = AgentCore.read_address_term_from_user_md()
        if term:
            return term
    except ImportError:
        pass
    return "朋友"


def cli_should_use_tui() -> bool:
    """判定是否启用 Textual TUI：可导入 && 交互式终端 && TERM 非 dumb。"""
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    if not sys.stdin.isatty():
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return True
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_cli_common.py -v`
Expected: 7 passed

- [ ] **Step 6: 重构 `cli.py` 复用 `cli_common`**

在 `cli.py` 顶部 `import cli_client` 之后加：

```python
from cli_common import STYLE, status_translate, get_model_info, command_entries, address_term
```

将 `cli.py` 里现存的本地定义替换为指向 `cli_common` 的别名（删除重复实现，保留 `_C` 兼容名）：

```python
# ANSI 颜色常量沿用现有命名，取值来自 cli_common.STYLE（Textual 用 STYLE，降级路径用 _C）
_C = STYLE
_status_translate = status_translate
_get_model_info = get_model_info
_command_entries = command_entries
```

并删除 `cli.py` 中原先独立的 `_status_translate`、`_get_model_info`、`_command_entries`、`_address_term`/`read_address_term_from_user_md` 重复实现，改为使用上述别名（`_address_term` 已在 `cli_common`，把 `CLIInterface._address_term` 方法体替换为 `return address_term()`）。

注意：`_refresh_model_arg_cache` 与 `_MODEL_ARG_CACHE` 是 prompt_toolkit 补全专用，**保留在 cli.py**，不迁移。

- [ ] **Step 7: 运行 CLI 相关测试确认无回归**

Run: `.venv/bin/python -m pytest tests/test_cli_common.py tests/test_cli_multistep.py tests/test_cli_menu.py -q`
Expected: 全部通过

- [ ] **Step 8: 提交**

```bash
git add requirements.txt cli_common.py tests/test_cli_common.py cli.py
git commit -m "feat(cli): 抽取 cli_common 共享助手与主题色板，新增 textual 依赖"
```

---

