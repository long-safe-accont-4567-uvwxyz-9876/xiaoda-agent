# Task 1 报告：`cli_common.py` 共享助手 + textual 依赖

## 状态
**DONE_WITH_CONCERNS**

## 变更内容
本次抽取 CLI 与 TUI 共享的纯函数到 `cli_common.py`，并新增 textual 依赖，为后续 Textual TUI 重构打底。

### 1. `requirements.txt`
- 在 `rich>=13.9.0`（行 22）之后新增：
  ```
  # ── Textual TUI（cli_app.py 使用；缺失时降级到 prompt_toolkit CLI）──
  textual>=0.80.0
  ```

### 2. 新建 `cli_common.py`
按控制器裁决，将**丰富版** `_status_translate`（含 `STATUS_MAP`、`AGENT_NAMES`，含 config/emotion 动态导入逻辑）整体迁入，未采用简报的简化版，避免降级路径翻译退化。包含：
- `STYLE: dict[str, str]`（纳西妲绿色色板，与简报一致）
- 丰富版 `STATUS_MAP` / `AGENT_NAMES` / `status_translate()`（保留 `try: from config import ...; from emotion.emoji_config import get_ack_message` 动态逻辑，ImportError 时用兜底 dict）
- `get_model_info(token="")`（复用 `cli_client.get_chat_model_label`，无 token 返回 `"mimo-v2.5"`，带异常兜底）
- `command_entries()`（从 `slash_commands.COMMAND_DESCRIPTIONS`/`OWNER_ONLY_COMMANDS` 返回 (public, owner)）
- `address_term()`（动态读 `AgentCore.read_address_term_from_user_md`，兜底 `"朋友"`）
- `cli_should_use_tui()`（textual 可导入 && `sys.stdin.isatty()` && TERM 非 dumb）

### 3. 新建 `tests/test_cli_common.py`
按控制器裁决调整了两个断言以匹配丰富版行为：
- `test_status_translate_maps_known`：`status_translate("thinking")` 返回非空且含 `"小妲"`（ACK 消息或默认文案）
- `test_status_translate_falls_back`：`"zzz_unknown" in status_translate("zzz_unknown")`（丰富版未知消息返回 `f"🌿 {msg}"` 包裹原串）
其余测试（STYLE 关键色、get_model_info 兜底、command_entries 含 /help、address_term 兜底、cli_should_use_tui 返回 bool）沿用简报。

### 4. 重构 `cli.py`
- 顶部 `import cli_client`/`import contextlib` 之后新增 `from cli_common import STYLE, STATUS_MAP, AGENT_NAMES, status_translate, get_model_info, command_entries, address_term`
- 新增兼容别名：`_status_translate = status_translate`、`_get_model_info = get_model_info`、`_command_entries = command_entries`
- 删除本地 `STATUS_MAP`、`AGENT_NAMES`（264-290 行）、`_command_entries`（306 行）、`_get_model_info`（315 行）、`_status_translate`（349 行）
- `CLIInterface._address_term` 方法体改为 `return address_term()`
- **保留** `_typewriter`、`NAHIDA_ASCII`、`_refresh_model_arg_cache` 与 `_MODEL_ARG_CACHE`（prompt_toolkit 补全专用，不迁移）

## 验证输出
| 命令 | 结果 |
| --- | --- |
| `pytest tests/test_cli_common.py -v`（红，重构前） | ERROR：`ModuleNotFoundError: No module named 'cli_common'` |
| `pytest tests/test_cli_common.py -v`（绿，实现后） | **7 passed in 1.55s** |
| `pytest tests/test_cli_multistep.py tests/test_cli_menu.py -q`（回归） | **7 passed in 0.46s** |
| `py_compile cli.py cli_common.py` | 通过（COMPILE_OK） |

## 提交
- `9031e94` feat(cli): 抽取 cli_common 共享助手与主题色板，新增 textual 依赖（4 files changed, 197 insertions(+), 91 deletions(-)）
  - 涉及文件：`requirements.txt`、`cli_common.py`（新增）、`tests/test_cli_common.py`（新增）、`cli.py`

## Self-review 发现
- 三处改动均与控制器裁决一致；丰富版 `status_translate` 行为与重构前完全相同（同一份代码迁入）。
- `STATUS_MAP`/`AGENT_NAMES` 在 cli.py 现仅被导入（由 cli_common 的 `status_translate` 使用），无其他直接引用——按控制器要求保留导入以兼容解析。
- `/help` 属于 `slash_commands.COMMAND_DESCRIPTIONS` 且不在 `OWNER_ONLY_COMMANDS`，故 `command_entries()` 返回公共组，`test_command_entries_include_help` 通过。
- 未提交无关的未跟踪/改动文件（`debug-*.md`、`.superpowers/sdd/*`）。
- **关注点（重要）**：简报/控制器要求 `_C = STYLE`，但 cli.py 的 `_C` 实际是**类**（`class _C`，含 `_C.RST`/`_C.LYELLOW` 等 ANSI 码属性，约 30 处调用点），并非色板 dict。若改为 `_C = STYLE`（dict）会使 `_C.RST` 等属性访问抛 `AttributeError`，导致降级路径崩溃。**故保留现有 `_C` 类**并维持 `STYLE`（Textual 用）与 `_C`（降级路径用）分离，未做 `_C = STYLE` 别名。这与"不使降级路径退化、保持现有调用点不变"的核心要求一致，但偏离了简报字面指令，需父代理知悉。

## 关注点
1. `_C = STYLE` 未执行（原因见上，为避免回归）。`_C` 类与 `STYLE` dict 是不同表示（ANSI 码 vs RGB hex），本任务按"行为不变"原则保留现状。
2. `get_model_info` 在 cli_common 中带 `try/except` 兜底返回 `"mimo-v2.5"`，与 cli.py 原实现（无异常捕获）略有差异——属可接受的增强，不影响测试。