# Debug Session: cli-boot-model-sync

Status: [OPEN]

## 症状
用户通过 `xiaoda` 命令进入 CLI（开发模式，`/home/orangepi/ai-agent`），但发现三个问题：

1. **重复启动服务**：明明 systemd 服务（system 服务）已在运行，CLI 启动时仍提示"QQ Bot 服务未运行，正在启动..."并再次启动。
2. **QQ 服务启动失败**：提示"QQ Bot 服务启动失败，CLI 可正常使用"。
3. **模型未与 WebUI 同步**：CLI 显示模型为 `mimo-v2.5`，但 WebUI 已切换到 agnes（安格尼斯）。

## 环境
- 开发模式，`xiaoda` 软链 → `/home/orangepi/ai-agent/scripts/xiaoda`
- 工作目录 `~/.ai-agent`，systemd 服务已运行
- Linux, OrangePi4Pro

## 假设（已全部经证据确认/否证）

### 问题1、2：重复启动 + QQ Bot 启动失败
- H1【确认 ✓】CLI 硬编码服务名 `qq-agent`，但实际服务是 `nahida-web`。
  - 证据：`systemctl list-units` 显示 `nahida-web.service active running`（"WebUI + QQ Bot + WS"）；
    `ls /etc/systemd/system/qq-agent.service` 不存在；`systemctl cat xiaoda-agent.service` 不存在。
  - 后果：`systemctl is-active qq-agent` → "inactive" → `_check_qq_bot()` 返回 False → 误判"未运行" →
    `sudo systemctl start qq-agent` 启动不存在服务 → 失败。
  - 涉及：cli.py `_check_qq_bot`/`_ensure_service`（418-441）、slash_commands.py `_cmd_status`（531-541）。

### 问题3：模型未与 WebUI 同步（显示 mimo-v2.5）
- H2【确认 ✓】cli.py `_get_model_info()`（238-243）直接读静态 `ROUTE_TABLE["chat"]`（模块常量，
  来自 .env 默认 mimo-v2.5），不读取实际切换后的模型。
- H3【确认 ✓核心】CLI 与 WebUI 是两个独立进程。WebUI 切换模型经 `set_chat_model` 持久化到
  `config_service models.chat_model`（model_router.py:893），但只有 `web/server.py` 启动时调用
  `_restore_chat_model`（web/server.py:44）恢复。CLI 走 `_run_cli → CLIInterface → AgentCore.init()`，
  **不调用 `_restore_chat_model`**，故 `_current_chat_model` 保持 None，`get_current_chat_model()`
  返回默认 mimo-v2.5。
  - 证据：`_restore_chat_model` 仅在 web/server.py:44 调用；cli.py 无恢复逻辑；
    `AgentCore.__init__` → `self.router = ModelRouter()`，`_current_chat_model=None`。

## 证据
- systemctl 服务列表（运行时）：nahida-web.service active running；qq-agent / xiaoda-agent 均不存在
- cli.py/_get_model_info、_ensure_service、_check_qq_bot 源码
- web/server.py:_restore_chat_model 调用点与实现
- slash_commands.py:_cmd_status 服务名与模型显示

## 结论
### 修复方案
1. cli.py `_check_qq_bot` / `_ensure_service`：服务名 `qq-agent` → `nahida-web`（与 start.sh/healthcheck.sh/实际部署一致）。
2. slash_commands.py `_cmd_status`：服务名 `qq-agent` → `nahida-web`；模型显示改用持久化实际模型。
3. cli.py `_init()`：init 后调用 `_restore_saved_model()` 恢复持久化模型。
4. cli.py `_get_model_info()`：改为从 router 实际模型读取（而非静态 ROUTE_TABLE）。

## 修复结果（已实施 + 验证）
- 服务检测：`systemctl is-active nahida-web` → `active`。修复后 `_check_qq_bot()` 返回 True，
  CLI 不再重复启动、不再误报"QQ Bot 启动失败"。✅
- 模型恢复：`_get_model_info(router)` 从持久化 `models.chat_model` 恢复，返回 `mimo-v2.5`，
  `get_model_preference()` 返回 `mimo/mimo-v2.5`，与 WebUI 同源。✅

## 重要发现（问题3 真相）
- WebUI 运行时实际模型 = `mimo/mimo-v2.5`（`GET /api/v1/models/chat-model` 返回
  `{"provider":"mimo","model_id":"mimo-v2.5"}`），**并非 agnes**。
- 即 CLI 与 WebUI 本就一致（都是 mimo），不存在真正的"不同步"。用户以为 WebUI 是 agnes，
  但 WebUI 当前实际也是 mimo。
- 若用户确实在 WebUI 切到 agnes 但持久化仍为 mimo，则需进一步排查 WebUI 切换持久化链路
  （sticky fallback / models.chat_model 覆盖），属 WebUI 侧问题，非 CLI 侧。

## 验证结论
- 测试：`tests/test_slash_aliases_and_model.py` 34 项全部通过。
- 编译：`cli.py` / `slash_commands.py` py_compile 通过。