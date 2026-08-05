# xiaoda CLI 用 Textual 重构为可点击富文本 TUI 设计文档

- **日期**：2026-08-05
- **作者**：爸爸 × AI 协作
- **状态**：待审查
- **关联目标**：解决 CLI 美术不统一、设计不统一、无法鼠标点击三大痛点；保持 CLI 在终端内运行，用富文本 TUI 统一纳西妲美术，重点优化斜杠命令面板。

## 1. 背景与目标

### 1.1 用户反馈的问题

1. **美术没有统一**：现有 CLI 的 banner 是 ASCII 字符画 + ANSI 颜色，与 WebUI 的纳西妲风格（藤蔓、绿色系、圆角）完全不是一套视觉语言。
2. **CLI 设计没有统一**：补全下拉（prompt_toolkit 默认样式）、多步命令菜单（`cli_menu.py` 方向键纯文本）、banner 各用各的风格，缺乏统一设计语言。
3. **体验不能点击**：终端里补全下拉和菜单项都无法鼠标点击，只能键盘导航，体验割裂。

### 1.2 目标

- **G1**：用 Textual 把 CLI 重做为可鼠标点击的富文本 TUI，保持 `xiaoda` 在终端内运行。
- **G2**：斜杠命令面板重点设计：输入 `/` 弹出，支持搜索过滤、分组展示、鼠标点击执行、多步命令二级选择。
- **G3**：美术统一为纳西妲绿色主题 + 藤蔓边框，与 WebUI 视觉语言一致。
- **G4**：仍作为主进程客户端，复用 `cli_client.py` 共享同一 AgentCore（模型、记忆、上下文与 WebUI 一致）。
- **G5**：跨终端兼容：Textual 不可用或终端不支持时，自动降级回现有 prompt_toolkit CLI。

### 1.3 非目标

- 不改变主进程（`web/server.py` / AgentCore / 各 API）的任何行为，TUI 只做客户端。
- 不引入浏览器/桌面窗口形态（用户明确要求保持 CLI）。
- 不重写命令定义，命令权威数据源仍为 `slash_commands.py`。
- 本次不做 Windows 旧 `cmd.exe` 的 Textual 适配，仅做降级检测。

## 2. 架构总览

```
                      ┌────────────────────────────────────────────┐
                      │  xiaoda 命令入口（scripts/xiaoda）          │
                      └───────────────────┬────────────────────────┘
                                          │
                         判定终端是否支持 Textual（true/false）
                                          │
                    ┌─────────────────────┴──────────────────┐
                    ▼                                        ▼
        ┌─────────────────────────┐            ┌──────────────────────────┐
        │  Textual TUI (cli_app.py)│            │  prompt_toolkit CLI      │
        │  · 聊天区 + 输入框        │            │  (cli.py) 降级路径       │
        │  · 斜杠命令面板(可点击)   │            │  （现状保留）            │
        └────────────┬────────────┘            └───────────┬──────────────┘
                     │  复用 cli_client.py                   │ 复用 cli_client.py
                     ▼                                      ▼
        ┌──────────────────────────────────────────────────────────────┐
        │  cli_client.py（WSClient / fetch_token / discover_models /   │
        │               cross-platform 拉起 / 心跳 ping→pong）          │
        └───────────────────────────────┬──────────────────────────────┘
                                        ▼
        ┌──────────────────────────────────────────────────────────────┐
        │  主进程（web/server.py · 共享 AgentCore）                      │
        │  · HTTP /api/v1/*（斜杠命令、模型、token）                    │
        │  · WebSocket /ws（聊天、流式、心跳）                          │
        └──────────────────────────────────────────────────────────────┘
```

**关键约束**：TUI（`cli_app.py`）与降级路径（`cli.py`）都只通过 `cli_client.py` 访问主进程，复用同一套已修复的客户端逻辑（含心跳 ping→pong）。命令定义为 `slash_commands.py` 单一数据源。

## 3. 模块设计

### 3.1 `cli_app.py`（Textual App，新增）

Textual 应用入口，组织为自上而下的布局：

```
┌──────────────────────────────────────────────┐
│  Header：⚜ 小妲 · 白草净华 · <模型ID>        │  ← 模型/连接状态
├──────────────────────────────────────────────┤
│                                              │
│  ChatView（消息流）                           │  占满中部
│   · 用户消息（绿色/右侧）                     │
│   · 助手回复（正文，Markdown）                │
│   · 工具状态/推理（灰色小字，可折叠）          │
│   · 流式更新                                 │
│                                              │
├──────────────────────────────────────────────┤
│  🌿 爸爸: [输入框]          （输入 / 弹面板）  │  Footer 输入
└──────────────────────────────────────────────┘
```

**交互**：
- 普通消息：回车发送 → `WSClient.chat()` → 流式渲染回复。
- 输入 `/`：弹出斜杠命令面板（见 3.2）。
- 命令面板选中多步命令 → 二级面板（见 3.2）。
- `exit` / Ctrl+C 退出。
- 连接失败：Header/面板内显示错误，不闪退。

**组件拆分**（每条职责单一、可独立测试）：
- `SlashPanel`：命令面板屏幕（搜索框 + 分组命令列表）。
- `ChatView`：消息列表渲染（含 Markdown、工具状态折叠、流式更新）。
- `App` 主体：布局、输入框、事件分发（普通消息 vs 斜杠命令 vs 多步展开）。

### 3.2 斜杠命令面板（核心）

**触发**：输入框首字符为 `/` 时弹出 `SlashPanel`（ModalScreen）。

**面板项生成**：从 `slash_commands.py` 权威数据源构建，不新造命令定义：
- 遍历 `COMMAND_DESCRIPTIONS`（命令名 + 描述）生成面板项。
- 用 `COMMAND_META` 的 `usage` 补充参数提示；用 `COMMAND_ALIASES` 标注别名。
- 按用途分组：聊天/记忆/系统/诊断/模型 等（分组规则见 3.3）。

**面板交互**：
- 搜索框实时过滤（按命令名或描述关键字）。
- 列表支持 ↑↓ 键盘导航 + **鼠标点击**选择。
- Enter 或点击选中：若命令是多步命令（`/model /agent /voice /doctor /cost /cam`），进入二级面板；否则执行命令返回聊天。

**多步命令二级面板**：
- `/model`：先列 provider（`cli_client.discover_models`）→ 再列该 provider 下模型 → 点击执行。
- `/agent`：列出子代理（`cli_client.list_agents`）→ 点击执行。
- `/voice ≈ [on|off]`、`/doctor ≈ [json|fix]`、`/cost ≈ [7d]`、`/cam ≈ [snap]`：列出固定参数（来自 `COMMAND_META.arg_completions`）→ 点击执行。

**执行**：生成完整命令串（如 `/model openai/gpt-4o`）→ 走主进程斜杠命令通道（HTTP API），与现有一致。

### 3.3 分组规则

为面板分组，定义命令 → 分组的映射（作为面板展示元数据，不改变命令语义）：

| 分组 | 命令 |
|------|------|
| 聊天 | `/help /reset /compress /learn /note` |
| 模型 | `/model /status /cost` |
| 记忆 | `/memory /forget /knowledge /self` |
| 诊断 | `/doctor /debug /sys /hw /emotion` |
| 设备 | `/cam /voice` |
| 子代理 | `/agent` |
| 工作流 | `/wf` |

（分组为展示层元数据，若后续命令增减，仅需维护该映射。）

### 3.4 消息渲染

- 用户消息：绿色、右对齐气泡感。
- 助手回复：正文支持 Markdown（Textual `Markdown` widget），保持 WebUI 同款排版感。
- 工具状态 / 推理过程：灰色小字，可折叠，避免刷屏。
- 流式：复用 WS `stream_text` 事件，实时追加到当前助手消息。

### 3.5 主进程连接（复用 `cli_client.py`）

- 启动：`ensure_main_process()`（已跨平台：Linux systemd / Windows / macOS）→ `fetch_token()` → `WSClient.connect()`。
- 聊天：`WSClient.chat()`（已含服务端心跳 ping→pong 修复，避免长回复被踢）。
- 斜杠命令：复用现有 HTTP 通道 / 由主进程共享 AgentCore 处理。
- 模型/子代理多步数据：`discover_models()` / `list_agents()`。

### 3.6 美术主题（Textual CSS）

统一纳西妲视觉语言：
- 主色草绿（`#8bc34a` 系），背景深绿/米色，强调金色。
- 面板、消息卡、Header/Footer 用藤蔓风格边框（Textual border + 圆角）。
- emoji 图标（🌿✿⚜）与分组色块。
- 与 WebUI 品牌色一致，避免两套美术。

## 4. 降级与跨平台

- 入口 `scripts/xiaoda`（或 `cli` 启动器）检测：Textual 可导入 && 终端为支持 TUI 的 TTY（`sys.stdin.isatty()` 等）→ 走 `cli_app.py`；否则回退 `cli.py`。
- Windows 旧 `cmd.exe`（不支持 TUI）→ 自动走 `cli.py` 降级。
- Windows Terminal / Linux / macOS 现代终端 → Textual。
- 检测失败或 Textual 启动异常：捕获并打印提示后回退 `cli.py`，不闪退。

## 5. 打包

- `requirements.txt`：新增 `textual`。
- `xiaoda-agent.spec`：把 `textual` 及其依赖打包进 PyInstaller bundle。
- `agent.py --cli`：接入 TUI 入口判定（Textual 可用走 TUI，否则降级 `cli.py`）。

## 6. 错误处理

| 场景 | 处理 |
|------|------|
| 主进程不可达 | 面板/Header 显示错误，不闪退 |
| token 获取失败（废弃/密码错） | 显示错误并退出引导 |
| Textual 导入失败 / 非 TTY | 降级回 `cli.py` |
| Textual 运行时异常 | 捕获，打印后回退 `cli.py` |
| WS 连接中断 | 提示并尝试重连 |

## 7. 测试

- **命令面板数据生成**：从 `COMMAND_DESCRIPTIONS`/`COMMAND_META`/`COMMAND_ALIASES` 生成的分组面板项数量、命令名、描述、别名正确。
- **多步命令展开**：`/model`（provider→模型）、固定参数命令（`/voice on/off` 等）展开逻辑正确。
- **降级检测**：Textual 不可用 / 非 TTY 时判定回退 `cli.py`；可用时走 `cli_app.py`。
- **分组映射**：所有 `COMMAND_DESCRIPTIONS` 命令都有分组归属。

## 8. 实施顺序（建议）

1. **阶段 1**：`requirements.txt` 加 `textual`；搭建 `cli_app.py` 空壳 App（Header/聊天区/输入框），`agent.py --cli` 接入 TUI 判定。
2. **阶段 2**：聊天区 + 消息渲染（Markdown、工具状态折叠、流式）+ 复用 `cli_client.py` 连接主进程。
3. **阶段 3**：斜杠命令面板（`SlashPanel`：搜索、分组、鼠标点击、`/help` 本地展示）。
4. **阶段 4**：多步命令二级面板（/model→provider→模型、/agent、固定参数命令）。
5. **阶段 5**：美术主题打磨（藤蔓边框、绿色系、分组色块、Header/Footer）。
6. **阶段 6**：降级检测 + `xiaoda-agent.spec` 打包 + 单测 + 手动验证。

## 9. 文件清单

### 新增
- `cli_app.py`（Textual App + SlashPanel + ChatView）
- `tests/test_cli_app_commands.py`（面板数据生成 / 多步展开 / 分组映射）
- `tests/test_cli_tui_entry.py`（降级检测逻辑）

### 修改
- `requirements.txt`（新增 textual）
- `xiaoda-agent.spec`（打包 textual）
- `agent.py`（`--cli` 接入 TUI 判定）
- `scripts/xiaoda`（可选：入口判定，或留在 agent.py 内）

## 10. 风险与缓解

| 风险 | 缓解 |
|------|------|
| Textual 依赖体积/TUI 渲染开销 | 富文本渲染轻量；仅现代终端启用，旧终端走轻量的 `cli.py` |
| 旧终端（cmd.exe）不支持 | 降级检测回退 `cli.py`，不破坏现状 |
| 流式与面板状态冲突 | ChatView 与 SlashPanel 分离，事件分发集中到 App 主体 |
| 命令面板命令过多 | 分组 + 搜索过滤缓解 |
| 与主进程共享不一致 | 复用 `cli_client.py` 单一客户端，不新开连接逻辑 |