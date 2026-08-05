# CLI 交互式斜杠命令实现计划（prompt_toolkit）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 CLI 输入 `/` 即弹出命令下拉，并把多步斜杠命令（`/model` `/agent` `/voice` 等）改为方向键+回车的菜单选择器，全程自动补全、无需手动记忆命令名。

**Architecture:** 输入层由 `input()+readline` 替换为 prompt_toolkit 的 `PromptSession`（`complete_while_typing=True` 实现 `/` 实时弹下拉）；新增可复用菜单模块 `cli_menu.py`（方向键选择）；多步命令在 `cli.py` 命令分发层识别 → 从 `cli_client.py` 的 HTTP 助手拉取动态选项（模型/代理）→ 弹菜单 → 拼接完整命令发给主进程共享 AgentCore。prompt_toolkit 缺失时优雅回退到现有 readline 路径，不崩溃。

**Tech Stack:** Python 3.11、prompt_toolkit（新增）、urllib（已有 HTTP 客户端）、PyInstaller（打包）。

## Global Constraints

- 依赖版本：新增 `prompt_toolkit>=3.0.0`（写入 `requirements.txt` 的 `# ── CLI 美化 ──` 段）。
- 命令名来源必须与 WebUI 同源：`slash_commands.COMMAND_DESCRIPTIONS` + `COMMAND_ALIASES`，不得另维护硬编码列表。
- 多步命令的合法参数来源：`slash_commands.COMMAND_META` 的 `arg_completions`；`/model` 参数动态来自模型发现缓存。
- 数据单一来源：CLI 仍作为主进程客户端，所有模型/代理选项经 `cli_client.py` 从主进程 HTTP 拉取，不读本地静态配置。
- 优雅降级：`prompt_toolkit` 或 `cli_menu` 缺失时，CLI 回退到现有 readline 行为，不得闪退。
- 平台：Windows 原生终端也需可用（prompt_toolkit 原生支持）。
- 用户称呼不得硬编码（沿用现有 `self._address_term()` 动态读取）。

---

### Task 1: 声明 prompt_toolkit 依赖并补齐打包 hiddenimports

**Files:**
- Modify: `requirements.txt`（`# ── CLI 美化 ──` 段，`rich>=13.9.0` 之后）
- Modify: `xiaoda-agent.spec`（`hiddenimports` 列表 + `collect_submodules` 循环）

**Interfaces:**
- Consumes: 无
- Produces: 环境具备 `prompt_toolkit`；打包产物包含其全部子模块。

- [ ] **Step 1: 在 requirements.txt 添加依赖**

在 `rich>=13.9.0` 行后新增一行：

```
# ── CLI 交互补全（/ 弹出下拉 + 多步菜单选择）──
prompt-toolkit>=3.0.0
```

- [ ] **Step 2: 在 xiaoda-agent.spec 的 hiddenimports 添加模块**

在 `hiddenimports` 列表末尾（`'web.middleware.rate_limit',` 之后）追加：

```python
    # CLI 交互输入（prompt_toolkit 动态 import，需显式声明）
    'prompt_toolkit',
    'prompt_toolkit.application',
    'prompt_toolkit.auto_suggest',
    'prompt_toolkit.completion',
    'prompt_toolkit.filters',
    'prompt_toolkit.formatted_text',
    'prompt_toolkit.history',
    'prompt_toolkit.key_binding',
    'prompt_toolkit.layout',
    'prompt_toolkit.layout.controls',
    'prompt_toolkit.styles',
```

- [ ] **Step 3: 把 prompt_toolkit 加入 collect_submodules 循环**

在 spec 中 `collect_submodules` 的包元组 `(... , 'h2', 'hpack', 'hyperframe'))` 里追加 `'prompt_toolkit'`：

```python
for pkg in ('openai', 'pydantic', 'starlette', 'anyio', 'uvicorn', 'psutil', 'httpx', 'certifi', 'httpcore', 'pilk', 'PIL', 'webview', 'h2', 'hpack', 'hyperframe', 'prompt_toolkit'):
```

- [ ] **Step 4: 验证依赖可导入**

Run: `python -c "import prompt_toolkit; print(prompt_toolkit.__version__)"`
Expected: 打印版本号（如 `3.0.50`）。若未安装，先 `pip install "prompt-toolkit>=3.0.0"`。

- [ ] **Step 5: Commit**

```bash
git add requirements.txt xiaoda-agent.spec
git commit -m "deps(cli): 引入 prompt_toolkit 并补齐 PyInstaller hiddenimports"
```

---

### Task 2: cli_client.py 补回 discover_models 与 list_agents

**Files:**
- Modify: `cli_client.py`（在 `get_chat_model_label` 之后追加）
- Test: `tests/test_cli_client_http.py`

**Interfaces:**
- Consumes: `cli_client._http_get_json(path, token, host, port, timeout)` 和 `cli_client.logger`（均已在模块内）。
- Produces:
  - `discover_models(token: str, host: str | None = None, port: int | None = None, timeout: float = 15.0) -> list[dict]`
  - `list_agents(token: str, host: str | None = None, port: int | None = None, timeout: float = 15.0) -> list[dict]`
  - 两者失败均返回 `[]`，不抛异常。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_cli_client_http.py`：

```python
import cli_client


def test_discover_models_parses_provider_list(monkeypatch):
    payload = {"data": [
        {"provider": "siliconflow", "label": "硅基流动",
         "models": [{"id": "Qwen2.5", "display_name": "Qwen2.5", "free": True}]},
        {"provider": "mimo", "models": [
            {"id": "mimo-v2.5", "display_name": "MiMo v2.5", "free": True}]},
    ]}
    monkeypatch.setattr(cli_client, "_http_get_json", lambda *a, **k: payload)
    providers = cli_client.discover_models("tok")
    assert providers == payload["data"]


def test_discover_models_returns_empty_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(cli_client, "_http_get_json", boom)
    assert cli_client.discover_models("tok") == []


def test_list_agents_parses_list(monkeypatch):
    payload = {"data": [{"name": "xiaoda", "display_name": "小妲"}]}
    monkeypatch.setattr(cli_client, "_http_get_json", lambda *a, **k: payload)
    assert cli_client.list_agents("tok") == [{"name": "xiaoda", "display_name": "小妲"}]


def test_list_agents_returns_empty_on_non_list(monkeypatch):
    monkeypatch.setattr(cli_client, "_http_get_json", lambda *a, **k: {"data": {}})
    assert cli_client.list_agents("tok") == []
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_cli_client_http.py -v`
Expected: FAIL（`AttributeError: module 'cli_client' has no attribute 'discover_models'`）。

- [ ] **Step 3: 在 cli_client.py 实现**

在 `get_chat_model_label` 函数之后追加：

```python
def discover_models(token: str, host: str | None = None, port: int | None = None,
                    timeout: float = 15.0) -> list[dict]:
    """从主进程拉取已发现模型（provider 分组列表）。

    GET /api/v1/models/discover 返回 Envelope(data=[{provider,label,models:[...]}, ...])。
    失败或结构不符时返回空列表，由调用方回退为手动输入。
    """
    try:
        data = _http_get_json("/api/v1/models/discover", token, host, port, timeout)
        providers = data.get("data")
        return providers if isinstance(providers, list) else []
    except Exception:
        logger.debug("cli_client.discover_models_error", exc_info=True)
        return []


def list_agents(token: str, host: str | None = None, port: int | None = None,
                timeout: float = 15.0) -> list[dict]:
    """从主进程拉取代理列表。

    GET /api/v1/agents 返回 Envelope(data=[{name,display_name,...}, ...])。
    失败或结构不符时返回空列表。
    """
    try:
        data = _http_get_json("/api/v1/agents", token, host, port, timeout)
        agents = data.get("data")
        return agents if isinstance(agents, list) else []
    except Exception:
        logger.debug("cli_client.list_agents_error", exc_info=True)
        return []
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_cli_client_http.py -v`
Expected: PASS（4 passed）。

- [ ] **Step 5: Commit**

```bash
git add cli_client.py tests/test_cli_client_http.py
git commit -m "feat(cli): cli_client 补回 discover_models / list_agents HTTP 助手"
```

---

### Task 3: 新增可复用菜单选择器 cli_menu.py

**Files:**
- Create: `cli_menu.py`
- Test: `tests/test_cli_menu.py`

**Interfaces:**
- Consumes: `prompt_toolkit`（`application` / `key_binding` / `layout` / `styles`）。
- Produces:
  - `MenuItem(label: str, value: str = "", description: str = "")` dataclass，`display() -> str` 返回 `label or value`。
  - `select_from_menu(title: str, options: list[MenuItem], hint: str = "") -> str | None`：返回选中项 `value`（value 为空用 `label`）；Esc/Ctrl+C 或空选项返回 `None`。

- [ ] **Step 1: 写测试**

创建 `tests/test_cli_menu.py`：

```python
from cli_menu import MenuItem, select_from_menu


def test_menu_item_display_prefers_label():
    assert MenuItem(label="小妲", value="xiaoda").display() == "小妲"
    assert MenuItem(label="", value="7d").display() == "7d"


def test_select_from_menu_empty_returns_none():
    assert select_from_menu("t", []) is None
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_cli_menu.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'cli_menu'`）。

- [ ] **Step 3: 创建 cli_menu.py**

```python
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
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_cli_menu.py -v`
Expected: PASS（2 passed）。

- [ ] **Step 5: Commit**

```bash
git add cli_menu.py tests/test_cli_menu.py
git commit -m "feat(cli): 新增可复用交互式单选菜单 cli_menu"
```

---

### Task 4: cli.py 集成 prompt_toolkit 输入 + 命令补全 + 多步菜单

**Files:**
- Modify: `cli.py`
- Test: `tests/test_cli_multistep.py`

**Interfaces:**
- Consumes: `_SlashCompleter`（本任务定义）、`cli_menu.MenuItem / select_from_menu`、`cli_client.discover_models / list_agents`、`slash_commands.COMMAND_DESCRIPTIONS / COMMAND_ALIASES / resolve_command / get_argument_completions`、`_all_command_names()`（cli.py 已有）。
- Produces: `CLIInterface` 新增私有方法 `_init_prompt_session()`、`_try_expand_multistep(cmd, arg) -> str | None`、`_menu_model() -> str | None`、`_menu_agent() -> str | None`、`_menu_fixed(cmd, choices) -> str | None`；`run()` 改用 prompt_toolkit 输入。

- [ ] **Step 1: 写测试（纯命令拼接逻辑，mock 数据源）**

创建 `tests/test_cli_multistep.py`：

```python
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
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_cli_multistep.py -v`
Expected: FAIL（`AttributeError: 'CLIInterface' object has no attribute '_menu_model'`）。

- [ ] **Step 3: 在 cli.py 顶部加 prompt_toolkit 探测与导入**

在 `import cli_client`、`import contextlib` 之后插入：

```python
# ── prompt_toolkit 支持（/ 弹出下拉 + 菜单选择）──────────────
# 缺失时优雅回退到 readline 路径，不崩溃（旧安装包兼容）。
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import FileHistory
    _HAS_PROMPT_TOOLKIT = True
except ImportError:
    _HAS_PROMPT_TOOLKIT = False

try:
    from cli_menu import MenuItem, select_from_menu
    _HAS_MENU = True
except ImportError:
    _HAS_MENU = False
```

- [ ] **Step 4: 新增模块级命令名常量**

在 `_all_command_names()` 定义之后新增：

```python
_ALL_CMD_NAMES = _all_command_names()  # 与 WebUI 同源（COMMAND_DESCRIPTIONS + 别名）
```

- [ ] **Step 5: 新增 SlashCompleter 类**

在 `_model_arg_completions` 函数之后新增：

```python
class _SlashCompleter(Completer):
    """斜杠命令补全：/ 输入即弹出命令下拉，命令后跟空格补全参数。

    命令名来源与 WebUI 同源（slash_commands.COMMAND_DESCRIPTIONS + 别名）。
    /model 参数动态从模型发现缓存实时补全，其余命令用 slash_commands 声明式参数。
    """

    def _arg_completions(self, command: str, partial: str) -> list[str]:
        if command == "/model":
            try:
                from model_router import list_discovered_model_ids
                opts = list_discovered_model_ids()
            except Exception:
                opts = []
        else:
            try:
                from slash_commands import get_argument_completions
                opts = get_argument_completions(command, partial)
            except ImportError:
                opts = []
        return [o for o in opts if o.startswith(partial)]

    def get_completions(self, document, complete_event):
        line = document.current_line_before_cursor.lstrip()
        if not line.startswith("/"):
            return
        parts = line.split(maxsplit=1)
        word = document.get_word_before_cursor(WORD=True)
        start = -len(word) if word else 0
        if len(parts) == 1:
            for name in _ALL_CMD_NAMES:
                if word and name.startswith(word) and name != word:
                    yield Completion(name, start_position=start)
        else:
            command = parts[0]
            try:
                from slash_commands import resolve_command
                command = resolve_command(command)
            except ImportError:
                pass
            for cand in self._arg_completions(command, word):
                yield Completion(cand, start_position=start)
```

- [ ] **Step 6: 在 CLIInterface 新增 prompt session 初始化与多步方法**

在 `_address_term` 方法之后插入：

```python
    def _init_prompt_session(self) -> None:
        """初始化 prompt_toolkit 会话（含历史、自动建议、斜杠补全）。"""
        if not _HAS_PROMPT_TOOLKIT:
            self._session = None
            return
        hist_path = os.path.expanduser("~/.ai-agent/cli_history")
        self._session = PromptSession(
            history=FileHistory(hist_path),
            auto_suggest=AutoSuggestFromHistory(),
            completer=_SlashCompleter(_ALL_CMD_NAMES),
            complete_while_typing=True,
        )

    def _menu_fixed(self, cmd: str, choices: list[str]) -> str | None:
        """固定参数多步命令：从 choices 单选，返回完整命令。"""
        items = [MenuItem(label=v, value=f"{cmd} {v}") for v in choices]
        return select_from_menu(f"{cmd} · 选择参数", items)

    def _menu_model(self) -> str | None:
        """/model 多步：先选 provider，再选该 provider 下模型。失败返回 None。"""
        providers = cli_client.discover_models(self._token)
        if not providers:
            print(f"\n  {_C.LYELLOW}模型选项加载失败，请手动输入 /model provider/模型{_C.RST}")
            return None
        p_items = []
        for p in providers:
            pid = p.get("provider") or p.get("id") or ""
            if not pid:
                continue
            models = p.get("models") or []
            p_items.append(MenuItem(
                label=str(p.get("label") or pid),
                value=pid,
                description=f"{len(models)} 个模型",
            ))
        pid = select_from_menu("/model · 选择模型提供方", p_items)
        if pid is None:
            return None
        provider = next(
            (p for p in providers if (p.get("provider") or p.get("id")) == pid), None)
        models = (provider or {}).get("models") or []
        m_items = [
            MenuItem(
                label=str(m.get("display_name") or m.get("id") or ""),
                value=f"{pid}/{m['id']}",
            )
            for m in models if m.get("id")
        ]
        picked = select_from_menu(f"/model · {pid} 选择模型", m_items)
        if picked is None:
            return None
        return f"/model {picked}"

    def _menu_agent(self) -> str | None:
        """/agent 多步：从代理列表单选。失败返回 None。"""
        agents = cli_client.list_agents(self._token)
        if not agents:
            print(f"\n  {_C.LYELLOW}代理列表加载失败，请手动输入 /agent 名称{_C.RST}")
            return None
        items = [
            MenuItem(label=str(a.get("display_name") or a.get("name") or ""),
                     value=str(a["name"]))
            for a in agents if a.get("name")
        ]
        picked = select_from_menu("/agent · 选择子代理", items)
        if picked is None:
            return None
        return f"/agent {picked}"

    def _try_expand_multistep(self, cmd: str, arg: str) -> str | None:
        """多步命令无参数时弹菜单，返回完整命令；否则返回 None（不展开）。"""
        if arg.strip() or not _HAS_MENU:
            return None
        if cmd == "/model":
            return self._menu_model()
        if cmd == "/agent":
            return self._menu_agent()
        if cmd == "/voice":
            return self._menu_fixed("/voice", ["on", "off"])
        if cmd == "/doctor":
            return self._menu_fixed("/doctor", ["json", "fix"])
        if cmd == "/cost":
            return self._menu_fixed("/cost", ["7d"])
        if cmd == "/cam":
            return self._menu_fixed("/cam", ["snap"])
        return None
```

> 说明：`cli.py` 顶部已 `import cli_client`，`_C` 为现有颜色类，`MenuItem`/`select_from_menu` 已由 Step 3 导入。

- [ ] **Step 7: 修改 `_dispatch_slash_command` 支持多步展开**

将现有方法体替换为：

```python
    def _dispatch_slash_command(self, text: str) -> None:
        """分发斜杠命令：/help 本地展示，其余交给主进程共享 AgentCore 处理。

        多步命令（/model /agent /voice /doctor /cost /cam）在无参数时先弹菜单选择，
        拼接完整命令后发送；有参数则直接发送。主进程 core.process() 内部识别并
        执行命令，故 CLI 无需本地 AgentCore。
        """
        stripped = text.strip()
        if stripped.startswith("//"):
            return  # 转义斜杠：作为普通消息发送
        cmd, _, arg = stripped.partition(" ")
        cmd_l = cmd.lower()
        if cmd_l == "/help":
            self._print_help()
            return
        # 多步命令：无参数时弹出菜单选择
        if not arg.strip():
            expanded = self._try_expand_multistep(cmd_l, arg)
            if expanded is not None:
                stripped = expanded
                cmd = stripped.split(maxsplit=1)[0].lower()
        if self._ws is None:
            self._print_unknown(cmd)
            return
        try:
            result = self._loop.run_until_complete(self._ws.chat(stripped))
        except Exception as e:
            logger.error("cli.slash_dispatch_error", command=cmd, error=str(e))
            print(f"\n  {_C.LYELLOW}执行 {cmd} 时出了点问题：{str(e)[:100]}{_C.RST}\n")
            return
        if result is None or not str(result).strip():
            self._print_unknown(cmd)
            return
        self._print_command_result(cmd, result)
```

- [ ] **Step 8: 修改 `run()` 使用 prompt_toolkit 输入**

将 `run()` 中 `self._print_welcome()` 之后、`while True:` 之前插入 `self._init_prompt_session()`，并把输入获取改为：

```python
        try:
            prompt = f"  {_C.GREEN}{_C.BOLD}🌿 {self._address_term()}:{_C.RST} "
            if self._session is not None:
                user_input = self._session.prompt(message=ANSI(prompt)).strip()
            else:
                user_input = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            farewell = random.choice(NAHIDA_FAREWELLS).replace("爸爸", self._address_term())
            print(f"\n  {_C.LGREEN}{farewell}{_C.RST}\n")
            break
```

- [ ] **Step 9: 运行测试验证通过**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_cli_multistep.py -v`
Expected: PASS（4 passed）。

- [ ] **Step 10: 语法与回归检查**

Run: `cd /home/orangepi/ai-agent && python -m py_compile cli.py cli_client.py cli_menu.py && python -m pytest tests/test_cli_client_http.py tests/test_cli_menu.py tests/test_cli_multistep.py -q`
Expected: 三个测试文件全部 PASS，`py_compile` 无输出（成功）。

- [ ] **Step 11: 手动验收（主进程在线）**

Run: `cd /home/orangepi/ai-agent && python cli.py`
Expected:
- 输入 `/` 立即弹出命令下拉（前缀过滤），方向键/继续输入缩小范围。
- 输入 `/` 后按 Tab 也可补全。
- 输入 `/model` 回车 → 弹出「选择模型提供方」菜单，选 provider → 弹出该 provider 模型菜单，选模型 → 执行 `/model provider/model`。
- 输入 `/agent` 回车 → 弹出代理菜单，选择后执行 `/agent 名称`。
- 输入 `/voice` 回车 → on/off 菜单。
- 菜单中按 Esc 取消 → 回到提示符，无副作用。
- prompt_toolkit 缺失回退：临时 `pip uninstall prompt-toolkit` 后 `python cli.py` 仍可正常聊天（readline 路径，无 `/` 下拉）。
- 普通聊天、`/help`、exit/Ctrl+C 退出流程不受影响。

- [ ] **Step 12: Commit**

```bash
git add cli.py tests/test_cli_multistep.py
git commit -m "feat(cli): prompt_toolkit 交互输入 + / 弹出命令下拉 + 多步菜单选择"
```

---

## Self-Review

- **Spec 覆盖**：
  - 4.1 输入层改造 → Task 4 Step 8。
  - 4.2 SlashCompleter → Task 4 Step 5。
  - 4.3 cli_menu → Task 3。
  - 4.4 多步命令调度（/model /agent /voice /doctor /cost /cam）→ Task 4 Step 6/7。
  - 4.5 cli_client discover_models/list_agents → Task 2。
  - 7 依赖与打包 → Task 1。
  - prompt_toolkit 缺失回退 → Task 4 Step 3（`_HAS_PROMPT_TOOLKIT`）+ run() 双路径。
  - 数据拉取失败回退手动输入 → `_menu_model`/`_menu_agent` 返回 None 并提示。
- **Placeholder 扫描**：所有代码步骤均含完整可运行代码，无 TBD/TODO。
- **类型一致性**：`MenuItem(label, value, description)`、`select_from_menu(title, options, hint="")`、`discover_models(token,...)->list[dict]`、`list_agents(token,...)->list[dict]`、`_try_expand_multistep(cmd, arg)->str|None` 在 Task 3/4 中签名一致；测试中的调用与实现参数一一对应。