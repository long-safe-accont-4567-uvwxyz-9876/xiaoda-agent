# CLI 交互式斜杠命令设计（prompt_toolkit）

- **日期**：2026-08-05
- **状态**：已批准，待实现
- **关联 PR**：`feat/cli-shared-process`（CLI 客户端化，本设计在其之上）

## 1. 背景与问题

CLI 当前使用 `input()` + readline 处理输入，斜杠命令存在两个明显的人性化缺陷：

1. **输入 `/` 不会自动弹出命令列表**，只能靠 Tab 补全 + 手动记忆命令名，非常难受。
2. **很多命令是多步操作**（如 `/model` 要先背 provider 再背模型 id、`/agent` 要背代理名），
   需要用户手动敲完整参数，不直观。

目标：`/` 输入即弹出命令下拉，多步命令改为**菜单选择器**逐步选择，方向键 + 回车完成。

## 2. 技术选型

- **prompt_toolkit**：跨平台（Windows 原生终端也支持）、支持实时补全下拉、样式可定制，最适合当前的交互升级。
- 新增依赖 `prompt_toolkit`（加入 `requirements`，并检查 PyInstaller spec 是否需要补充 hiddenimports）。
- 若 prompt_toolkit 未安装（旧安装包），**优雅回退**到现有 readline 行为，不崩溃。

## 3. 架构总览

```
用户输入 ──► prompt_session.prompt() ──► SlashCompleter（/ 实时弹命令下拉）
                                        │
                                        ▼
                              ┌─ 多步命令（/model /agent /voice ...）
                              │      └─► 菜单选择器（方向键/回车）─► 拼接完整命令
                              └─ 普通命令 ────────────────────────►
                                        │
                                        ▼
                              发给主进程（WS chat / HTTP 拉取选项）
```

- **输入层**：`cli.py` 的 run 循环改用 prompt_toolkit 的 `prompt()`，替换 `input()` + readline。
- **命令补全**：自定义 `Completer`，缓冲区以 `/` 开头时实时弹出匹配命令（前缀过滤）。
- **菜单选择器**：可复用的交互式单选菜单（新增小模块）。
- **多步调度**：命令层识别多步命令 → 拉取选项 → 逐步菜单选择 → 发给主进程。
- **数据来源**：`cli_client.py` 补回 HTTP 助手，从主进程拉取模型/代理选项。

## 4. 组件设计

### 4.1 输入层（改造 `cli.py`）

- 用 `prompt_session.prompt(message, completer=..., complete_while_typing=True, history=...)`
  替换 `input()`。
- 保留：颜色、欢迎界面、typewriter 输出、斜杠命令历史。
- 打印时机：状态消息与最终回复都在**两次 prompt 之间**输出（当前流程如此），
  prompt_toolkit 每次迭代新建 prompt，天然兼容，无需特殊处理。
- **回退**：`try: import prompt_toolkit` 失败时走回退路径（保留现有 readline 逻辑），
  保证旧安装可用。

### 4.2 命令补全（SlashCompleter）

- 复用现有两级补全思路：`/` 输入时按前缀过滤命令名；命令后跟空格时按参数补全。
- 命令列表来源维持 `slash_commands.COMMAND_DESCRIPTIONS` + 别名（与 WebUI 同源）。
- prompt_toolkit `Completer` 返回 `Completion` 列表，实时渲染下拉。
- 参数补全：`/model` 动态拉取已发现模型（对齐 WebUI），其余命令用
  `slash_commands.get_argument_completions`。

### 4.3 菜单选择器（新增 `cli_menu.py`）

可复用交互式单选菜单，基于 prompt_toolkit 实现：

- 上下方向键高亮、回车确认、Esc 取消。
- 入参：`title`、`options: list[MenuItem]`。
- 出参：选中的索引或 `None`（取消）。
- 样式与现有 nahida 配色一致（绿/青，非单调暗底）。

### 4.4 多步命令调度（`cli.py` 命令分发）

| 命令 | 步骤 | 数据源 |
|------|------|--------|
| `/model` | 选 provider → 选模型 | `GET /models/discover` |
| `/agent` | 选代理 | `GET /agents` |
| `/voice` | 选 on/off | 固定选项 |
| `/doctor` | 选默认/json/fix | 固定选项 |
| `/cost` | 选 今日/近7天 | 固定选项 |
| `/cam` | 选 snap | 固定选项 |

- 选择完成后拼接完整命令（如 `/model siliconflow/Qwen2.5`）发给主进程 WS。
- 普通命令直接发送，无需多步。
- 用户 Esc 取消 → 返回输入状态，不误发。

### 4.5 数据获取（`cli_client.py` 补回 HTTP 助手）

- `discover_models(token)` → 调 `GET /api/v1/models/discover`，返回 provider 列表。
  - 兼容字段：provider 项可能用 `provider` 或 `id` 标识，模型项含 `id`/`display_name`/`free`。
- `list_agents(token)` → 调 `GET /api/v1/agents`，返回代理列表（含名字/显示名）。
- 拉取失败 → 返回空并提示，回退到手动输入，不阻塞主流程。

## 5. 数据流

1. 用户输入 `/` → `SlashCompleter` 弹出命令下拉。
2. 选完命令回车 → 若为多步命令，进入菜单选择器逐步选择。
3. 多步命令需动态选项时，先从主进程 HTTP 拉取（`discover_models` / `list_agents`）。
4. 拼接最终命令文本 → 通过 `WSClient.chat()` 发给主进程共享 AgentCore。
5. 主进程返回结果 → CLI 用现有 typewriter 展示。

## 6. 错误处理

- prompt_toolkit 缺失 → 回退 readline，不崩溃。
- 菜单数据拉取失败 → 提示"选项加载失败，请手动输入"，回退手动键入。
- 多步菜单 Esc 取消 → 丢弃本次选择，返回提示符。
- 主进程不可达 → 沿用现有 `_connect_main_process` 报错不闪退逻辑。

## 7. 依赖与打包

- 新增 `prompt_toolkit` 到 `requirements`。
- 检查 `xiaoda-agent.spec` 的 hiddenimports，若需要补充 prompt_toolkit 相关模块则补齐。
- 更新 `scripts/start.sh` 无需改动（仍运行 `cli.py`）。

## 8. 测试策略

- 本地主进程在线：验证 `/` 弹出下拉、方向键选择、`/model`/`/agent` 菜单逐步选择并成功执行。
- prompt_toolkit 缺失回退：临时禁用依赖，确认 CLI 仍可用（readline 路径）。
- 数据拉取失败：模拟 API 异常，确认回退手动输入。
- 回归：普通聊天、`/help`、Tab 补全、退出流程不受影响。

## 9. 范围外（YAGNI）

- 不做表情包/贴纸在 CLI 的展示（保持现状）。
- 不做多选命令（如一次切多个模型）。
- 不做 CLI 内嵌 WebUI 渲染。